from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from codeinsight.analysis import flow_analysis as fa
from codeinsight.application.boundary_service import BoundaryItem, BoundaryService
from codeinsight.application.config_service import ConfigItem, ConfigService
from codeinsight.application.describe_service import DescribeService
from codeinsight.application.external_service import (
    INPUT_OPERATIONS,
    EffectSummary,
    ExternalService,
    ExternalUse,
)
from codeinsight.application.flow_service import (
    ExceptionReport,
    FlowAnalysisError,
    FlowService,
)
from codeinsight.application.history_service import HistoryService, SymbolHistory
from codeinsight.application.impact_service import ImpactReport, ImpactService
from codeinsight.application.navigation_service import NavigationService, ReferenceHit
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.risk_service import Finding, RiskService
from codeinsight.application.test_map_service import TestMapService, TestMapping
from codeinsight.domain import Language, Project, Symbol, SymbolKind

_INPUT_CATEGORIES = ("filesystem", "network", "database", "config", "process", "persistence")
_GENERIC_NAMES = frozenset({"main", "run", "get", "set", "load", "save", "init", "start", "stop", "close", "open", "read", "write"})
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".org")
_TODO = re.compile(r"#.*\b(TODO|FIXME|XXX|HACK)\b")


@dataclass(frozen=True)
class Parameter:
    name: str
    annotation: str
    default: str
    kind: str  # positional / keyword-only / *args / **kwargs


@dataclass
class Understanding:
    """1つの関数・メソッドについて、コードリーディングの8つの問いに、確認できた事実で答えたもの。

    すべて静的解析・Gitの履歴・ソース中の記述から得た事実で、理由や意図の推測は含まない。
    確認できなかったこと・対応範囲外のことは limitations に明示する。
    """

    symbol: Symbol
    path: str
    language: Language
    summary: str
    declaration: list[tuple[int, str]]
    module_summary: str
    # 1. なぜ存在するのか
    test_names: list[str] = field(default_factory=list)
    doc_mentions: list[tuple[str, int, str]] = field(default_factory=list)
    todo_comments: list[tuple[int, str]] = field(default_factory=list)
    # 2. 誰が呼ぶのか
    callers: list[ReferenceHit] = field(default_factory=list)
    caller_total: int = 0
    registrations: list[BoundaryItem] = field(default_factory=list)
    # 3. 何を入力するのか
    parameters: list[Parameter] = field(default_factory=list)
    caller_arguments: dict[str, list[str]] = field(default_factory=dict)
    config_reads: list[ConfigItem] = field(default_factory=list)
    external_inputs: list[ExternalUse] = field(default_factory=list)
    # 4. 何を変更するのか
    state_changes: list[str] = field(default_factory=list)
    parameter_mutations: list[str] = field(default_factory=list)
    effects: EffectSummary | None = None
    # 5. 何を返すのか
    return_annotation: str = ""
    returns: list[tuple[int, str]] = field(default_factory=list)
    is_generator: bool = False
    is_async: bool = False
    # 6. 誰に影響するのか
    impact: ImpactReport | None = None
    tests: TestMapping | None = None
    # 7. 失敗するとどうなるのか
    exceptions: ExceptionReport | None = None
    caught_by_callers: dict[str, tuple[int, int]] = field(default_factory=dict)  # 例外 -> (捕捉する呼び出し箇所, 全呼び出し箇所)
    resilience: list[fa.Hint] = field(default_factory=list)
    risks: list[Finding] = field(default_factory=list)
    # 8. なぜ現在の実装になっているのか
    history: SymbolHistory | None = None
    limitations: list[str] = field(default_factory=list)


class UnderstandService:
    def __init__(self, navigation: NavigationService) -> None:
        self._navigation = navigation
        self._flow = FlowService(navigation)

    def understand(self, project: Project, index: ProjectIndex, symbol: Symbol, depth: int = 3) -> Understanding:
        source_file = index.files[symbol.file_id]
        path = source_file.relative_path
        description = DescribeService(self._navigation).describe(project, index, symbol)
        module = next((s for s in index.symbols.values() if s.file_id == symbol.file_id and s.kind == SymbolKind.MODULE), None)
        result = Understanding(
            symbol, path, source_file.language, symbol.summary, description.declaration,
            module.summary if module else "",
        )
        is_python = source_file.language == Language.PYTHON
        callable_symbol = symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)

        external = ExternalService()
        externals = external.report(index)
        # 入口の特定は、構文解析をしない軽い走査で。登録箇所の探索は、シンボル名を含むファイルだけを構文解析する。
        boundary = BoundaryService()
        light_items, _ = boundary.scan(project, index, light=True)
        named_items, _ = boundary.scan(project, index, contains=symbol.name) if is_python else ([], [])
        boundary_items = [*light_items, *named_items]
        entry_ids = {i.target.symbol_id for i in boundary_items if i.target and i.kind in ("entry", "cli", "http", "event", "thread")}

        # --- 1. なぜ存在するのか（手がかり） ---
        tests = TestMapService().tests_for(index, symbol, depth)
        result.tests = tests
        result.test_names = [r.test.qualified_name for r in tests.reaches if r.direct][:6]
        result.doc_mentions = self._doc_mentions(project, symbol)
        # --- 2. 誰が呼ぶのか ---
        callers = self._navigation.callers(index, symbol)
        result.caller_total = len(callers)
        result.callers = callers
        result.registrations = [i for i in boundary_items if i.target and i.target.symbol_id == symbol.symbol_id]
        # --- 6. 誰に影響するのか ---
        result.impact = ImpactService().impact(index, symbol, depth + 1, entry_ids)
        # --- 4（外部への副作用）・3（外部からの入力） ---
        if callable_symbol:
            result.effects = external.effects(index, externals, symbol, depth)
            result.external_inputs = [
                u
                for u in externals.uses
                if u.source_id == symbol.symbol_id and u.kind == "call" and u.operation in INPUT_OPERATIONS
                and u.category in _INPUT_CATEGORIES
            ]
            owner_qn = symbol.qualified_name
            config_items, _ = ConfigService().scan(project, index, only_paths={path}) if is_python else ([], [])
            result.config_reads = [c for c in config_items if c.owner == owner_qn and c.kind in ("env", "cli_option", "config_file")]
        # --- 8. なぜ現在の実装か ---
        result.history = HistoryService().symbol_history(project, index, symbol)

        if not is_python:
            result.limitations.append("入力・変更・戻り値・失敗時の挙動の解析は、現在はPythonのみ対応です（Cは呼び出し元・影響範囲・外部連携・履歴・テストのみ）。")
            return result
        if not callable_symbol:
            result.limitations.append(f"{symbol.kind.value} のため、入力・戻り値・失敗時の解析は対象外です（関数・メソッドのみ）。")
            return result

        try:
            self._fill_function_facts(project, index, symbol, result, depth)
        except FlowAnalysisError as exc:
            result.limitations.append(str(exc))
        return result

    # --- 関数のASTから得る事実 ---

    def _fill_function_facts(self, project: Project, index: ProjectIndex, symbol: Symbol, result: Understanding, depth: int) -> None:
        function = self._flow.function_ast(project, index, symbol)
        result.is_async = isinstance(function, ast.AsyncFunctionDef)
        result.parameters = _parameters(function)
        result.return_annotation = fa.unparse(function.returns, 60)
        result.returns = _returns(function)
        flow_summary = self._flow.control_flow(project, index, symbol)
        result.is_generator = flow_summary.metrics["yields"] > 0
        result.resilience = flow_summary.hints

        # 3. 呼び出し元が実際に渡す引数
        names = [p.name for p in result.parameters if p.name not in ("self", "cls") and p.kind in ("positional", "keyword-only")]
        for name in names:
            try:
                nodes = self._flow.upstream(project, index, symbol, name, depth=0)
            except FlowAnalysisError:
                continue
            distinct = list(dict.fromkeys(n.argument for n in nodes))
            if distinct:
                result.caller_arguments[name] = distinct

        # 4. 状態・引数の変更
        parent = index.symbols.get(symbol.parent_symbol_id) if symbol.parent_symbol_id else None
        if symbol.kind == SymbolKind.METHOD and parent is not None and parent.kind == SymbolKind.CLASS:
            for access in self._flow.class_state(project, index, parent):
                if access.method == symbol.name and access.mode in ("write", "mutate"):
                    verb = "書き込む" if access.mode == "write" else "破壊的に変更する"
                    result.state_changes.append(f"L{access.line} self.{access.attribute} を{verb}（オブジェクトの状態）")
        module_symbol = next((s for s in index.symbols.values() if s.file_id == symbol.file_id and s.kind == SymbolKind.MODULE), None)
        if module_symbol is not None:
            for write in self._flow.module_state(project, index, module_symbol):
                if write.function == symbol.name:
                    verb = "global宣言で再代入する" if write.mode == "global_assign" else "破壊的に変更する"
                    result.state_changes.append(f"L{write.line} モジュール変数 {write.name} を{verb}")
        result.parameter_mutations = _parameter_mutations(function, fa.analyze_variables(function))

        # 7. 失敗したときの挙動
        report = self._flow.exceptions(project, index, symbol, depth + 1)
        result.exceptions = report
        result.caught_by_callers = self._caught_by_callers(project, index, symbol, report, result.callers)
        findings, _ = RiskService().scan(project, index, only_paths={result.path})
        result.risks = [f for f in findings if symbol.start_line <= f.line <= symbol.end_line]
        result.todo_comments = self._todo_comments(project, index, symbol)

    def _caught_by_callers(self, project, index, symbol, report: ExceptionReport, callers: list[ReferenceHit]) -> dict[str, tuple[int, int]]:
        """伝播する例外ごとに、呼び出し元の呼び出し箇所のうち、tryで捕捉しているものの数を数える。"""

        bases = self._flow._exception_bases(index)
        summaries: dict[str, fa.ControlFlowSummary | None] = {}
        counts: dict[str, list[int]] = {}
        for exception in {e.exception for e in report.propagated}:
            counts[exception] = [0, 0]
        for hit in callers:
            caller = hit.source
            if caller.symbol_id not in summaries:
                try:
                    summaries[caller.symbol_id] = self._flow.control_flow(project, index, caller)
                except FlowAnalysisError:
                    summaries[caller.symbol_id] = None
            summary = summaries[caller.symbol_id]
            line = hit.reference.source_location.start_line
            guards = tuple(
                t for tr in (summary.tries if summary else []) if tr.start < line <= tr.end for group in tr.handler_types for t in group
            )
            for exception, pair in counts.items():
                pair[1] += 1
                if guards and fa.exception_caught(exception, guards, bases):
                    pair[0] += 1
        return {k: (v[0], v[1]) for k, v in counts.items()}

    def _todo_comments(self, project: Project, index: ProjectIndex, symbol: Symbol) -> list[tuple[int, str]]:
        try:
            lines = (project.root_path / index.path_of(symbol.file_id)).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return [
            (n, lines[n - 1].strip()[:80])
            for n in range(symbol.start_line, min(len(lines), symbol.end_line) + 1)
            if _TODO.search(lines[n - 1])
        ]

    def _doc_mentions(self, project: Project, symbol: Symbol, limit: int = 5) -> list[tuple[str, int, str]]:
        """プロジェクト内の文書（md/rst/txt/org）で、シンボル名に言及している箇所。"""

        name = symbol.name
        if len(name) < 4 or name in _GENERIC_NAMES or (name.startswith("__") and name.endswith("__")):
            return []
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
        found: list[tuple[str, int, str]] = []
        root = project.root_path
        for path in sorted(root.rglob("*")):
            if len(found) >= limit:
                break
            if path.suffix.lower() not in _DOC_SUFFIXES or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if any(part.startswith(".") or part in ("node_modules", "venv", "build", "dist") for part in path.relative_to(root).parts):
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                for number, text in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if pattern.search(text):
                        found.append((relative, number, text.strip()[:100]))
                        break
            except OSError:
                continue
        return found


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[Parameter]:
    args = function.args
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    parameters = [
        Parameter(a.arg, fa.unparse(a.annotation, 40), fa.unparse(d, 30), "positional") for a, d in zip(positional, defaults)
    ]
    if args.vararg:
        parameters.append(Parameter(args.vararg.arg, fa.unparse(args.vararg.annotation, 40), "", "*args"))
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append(Parameter(a.arg, fa.unparse(a.annotation, 40), fa.unparse(d, 30), "keyword-only"))
    if args.kwarg:
        parameters.append(Parameter(args.kwarg.arg, fa.unparse(args.kwarg.annotation, 40), "", "**kwargs"))
    return parameters


def _returns(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    found = []
    stack: list[ast.AST] = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return):
            found.append((node.lineno, fa.unparse(node.value, 60) if node.value is not None else "None"))
        stack.extend(ast.iter_child_nodes(node))
    return sorted(found)


def _parameter_mutations(function: ast.FunctionDef | ast.AsyncFunctionDef, variables) -> list[str]:
    """引数として受け取ったオブジェクトを、関数内で変更している箇所。"""

    params = {n for n, i in variables.items() if i.is_param and n not in ("self", "cls")}
    found: list[str] = []
    for name in sorted(params):
        for flow in variables[name].flows:
            if flow.kind == "method_call" and flow.target.rsplit(".", 1)[-1] in fa._MUTATING_METHODS:
                found.append(f"L{flow.line} 引数 {name} を {flow.target.rsplit('.', 1)[-1]}() で破壊的に変更する")
    for node in ast.walk(function):
        targets = node.targets if isinstance(node, ast.Assign) else ([node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else [])
        for target in targets:
            base = target.value if isinstance(target, (ast.Attribute, ast.Subscript)) else None
            if isinstance(base, ast.Name) and base.id in params:
                found.append(f"L{node.lineno} 引数 {base.id} の属性/要素に書き込む（{fa.unparse(target, 40)}）")
    return sorted(set(found), key=lambda text: int(text[1:].split(" ", 1)[0]))

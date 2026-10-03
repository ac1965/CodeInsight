from __future__ import annotations

import ast
import hashlib
from collections import defaultdict
from dataclasses import dataclass, field

from codeinsight.analysis import flow_analysis as fa
from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import (
    Language,
    Project,
    ReferenceKind,
    ResolutionStatus,
    Symbol,
    SymbolKind,
)

_CALLABLE = (SymbolKind.FUNCTION, SymbolKind.METHOD)


class FlowAnalysisError(Exception):
    """この解析を実行できない（対応言語外・ソースが解析後に変更されている等）。"""


@dataclass
class _Loaded:
    tree: ast.Module
    definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    lines: list[str]


@dataclass
class VariableTrace:
    """1つの変数について、定義・使用・伝播先を集めたもの（関数内＋引数を介した呼び出し先）。"""

    symbol: Symbol
    path: str
    name: str
    is_param: bool
    definitions: list[fa.Definition]
    uses: list[int]
    flows: list["TraceFlow"] = field(default_factory=list)


@dataclass
class TraceFlow:
    kind: str
    line: int
    description: str
    callee: Symbol | None = None
    callee_param: str | None = None
    status: str = ""  # 呼び出し先を特定できない理由・補足
    child: VariableTrace | None = None


@dataclass
class UpstreamNode:
    """引数の値がどこから渡されるか（呼び出し元の実引数）。"""

    caller: Symbol
    path: str
    line: int
    argument: str
    children: list["UpstreamNode"] = field(default_factory=list)


@dataclass
class PropagatedException:
    exception: str
    raised_in: Symbol
    raised_line: int
    chain: list[tuple[Symbol, int]]  # 呼び出し経路（このシンボルから、送出元へ向かう）


@dataclass
class ExceptionReport:
    symbol: Symbol
    path: str
    propagated: list[PropagatedException]
    caught_inside: list[tuple[int, str, tuple[str, ...]]]  # (行, 例外, 捕捉する型)
    handlers: list[fa.HandlerInfo]
    unresolved_calls: int  # 呼び出し先を特定できず、例外を追えない呼び出しの数


class FlowService:
    """関数・クラス単位の制御フロー・データフロー・状態変化・例外経路の解析（Python）。

    解析結果は、保存済みの解析結果（シンボルの位置・呼び出しの解決結果）と、現在のソースの
    ASTを組み合わせたもの。ソースが解析後に変更されている場合は、位置がずれて誤った結果に
    なるため実行しない。
    """

    def __init__(self, navigation: NavigationService) -> None:
        self._navigation = navigation

    # --- 読み込み ---

    def _load(self, project: Project, index: ProjectIndex, symbol: Symbol, cache: dict) -> _Loaded:
        source_file = index.files[symbol.file_id]
        if source_file.language != Language.PYTHON:
            raise FlowAnalysisError(
                f"{source_file.language.value} のシンボルは未対応です（この解析はPythonのみ対応）。"
            )
        if source_file.relative_path not in cache:
            path = project.root_path / source_file.relative_path
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise FlowAnalysisError(f"ファイルを読み込めません: {source_file.relative_path}: {exc}") from exc
            if hashlib.sha256(data).hexdigest() != source_file.content_hash:
                raise FlowAnalysisError(
                    f"{source_file.relative_path} は解析後に変更されています。"
                    "再解析（codeinsight analyze）してから実行してください。"
                )
            text = data.decode("utf-8", errors="replace")
            cache[source_file.relative_path] = ast.parse(text)
            cache[(source_file.relative_path, "lines")] = text.splitlines()
        tree = cache[source_file.relative_path]
        definition = fa.find_definition(tree, symbol.start_line)
        if definition is None or definition.name != symbol.name:
            raise FlowAnalysisError(f"{symbol.qualified_name} の定義をソースから特定できません。")
        return _Loaded(tree, definition, cache[(source_file.relative_path, "lines")])

    def _function(self, project, index, symbol, cache):
        if symbol.kind not in _CALLABLE:
            raise FlowAnalysisError(f"{symbol.qualified_name} は関数/メソッドではありません（{symbol.kind.value}）。")
        loaded = self._load(project, index, symbol, cache)
        assert isinstance(loaded.definition, (ast.FunctionDef, ast.AsyncFunctionDef))
        return loaded.definition

    # --- 制御フロー ---

    def control_flow(self, project: Project, index: ProjectIndex, symbol: Symbol) -> fa.ControlFlowSummary:
        cache: dict = {}
        function = self._function(project, index, symbol, cache)
        lines = cache[(index.path_of(symbol.file_id), "lines")]
        return fa.analyze_control_flow(function, lines)

    # --- データフロー（変数のライフサイクルと伝播） ---

    def variables(self, project: Project, index: ProjectIndex, symbol: Symbol) -> dict[str, fa.VariableInfo]:
        return fa.analyze_variables(self._function(project, index, symbol, {}))

    def trace_variable(
        self,
        project: Project,
        index: ProjectIndex,
        symbol: Symbol,
        variable: str,
        depth: int = 2,
    ) -> VariableTrace:
        """変数の値の行き先を、関数内の代入・呼び出し引数・戻り値・属性への書き込みでたどる。

        解決済みの呼び出しは、実引数に対応する呼び出し先の仮引数へ `depth` 段まで追う。
        流れ非依存（実行順序・条件を考慮しない）の近似で、値そのものは追わない。
        """

        cache: dict = {}
        calls_by_line = self._calls_by_line(index)
        return self._trace(project, index, symbol, variable, depth, cache, calls_by_line, set())

    def _calls_by_line(self, index: ProjectIndex) -> dict[tuple[str, int], list]:
        table: dict[tuple[str, int], list] = defaultdict(list)
        for reference in index.references:
            if reference.reference_kind == ReferenceKind.CALL:
                table[(reference.source_symbol_id, reference.source_location.start_line)].append(reference)
        return table

    def _trace(self, project, index, symbol, variable, depth, cache, calls_by_line, visited) -> VariableTrace:
        function = self._function(project, index, symbol, cache)
        variables = fa.analyze_variables(function)
        info = variables.get(variable)
        path = index.path_of(symbol.file_id)
        if info is None:
            raise FlowAnalysisError(f"{symbol.qualified_name} に変数 '{variable}' が見つかりません。")
        trace = VariableTrace(symbol, path, variable, info.is_param, info.definitions, sorted(set(info.uses)))
        visited = visited | {(symbol.symbol_id, variable)}
        for flow in sorted(info.flows, key=lambda f: (f.line, f.kind, f.target)):
            if flow.kind in ("copy", "derive"):
                child = None
                if (symbol.symbol_id, flow.target) not in visited and depth > 0:
                    child = self._trace(project, index, symbol, flow.target, depth, cache, calls_by_line, visited)
                verb = "そのまま代入" if flow.kind == "copy" else "式に含めて代入"
                trace.flows.append(TraceFlow(flow.kind, flow.line, f"{flow.target} へ{verb}", child=child))
            elif flow.kind == "call_arg":
                trace.flows.append(self._call_flow(project, index, symbol, flow, depth, cache, calls_by_line, visited))
            elif flow.kind == "return":
                trace.flows.append(TraceFlow("return", flow.line, "戻り値として返す"))
            elif flow.kind == "yield":
                trace.flows.append(TraceFlow("yield", flow.line, "yield する"))
            elif flow.kind == "attr_store":
                trace.flows.append(TraceFlow("attr_store", flow.line, f"{flow.target} に書き込む（オブジェクトの状態）"))
            elif flow.kind == "subscript_store":
                trace.flows.append(TraceFlow("subscript_store", flow.line, f"{flow.target} に書き込む"))
            elif flow.kind == "method_call":
                trace.flows.append(TraceFlow("method_call", flow.line, f"{flow.target}() を呼ぶ"))
        return trace

    def _call_flow(self, project, index, symbol, flow, depth, cache, calls_by_line, visited) -> TraceFlow:
        label = f"{flow.call_text} の" + (
            f"キーワード引数 {flow.keyword}" if flow.keyword else f"第{(flow.position or 0) + 1}引数"
        )
        if flow.starred:
            return TraceFlow("call_arg", flow.line, f"{label}（*展開のため仮引数を特定できない）", status="未解決")
        candidates = [
            r for r in calls_by_line.get((symbol.symbol_id, flow.call_line), []) if r.target_name == flow.target
        ] or calls_by_line.get((symbol.symbol_id, flow.call_line), [])
        reference = candidates[0] if len(candidates) == 1 else next(
            (r for r in candidates if r.target_name == flow.target), None
        )
        if reference is None or reference.resolution_status != ResolutionStatus.RESOLVED or not reference.target_symbol_id:
            status = "外部" if reference and reference.resolution_status == ResolutionStatus.EXTERNAL else "呼び出し先を静的に特定できない"
            return TraceFlow("call_arg", flow.line, f"{label}として渡す", status=status)
        callee = index.symbols[reference.target_symbol_id]
        target = self._constructor_or_self(index, callee)
        if target is None:
            return TraceFlow("call_arg", flow.line, f"{label}として渡す", callee=callee, status="仮引数を特定できない")
        try:
            function = self._function(project, index, target, cache)
        except FlowAnalysisError as exc:
            return TraceFlow("call_arg", flow.line, f"{label}として渡す", callee=callee, status=str(exc))
        parameters = fa.parameter_names(function)
        offset = 1 if target.kind == SymbolKind.METHOD and parameters[:1] in (["self"], ["cls"]) and "." in flow.target else 0
        if flow.keyword:
            parameter = flow.keyword if flow.keyword in parameters else None
        else:
            position = (flow.position or 0) + offset
            parameter = parameters[position] if position < len(parameters) else None
        if parameter is None:
            return TraceFlow("call_arg", flow.line, f"{label}として渡す", callee=target, status="対応する仮引数が見つからない（可変長引数など）")
        child = None
        if depth > 0 and (target.symbol_id, parameter) not in visited:
            child = self._trace(project, index, target, parameter, depth - 1, cache, calls_by_line, visited)
        return TraceFlow("call_arg", flow.line, f"{label}として渡す → {target.qualified_name} の仮引数 {parameter}", callee=target, callee_param=parameter, child=child)

    def _constructor_or_self(self, index: ProjectIndex, callee: Symbol) -> Symbol | None:
        """呼び出し先の関数。クラスの場合は __init__ メソッドに対応付ける。"""

        if callee.kind in _CALLABLE:
            return callee
        if callee.kind == SymbolKind.CLASS:
            for symbol in index.symbols.values():
                if symbol.qualified_name == f"{callee.qualified_name}.__init__" and symbol.kind == SymbolKind.METHOD:
                    return symbol
        return None

    def upstream(
        self,
        project: Project,
        index: ProjectIndex,
        symbol: Symbol,
        parameter: str,
        depth: int = 2,
    ) -> list[UpstreamNode]:
        """仮引数に渡される実引数を、呼び出し元の呼び出し箇所から調べる（解決済みの呼び出しのみ）。"""

        cache: dict = {}
        return self._upstream(project, index, symbol, parameter, depth, cache, set())

    def _upstream(self, project, index, symbol, parameter, depth, cache, visited) -> list[UpstreamNode]:
        function = self._function(project, index, symbol, cache)
        parameters = fa.parameter_names(function)
        if parameter not in parameters:
            raise FlowAnalysisError(f"{symbol.qualified_name} に引数 '{parameter}' がありません。")
        nodes: list[UpstreamNode] = []
        offset_method = symbol.kind == SymbolKind.METHOD and parameters[:1] in (["self"], ["cls"])
        for hit in self._navigation.callers(index, symbol):
            caller = hit.source
            try:
                caller_function = self._function(project, index, caller, cache)
            except FlowAnalysisError:
                continue
            line = hit.reference.source_location.start_line
            for call in [n for n in ast.walk(caller_function) if isinstance(n, ast.Call) and n.lineno == line]:
                if fa.unparse(call.func, 60).rsplit(".", 1)[-1] != hit.reference.target_name.rsplit(".", 1)[-1]:
                    continue
                argument = self._argument_for(call, parameters, parameter, offset_method and isinstance(call.func, ast.Attribute))
                if argument is None:
                    continue
                node = UpstreamNode(caller, hit.path, line, fa.unparse(argument, 70))
                if (
                    depth > 0
                    and isinstance(argument, ast.Name)
                    and argument.id in fa.parameter_names(caller_function)
                    and (caller.symbol_id, argument.id) not in visited
                ):
                    node.children = self._upstream(project, index, caller, argument.id, depth - 1, cache, visited | {(symbol.symbol_id, parameter)})
                nodes.append(node)
        return nodes

    @staticmethod
    def _argument_for(call: ast.Call, parameters: list[str], parameter: str, skip_self: bool) -> ast.expr | None:
        for keyword in call.keywords:
            if keyword.arg == parameter:
                return keyword.value
        position = parameters.index(parameter) - (1 if skip_self else 0)
        if 0 <= position < len(call.args) and not any(isinstance(a, ast.Starred) for a in call.args[: position + 1]):
            return call.args[position]
        return None

    # --- 状態の変化 ---

    def class_state(self, project: Project, index: ProjectIndex, symbol: Symbol) -> list[fa.StateAccess]:
        loaded = self._load(project, index, symbol, {})
        if not isinstance(loaded.definition, ast.ClassDef):
            raise FlowAnalysisError(f"{symbol.qualified_name} はクラスではありません（{symbol.kind.value}）。")
        return fa.analyze_class_state(loaded.definition)

    def module_state(self, project: Project, index: ProjectIndex, symbol: Symbol) -> list[fa.GlobalWrite]:
        """モジュール変数を、関数内から書き換える箇所（Python）。"""

        source_file = index.files[symbol.file_id]
        if source_file.language != Language.PYTHON:
            raise FlowAnalysisError("モジュールの状態解析はPythonのみ対応です。")
        data = self._read_fresh(project, source_file)
        return fa.analyze_module_state(ast.parse(data.decode("utf-8", errors="replace")))

    @staticmethod
    def _read_fresh(project: Project, source_file) -> bytes:
        try:
            data = (project.root_path / source_file.relative_path).read_bytes()
        except OSError as exc:
            raise FlowAnalysisError(f"ファイルを読み込めません: {source_file.relative_path}: {exc}") from exc
        if hashlib.sha256(data).hexdigest() != source_file.content_hash:
            raise FlowAnalysisError(f"{source_file.relative_path} は解析後に変更されています。再解析してください。")
        return data

    # --- 例外の経路 ---

    def exceptions(self, project: Project, index: ProjectIndex, symbol: Symbol, depth: int = 4) -> ExceptionReport:
        """関数から呼び出し元へ出うる例外を、明示的なraiseと解決済みの呼び出しからたどる。

        例外を送出しうるのは明示的な `raise` のみで、組み込み関数・外部ライブラリが送出する例外は
        含まない。呼び出し先を特定できない呼び出しの数を併せて示す。
        """

        cache: dict = {}
        calls_by_line = self._calls_by_line(index)
        bases = self._exception_bases(index)
        memo: dict[str, list[PropagatedException]] = {}
        unresolved = {"count": 0}
        propagated = self._propagate(project, index, symbol, depth, cache, calls_by_line, bases, memo, set(), unresolved)
        summary = fa.analyze_control_flow(self._function(project, index, symbol, cache))
        inside = [
            (r.line, r.exception, tuple(t for group in r.handlers for t in group))
            for r in summary.raises
            if r.handlers and fa.exception_caught(r.exception, tuple(t for g in r.handlers for t in g), bases)
        ]
        return ExceptionReport(
            symbol, index.path_of(symbol.file_id), propagated, inside, summary.handlers, unresolved["count"]
        )

    def _exception_bases(self, index: ProjectIndex):
        """プロジェクト内の例外クラスの基底クラス名（推移的）を返す関数。"""

        direct: dict[str, set[str]] = defaultdict(set)
        for symbol in index.symbols.values():
            if symbol.kind == SymbolKind.CLASS:
                direct[symbol.name].update(b.rsplit(".", 1)[-1] for b in symbol.base_classes)

        def bases_of(name: str) -> set[str]:
            seen: set[str] = set()
            stack = [name.rsplit(".", 1)[-1]]
            while stack:
                current = stack.pop()
                for base in direct.get(current, ()):
                    if base not in seen:
                        seen.add(base)
                        stack.append(base)
            return seen

        return bases_of

    def _propagate(self, project, index, symbol, depth, cache, calls_by_line, bases, memo, active, unresolved):
        if symbol.symbol_id in memo:
            return memo[symbol.symbol_id]
        if symbol.symbol_id in active:
            return []
        try:
            function = self._function(project, index, symbol, cache)
        except FlowAnalysisError:
            return []
        summary = fa.analyze_control_flow(function)
        result: list[PropagatedException] = []
        for raised in summary.raises:
            if raised.exception == "<再送出>":
                continue
            if raised.handlers and fa.exception_caught(raised.exception, tuple(t for g in raised.handlers for t in g), bases):
                continue
            result.append(PropagatedException(raised.exception, symbol, raised.line, []))
        if depth > 0:
            for (source_id, line), references in calls_by_line.items():
                if source_id != symbol.symbol_id:
                    continue
                for reference in references:
                    if reference.resolution_status != ResolutionStatus.RESOLVED or not reference.target_symbol_id:
                        if reference.resolution_status in (ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS):
                            unresolved["count"] += 1
                        continue
                    callee = index.symbols.get(reference.target_symbol_id)
                    if callee is None or callee.kind not in _CALLABLE:
                        continue
                    inner = self._propagate(project, index, callee, depth - 1, cache, calls_by_line, bases, memo, active | {symbol.symbol_id}, unresolved)
                    guards = [t for tr in summary.tries if tr.start < line <= tr.end for group in tr.handler_types for t in group]
                    for exc in inner:
                        if guards and fa.exception_caught(exc.exception, tuple(guards), bases):
                            continue
                        result.append(PropagatedException(exc.exception, exc.raised_in, exc.raised_line, [(callee, line), *exc.chain]))
        unique = {(e.exception, e.raised_in.symbol_id, e.raised_line, tuple(s.symbol_id for s, _ in e.chain)): e for e in result}
        memo[symbol.symbol_id] = list(unique.values())
        return memo[symbol.symbol_id]

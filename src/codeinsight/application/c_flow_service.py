"""C言語の関数単位の制御フロー・データフロー・状態・終了経路・リスク。

保存済みの解析結果（シンボルの位置・呼び出しの解決結果）と、現在のソースのClang ASTを組み合わせる。
解析時と同じコンパイル設定（プロジェクトに保存した compile_commands.json の場所）で再度構文解析する。
ソースが解析後に変更されている場合は、位置がずれて誤った結果になるため実行しない。
"""

from __future__ import annotations

import functools
import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

import clang.cindex as ci

from codeinsight.analysis import c_flow_analysis as cf
from codeinsight.analysis import flow_analysis as fa
from codeinsight.analysis.c_analyzer import CAnalyzer, CppAnalyzer
from codeinsight.analysis.language_adapter import FileAnalysis, SourceUnit
from codeinsight.application.flow_service import FlowAnalysisError, TraceFlow, UpstreamNode, VariableTrace
from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.risk_service import RULES, Finding
from codeinsight.domain import Language, Project, ReferenceKind, ResolutionStatus, Symbol, SymbolKind

_CALLABLE = (SymbolKind.FUNCTION, SymbolKind.METHOD)
C_FAMILY = (Language.C, Language.CPP)  # Clang AST で関数単位の解析ができる言語（C++ は、C と同じ近似に、try/catch/throw・範囲for・this 経由の状態を加える）


_UNKNOWN_AST = "このlibclangが認識できない種類のASTノードがあり、解析できません（libclangのバージョン差）"


def _guarded(method):
    """libclangのPythonバインディングが知らないAST（バージョン差）で ValueError になる場合を、解析できなかったこととして扱う。"""

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ValueError as exc:
            raise FlowAnalysisError(f"{_UNKNOWN_AST}: {exc}") from exc

    return wrapper


@dataclass(frozen=True)
class FieldUse:
    path: str
    line: int
    function: str  # 含む関数の修飾名（関数の外なら空）
    owner: str
    mode: str
    text: str


@dataclass
class FieldReport:
    uses: list[FieldUse] = field(default_factory=list)
    owners: set[str] = field(default_factory=set)  # その名前のフィールドを持つ構造体・クラス
    scanned: int = 0  # 構文解析して調べたファイル（名前が現れるもの）
    skipped: list[str] = field(default_factory=list)  # 解析後に変更された・構文解析できず、調べられなかったファイル


@dataclass(frozen=True)
class PropagatedExit:
    """呼び出し先を経由して終了しうる経路（静的な呼び出しの連鎖。条件によっては実際には通らない）。"""

    kind: str  # terminate / longjmp
    detail: str  # 例: exit()
    origin: Symbol  # 終了を直接呼ぶ関数
    line: int  # origin 内の行
    chain: tuple[tuple[Symbol, int], ...]  # 呼び出し元から順に (呼び出し先, 直前の関数内の呼び出し行)


@dataclass
class ExitReport:
    direct: list[cf.CExit]
    propagated: list[PropagatedExit] = field(default_factory=list)
    unresolved_calls: int = 0  # 関数ポインタなど、呼び出し先を特定できない呼び出しの数（そこからの終了は追えていない）
    skipped: list[str] = field(default_factory=list)  # 解析できず、終了の有無を確認できなかった呼び出し先
    truncated: bool = False  # 深さの上限で打ち切った


_PROPAGATING = ("terminate", "longjmp", "throw")  # assert は NDEBUG で消えるため、伝播させない。throw は、呼び出し先の同じ関数の catch で捕捉されるものを除く


class CFlowService:
    def __init__(self, navigation: NavigationService | None) -> None:  # 構文解析だけを使う用途（設定値・境界の走査）では、None でよい
        self._nav = navigation
        self._analyzers: dict[tuple[Language, str | None], CAnalyzer] = {}
        self._parsed: dict[tuple[str, str], tuple] = {}

    @property
    def _navigation(self) -> NavigationService:
        if self._nav is None:
            raise FlowAnalysisError("この操作には、呼び出し関係（NavigationService）が必要です（構文解析だけの用途では使えません）。")
        return self._nav

    # --- 読み込み ---

    def _analyzer(self, project: Project, language: Language = Language.C) -> CAnalyzer:
        directory = project.configuration.compile_commands_dir
        key = (language, directory)
        if key not in self._analyzers:
            analyzer_class = CppAnalyzer if language == Language.CPP else CAnalyzer
            self._analyzers[key] = analyzer_class(compile_commands_dir=Path(directory) if directory else None)
        return self._analyzers[key]

    def _translation_unit(self, project: Project, index: ProjectIndex, symbol: Symbol):
        source_file = index.files[symbol.file_id]
        if source_file.language not in C_FAMILY:
            raise FlowAnalysisError(f"{source_file.language.value} のシンボルは、C・C++の解析の対象ではありません。")
        return self._parse_file(project, source_file, symbol.file_id)

    def _parse_file(self, project: Project, source_file, file_id: str):
        key = (project.project_id, source_file.relative_path)
        if key not in self._parsed:
            absolute = project.root_path / source_file.relative_path
            try:
                data = absolute.read_bytes()
            except OSError as exc:
                raise FlowAnalysisError(f"ファイルを読み込めません: {source_file.relative_path}: {exc}") from exc
            if hashlib.sha256(data).hexdigest() != source_file.content_hash:
                raise FlowAnalysisError(
                    f"{source_file.relative_path} は解析後に変更されています。再解析（codeinsight analyze）してから実行してください。"
                )
            unit = SourceUnit(file_id, absolute, source_file.relative_path, data)
            result = FileAnalysis()
            translation_unit = self._analyzer(project, source_file.language).parse(unit, result)
            if translation_unit is None:
                reason = result.errors[0] if result.errors else "原因不明"
                raise FlowAnalysisError(f"{source_file.relative_path} を構文解析できません（{reason}）。コンパイル設定が必要な場合は analyze --compile-commands を使ってください。")
            self._parsed[key] = (translation_unit, data, str(absolute.resolve()))
        return self._parsed[key]

    def load(self, project: Project, index: ProjectIndex, symbol: Symbol) -> cf.CFunction:
        if symbol.kind not in _CALLABLE:
            raise FlowAnalysisError(f"{symbol.qualified_name} は関数ではありません（{symbol.kind.value}）。")
        translation_unit, data, absolute = self._translation_unit(project, index, symbol)
        function = cf.find_function(translation_unit, absolute, symbol.name, symbol.start_line, data)
        if function is None:
            raise FlowAnalysisError(f"{symbol.qualified_name} の定義をソースから特定できません（条件付きコンパイルで除外されている可能性があります）。")
        return function

    # --- 制御フロー・データフロー・状態・終了経路 ---

    @_guarded
    def control_flow(self, project: Project, index: ProjectIndex, symbol: Symbol) -> fa.ControlFlowSummary:
        return cf.analyze_control_flow(self.load(project, index, symbol))

    @_guarded
    def return_values(self, project: Project, index: ProjectIndex, symbol: Symbol) -> tuple[list[int], bool]:
        return cf.analyze_return_values(self.load(project, index, symbol))

    @_guarded
    def macros(self, project: Project, index: ProjectIndex, symbol: Symbol) -> list[cf.CMacroUse]:
        return cf.macros_used(self.load(project, index, symbol))

    @_guarded
    def variables(self, project: Project, index: ProjectIndex, symbol: Symbol) -> dict[str, cf.CVariable]:
        return cf.analyze_variables(self.load(project, index, symbol))

    @_guarded
    def state(self, project: Project, index: ProjectIndex, symbol: Symbol) -> cf.CState:
        return cf.analyze_state(self.load(project, index, symbol))

    @_guarded
    def exits(self, project: Project, index: ProjectIndex, symbol: Symbol) -> list[cf.CExit]:
        return cf.analyze_exits(self.load(project, index, symbol))

    @_guarded
    def exit_report(self, project: Project, index: ProjectIndex, symbol: Symbol, depth: int = 4) -> ExitReport:
        """この関数の終了・失敗の経路と、解決済みの呼び出しを `depth` 段たどって終了に至る経路。

        幅優先で、各関数を1度だけ調べる（再帰・相互再帰で止まる）。関数ごとに最短の連鎖を1つ示す。
        """

        report = ExitReport(self.exits(project, index, symbol))
        visited = {symbol.symbol_id}
        queue: deque[tuple[Symbol, tuple[tuple[Symbol, int], ...], int]] = deque([(symbol, (), 0)])
        while queue:
            current, chain, level = queue.popleft()
            for hit in self._navigation.callees(index, current):
                reference = hit.reference
                if reference.reference_kind != ReferenceKind.CALL:
                    continue
                if hit.target is None:
                    if reference.resolution_status != ResolutionStatus.EXTERNAL:
                        report.unresolved_calls += 1  # 関数ポインタ・曖昧など。そこからの終了は追えない
                    continue
                callee = hit.target
                source_file = index.files.get(callee.file_id)
                if callee.symbol_id in visited or source_file is None or source_file.language not in C_FAMILY or callee.kind not in _CALLABLE:
                    continue
                visited.add(callee.symbol_id)
                step = (*chain, (callee, reference.source_location.start_line))
                try:
                    callee_exits = self.exits(project, index, callee)
                except FlowAnalysisError:
                    report.skipped.append(callee.qualified_name)
                    continue
                for item in callee_exits:
                    if item.kind in _PROPAGATING and not item.caught_locally:
                        report.propagated.append(PropagatedExit(item.kind, item.detail, callee, item.line, step))
                if level + 1 < depth:
                    queue.append((callee, step, level + 1))
                elif any(
                    h.reference.reference_kind == ReferenceKind.CALL and h.target is not None and h.target.symbol_id not in visited
                    for h in self._navigation.callees(index, callee)
                ):
                    report.truncated = True  # 深さの上限の先に、まだ調べていない呼び出し先がある
        return report

    @_guarded
    def facts(self, project: Project, index: ProjectIndex, symbol: Symbol) -> dict:
        function = self.load(project, index, symbol)
        return {
            "parameters": cf.parameters(function), "return_type": cf.return_type(function),
            "returns": cf.return_statements(function), "environment": cf.environment_reads(function),
        }

    # --- フィールド単位の読み書き（プロジェクト全体） ---

    def field_uses(self, project: Project, index: ProjectIndex, field_name: str, owner: str | None = None) -> FieldReport:
        """構造体・クラスのフィールドを、プロジェクト全体で、読み書きしている箇所を、含む関数とともに示す。

        名前が（単語として）現れるC/C++のファイルだけを構文解析する。マクロの定義の中だけでフィールドが使われ、使う側のファイルに名前が
        現れない場合は、見つけられない。位置による初期化（`{a, b}`）は、どのフィールドに入るかを追わない。
        """

        import re

        report = FieldReport()
        word = re.compile(rb"\b" + re.escape(field_name.encode("utf-8")) + rb"\b")
        callables_by_file: dict[str, list[Symbol]] = defaultdict(list)
        for symbol in index.symbols.values():
            if symbol.kind in _CALLABLE:
                callables_by_file[symbol.file_id].append(symbol)
        for file_id, source_file in sorted(index.files.items(), key=lambda item: item[1].relative_path):
            if source_file.language not in C_FAMILY:
                continue
            try:
                data = (project.root_path / source_file.relative_path).read_bytes()
            except OSError:
                report.skipped.append(f"{source_file.relative_path}（読み込めない）")
                continue
            if not word.search(data):
                continue
            try:
                translation_unit, source, absolute = self._parse_file(project, source_file, file_id)
                accesses = cf.field_accesses(translation_unit, absolute, source, field_name, owner)
            except (FlowAnalysisError, ValueError) as exc:
                report.skipped.append(f"{source_file.relative_path}（{str(exc).split('。')[0][:60]}）")
                continue
            report.scanned += 1
            for access in accesses:
                inside = [s for s in callables_by_file.get(file_id, []) if s.start_line <= access.line <= s.end_line]
                function = min(inside, key=lambda s: s.end_line - s.start_line).qualified_name if inside else ""
                report.uses.append(FieldUse(source_file.relative_path, access.line, function, access.owner, access.mode, access.text))
                report.owners.add(access.owner)
        return report

    # --- 引数の実引数（呼び出し元から渡される値の式） ---

    @_guarded
    def upstream(self, project: Project, index: ProjectIndex, symbol: Symbol, parameter: str, depth: int = 2) -> list[UpstreamNode]:
        """仮引数に渡される実引数を、呼び出し元の呼び出し箇所から調べる（解決済みの呼び出しのみ。実引数の式のソース上の文字）。

        実引数が、呼び出し元自身の引数そのものなら、`depth` 段まで、その呼び出し元の呼び出し元へたどる。関数ポインタ経由・
        曖昧な呼び出しは、呼び出し元として確定できないため含まれない。可変引数（`...`）は、位置で対応づけられない。
        """

        return self._upstream(project, index, symbol, parameter, depth, set())

    def _upstream(self, project, index, symbol, parameter, depth, visited) -> list[UpstreamNode]:
        names = [p.name for p in cf.parameters(self.load(project, index, symbol))]
        if parameter not in names:
            raise FlowAnalysisError(f"{symbol.qualified_name} に引数 '{parameter}' がありません。")
        position = names.index(parameter)
        nodes: list[UpstreamNode] = []
        for hit in self._navigation.callers(index, symbol):
            caller = hit.source
            source_file = index.files.get(caller.file_id)
            if source_file is None or source_file.language not in C_FAMILY or caller.kind not in _CALLABLE:
                continue
            try:
                caller_function = self.load(project, index, caller)
            except FlowAnalysisError:
                continue
            line = hit.reference.source_location.start_line
            for call in caller_function.cursor.walk_preorder():
                if call.kind != ci.CursorKind.CALL_EXPR or call.extent.start.line != line or cf.call_name(call) != symbol.name:
                    continue
                arguments = cf._call_arguments(call)
                if position >= len(arguments):
                    continue  # 可変引数・既定の引数など、位置で対応づけられない
                argument = arguments[position]
                node = UpstreamNode(caller, hit.path, line, cf.text(caller_function, argument, 70))
                inner = cf._unwrap(argument)
                caller_names = [p.name for p in cf.parameters(caller_function)]
                if depth > 0 and inner.kind == ci.CursorKind.DECL_REF_EXPR and inner.spelling in caller_names and (caller.symbol_id, inner.spelling) not in visited:
                    node.children = self._upstream(project, index, caller, inner.spelling, depth - 1, visited | {(symbol.symbol_id, parameter)})
                nodes.append(node)
        return nodes

    # --- 変数の追跡（引数を介した呼び出し先まで） ---

    @_guarded
    def trace_variable(self, project: Project, index: ProjectIndex, symbol: Symbol, variable: str, depth: int = 2) -> VariableTrace:
        calls: dict[tuple[str, int], list] = defaultdict(list)
        for reference in index.references:
            if reference.reference_kind == ReferenceKind.CALL:
                calls[(reference.source_symbol_id, reference.source_location.start_line)].append(reference)
        return self._trace(project, index, symbol, variable, depth, calls, set())

    def _trace(self, project, index, symbol, variable, depth, calls, visited) -> VariableTrace:
        variables = self.variables(project, index, symbol)
        info = variables.get(variable)
        if info is None:
            raise FlowAnalysisError(f"{symbol.qualified_name} に変数 '{variable}' が見つかりません。")
        role = {"param": "引数", "local": "ローカル変数", "global": "グローバル変数", "static": "静的変数"}[info.scope]
        trace = VariableTrace(symbol, index.path_of(symbol.file_id), variable, info.is_param, info.definitions, info.uses)
        trace.name = f"{variable}（{role}）" if not info.is_param else variable
        visited = visited | {(symbol.symbol_id, variable)}
        for flow in sorted(info.flows, key=lambda f: (f.line, f.kind, f.target)):
            if flow.kind in ("copy", "derive"):
                child = None
                if (symbol.symbol_id, flow.target) not in visited and depth > 0 and flow.target in variables:
                    child = self._trace(project, index, symbol, flow.target, depth, calls, visited)
                verb = "そのまま代入" if flow.kind == "copy" else "式に含めて代入"
                trace.flows.append(TraceFlow(flow.kind, flow.line, f"{flow.target} へ{verb}", child=child))
            elif flow.kind == "call_arg":
                trace.flows.append(self._call_flow(project, index, symbol, flow, depth, calls, visited))
            elif flow.kind == "return":
                trace.flows.append(TraceFlow("return", flow.line, "戻り値として返す"))
            elif flow.kind == "attr_store":
                trace.flows.append(TraceFlow("attr_store", flow.line, f"{flow.target} に書き込む（構造体のフィールド。書き込み先の実体は追えない）"))
            elif flow.kind == "subscript_store":
                trace.flows.append(TraceFlow("subscript_store", flow.line, f"{flow.target} に書き込む（配列の要素）"))
            elif flow.kind == "deref_store":
                trace.flows.append(TraceFlow("deref_store", flow.line, f"{flow.target} に書き込む（ポインタ経由。書き込み先の実体は追えない）", status="エイリアスは追えない"))
        return trace

    def _call_flow(self, project, index, symbol, flow, depth, calls, visited) -> TraceFlow:
        label = f"{flow.call_text} の第{(flow.position or 0) + 1}引数"
        candidates = [r for r in calls.get((symbol.symbol_id, flow.call_line), []) if r.target_name == flow.target]
        reference = candidates[0] if len(candidates) == 1 else None
        if reference is None or reference.resolution_status != ResolutionStatus.RESOLVED or not reference.target_symbol_id:
            if reference is not None and reference.resolution_status == ResolutionStatus.EXTERNAL:
                status = "外部（ライブラリ・システム関数）"
            elif reference is not None:
                status = "呼び出し先を静的に特定できない（関数ポインタ・マクロなど）"
            else:
                status = "外部（ライブラリ・システム関数）または特定できない"
            return TraceFlow("call_arg", flow.line, f"{label}として渡す", status=status)
        callee = index.symbols[reference.target_symbol_id]
        try:
            function = self.load(project, index, callee)
        except FlowAnalysisError as exc:
            return TraceFlow("call_arg", flow.line, f"{label}として渡す", callee=callee, status=str(exc))
        names = [p.name for p in cf.parameters(function)]
        position = flow.position or 0
        parameter = names[position] if position < len(names) else None
        if parameter is None:
            return TraceFlow("call_arg", flow.line, f"{label}として渡す", callee=callee, status="対応する仮引数が見つからない（可変長引数など）")
        child = None
        if depth > 0 and (callee.symbol_id, parameter) not in visited:
            child = self._trace(project, index, callee, parameter, depth - 1, calls, visited)
        return TraceFlow("call_arg", flow.line, f"{label}として渡す → {callee.qualified_name} の仮引数 {parameter}", callee=callee, callee_param=parameter, child=child)

    # --- リスク ---

    def scan_risks(self, project: Project, index: ProjectIndex, only_paths: set[str] | None = None) -> tuple[list[Finding], list[str]]:
        """プロジェクト内のCの関数のリスクの手がかり。構文解析できなかったファイルは、第2の戻り値に記録する（正常として扱わない）。"""

        findings: list[Finding] = []
        skipped: list[str] = []
        by_file: dict[str, list[Symbol]] = defaultdict(list)
        for symbol in index.symbols.values():
            source_file = index.files.get(symbol.file_id)
            if source_file is None or source_file.language not in C_FAMILY or symbol.kind not in _CALLABLE:
                continue
            if only_paths is not None and source_file.relative_path not in only_paths:
                continue
            by_file[source_file.relative_path].append(symbol)
        for path in sorted(by_file):
            for symbol in sorted(by_file[path], key=lambda s: s.start_line):
                try:
                    function = self.load(project, index, symbol)
                    hits = cf.scan_risks(function)
                except (FlowAnalysisError, ValueError):  # 解析できなかったファイルは、検査済みとして扱わず記録する
                    if path not in skipped:
                        skipped.append(path)
                    break
                for hit in hits:
                    severity = RULES[hit.rule][0]
                    findings.append(Finding(hit.rule, severity, path, hit.line, symbol.qualified_name, hit.detail))
        return findings, skipped

"""C言語の関数単位の制御フロー・データフロー・状態・終了経路・リスク。

保存済みの解析結果（シンボルの位置・呼び出しの解決結果）と、現在のソースのClang ASTを組み合わせる。
解析時と同じコンパイル設定（プロジェクトに保存した compile_commands.json の場所）で再度構文解析する。
ソースが解析後に変更されている場合は、位置がずれて誤った結果になるため実行しない。
"""

from __future__ import annotations

import functools
import hashlib
from collections import defaultdict
from pathlib import Path

from codeinsight.analysis import c_flow_analysis as cf
from codeinsight.analysis import flow_analysis as fa
from codeinsight.analysis.c_analyzer import CAnalyzer
from codeinsight.analysis.language_adapter import FileAnalysis, SourceUnit
from codeinsight.application.flow_service import FlowAnalysisError, TraceFlow, VariableTrace
from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.risk_service import RULES, Finding
from codeinsight.domain import Language, Project, ReferenceKind, ResolutionStatus, Symbol, SymbolKind

_CALLABLE = (SymbolKind.FUNCTION,)


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


class CFlowService:
    def __init__(self, navigation: NavigationService) -> None:
        self._navigation = navigation
        self._analyzers: dict[str | None, CAnalyzer] = {}
        self._parsed: dict[tuple[str, str], tuple] = {}

    # --- 読み込み ---

    def _analyzer(self, project: Project) -> CAnalyzer:
        directory = project.configuration.compile_commands_dir
        if directory not in self._analyzers:
            self._analyzers[directory] = CAnalyzer(compile_commands_dir=Path(directory) if directory else None)
        return self._analyzers[directory]

    def _translation_unit(self, project: Project, index: ProjectIndex, symbol: Symbol):
        source_file = index.files[symbol.file_id]
        if source_file.language != Language.C:
            raise FlowAnalysisError(f"{source_file.language.value} のシンボルは、C言語の解析の対象ではありません。")
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
            unit = SourceUnit(symbol.file_id, absolute, source_file.relative_path, data)
            result = FileAnalysis()
            translation_unit = self._analyzer(project).parse(unit, result)
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
    def variables(self, project: Project, index: ProjectIndex, symbol: Symbol) -> dict[str, cf.CVariable]:
        return cf.analyze_variables(self.load(project, index, symbol))

    @_guarded
    def state(self, project: Project, index: ProjectIndex, symbol: Symbol) -> cf.CState:
        return cf.analyze_state(self.load(project, index, symbol))

    @_guarded
    def exits(self, project: Project, index: ProjectIndex, symbol: Symbol) -> list[cf.CExit]:
        return cf.analyze_exits(self.load(project, index, symbol))

    @_guarded
    def facts(self, project: Project, index: ProjectIndex, symbol: Symbol) -> dict:
        function = self.load(project, index, symbol)
        return {
            "parameters": cf.parameters(function), "return_type": cf.return_type(function),
            "returns": cf.return_statements(function), "environment": cf.environment_reads(function),
        }

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
            if source_file is None or source_file.language != Language.C or symbol.kind not in _CALLABLE:
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

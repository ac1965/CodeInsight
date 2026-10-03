"""Emacs Lispの関数単位の制御フロー・例外（シグナル）・状態。

保存済みの解析結果（シンボルの位置・呼び出しの解決結果）と、現在のソースのS式を組み合わせる。
ソースが解析後に変更されている場合は、位置がずれて誤った結果になるため実行しない。
"""

from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass, field

from codeinsight.analysis import elisp_dataflow_analysis as ed
from codeinsight.analysis import elisp_flow_analysis as ef
from codeinsight.analysis import flow_analysis as fa
from codeinsight.analysis.elisp_analyzer import ElispSyntaxError, Form, _decode, org_elisp_blocks, read_forms
from codeinsight.analysis.language_adapter import FileAnalysis
from codeinsight.application.flow_service import FlowAnalysisError, TraceFlow, VariableTrace
from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.risk_service import RULES, Finding
from codeinsight.domain import Language, Project, ReferenceKind, ResolutionStatus, Symbol, SymbolKind


@dataclass(frozen=True)
class PropagatedSignal:
    """呼び出し先を経由して送出されうる経路（名前の一致による推定の連鎖。条件によっては実際には通らない）。"""

    signal: ef.ElispSignal
    origin: Symbol
    chain: tuple[tuple[Symbol, int], ...]  # 呼び出し元から順に (呼び出し先, 直前の関数内の呼び出し行)
    guards: tuple[str, ...]  # 連鎖の途中で、呼び出しを囲んでいる condition-case 等


@dataclass
class ElispExitReport:
    direct: ef.ElispExits
    propagated: list[PropagatedSignal] = field(default_factory=list)
    unresolved_calls: int = 0
    skipped: list[str] = field(default_factory=list)
    truncated: bool = False


class ElispFlowService:
    def __init__(self, navigation: NavigationService) -> None:
        self._navigation = navigation
        self._forms: dict[tuple[str, str], list[Form]] = {}
        self._lines: dict[tuple[str, str], list[str]] = {}
        self._known: set[str] = set()
        self._known_key: int | None = None

    def _file_forms(self, project: Project, index: ProjectIndex, symbol: Symbol) -> list[Form]:
        source_file = index.files[symbol.file_id]
        if source_file.language != Language.ELISP:
            raise FlowAnalysisError(f"{source_file.language.value} のシンボルは、Emacs Lispの解析の対象ではありません。")
        key = (project.project_id, source_file.relative_path)
        if key not in self._forms:
            try:
                data = (project.root_path / source_file.relative_path).read_bytes()
            except OSError as exc:
                raise FlowAnalysisError(f"ファイルを読み込めません: {source_file.relative_path}: {exc}") from exc
            if hashlib.sha256(data).hexdigest() != source_file.content_hash:
                raise FlowAnalysisError(f"{source_file.relative_path} は解析後に変更されています。再解析（codeinsight analyze）してから実行してください。")
            text = _decode(data, FileAnalysis())
            forms: list[Form] = []
            try:
                if source_file.relative_path.lower().endswith(".org"):
                    for _, body in org_elisp_blocks(text):
                        try:
                            forms.extend(read_forms(body))
                        except ElispSyntaxError:
                            continue  # 解析時に警告済みのブロック
                else:
                    forms = read_forms(text)
            except ElispSyntaxError as exc:
                raise FlowAnalysisError(f"{source_file.relative_path} を構文解析できません（{exc}）。") from exc
            self._forms[key] = forms
            self._lines[key] = text.split("\n")
        return self._forms[key]

    def load(self, project: Project, index: ProjectIndex, symbol: Symbol) -> Form:
        if symbol.kind not in (SymbolKind.FUNCTION, SymbolKind.MACRO):
            raise FlowAnalysisError(f"{symbol.qualified_name} は関数ではありません（{symbol.kind.value}）。")
        definition = ef.find_definition(self._file_forms(project, index, symbol), symbol.name, symbol.start_line)
        if definition is None:
            raise FlowAnalysisError(f"{symbol.qualified_name} の定義をソースから特定できません（defalias など、本体を持たない定義の可能性があります）。")
        return definition

    def control_flow(self, project: Project, index: ProjectIndex, symbol: Symbol) -> fa.ControlFlowSummary:
        return ef.analyze_control_flow(self.load(project, index, symbol))

    def parameters(self, project: Project, index: ProjectIndex, symbol: Symbol) -> list[str]:
        return ef.function_parts(self.load(project, index, symbol))[0]

    def exits(self, project: Project, index: ProjectIndex, symbol: Symbol) -> ef.ElispExits:
        return ef.analyze_exits(self.load(project, index, symbol))

    def state(self, project: Project, index: ProjectIndex, symbol: Symbol) -> ef.ElispState:
        return ef.analyze_state(self.load(project, index, symbol), self._globals(index))

    def exit_report(self, project: Project, index: ProjectIndex, symbol: Symbol, depth: int = 3) -> ElispExitReport:
        """この関数のシグナル・終了と、解決済みの呼び出しを `depth` 段たどって送出されうる経路。

        幅優先で、各関数を1度だけ調べる。呼び出しの解決は名前の一致（advice・再定義で変わりうる）。
        途中の呼び出しが condition-case 等に囲まれている場合は、その保護を併記する（捕捉されるかは、条件の一致によるため判定しない）。
        """

        direct = self.exits(project, index, symbol)
        report = ElispExitReport(direct)
        visited = {symbol.symbol_id}
        queue: deque[tuple[Symbol, tuple[tuple[Symbol, int], ...], tuple[str, ...], ef.ElispExits, int]] = deque([(symbol, (), (), direct, 0)])
        while queue:
            current, chain, guards, current_exits, level = queue.popleft()
            for hit in self._navigation.callees(index, current):
                reference = hit.reference
                if reference.reference_kind != ReferenceKind.CALL:
                    continue
                if hit.target is None:
                    if reference.resolution_status != ResolutionStatus.EXTERNAL:
                        report.unresolved_calls += 1
                    continue
                callee = hit.target
                source_file = index.files.get(callee.file_id)
                if callee.symbol_id in visited or source_file is None or source_file.language != Language.ELISP or callee.kind not in (SymbolKind.FUNCTION, SymbolKind.MACRO):
                    continue
                visited.add(callee.symbol_id)
                call_line = reference.source_location.start_line
                step = (*chain, (callee, call_line))
                step_guards = (*guards, *(f"{index.path_of(current.file_id)}:{ef._guard_label(g)}" for g in ef.guards_at(current_exits.guards, call_line)))
                try:
                    callee_exits = self.exits(project, index, callee)
                except FlowAnalysisError:
                    report.skipped.append(callee.qualified_name)
                    continue
                for item in callee_exits.signals:
                    if item.kind in ("signal", "terminate"):
                        report.propagated.append(PropagatedSignal(item, callee, step, step_guards))
                if level + 1 < depth:
                    queue.append((callee, step, step_guards, callee_exits, level + 1))
                elif any(h.reference.reference_kind == ReferenceKind.CALL and h.target is not None and h.target.symbol_id not in visited for h in self._navigation.callees(index, callee)):
                    report.truncated = True
        return report

    # --- データフロー・戻り値・リスク ---

    def _globals(self, index: ProjectIndex) -> set[str]:
        key = id(index)
        if self._known_key != key:
            self._known = {s.name for s in index.symbols.values() if s.kind == SymbolKind.GLOBAL_VARIABLE and index.files[s.file_id].language == Language.ELISP}
            self._known_key = key
        return self._known

    def variables(self, project: Project, index: ProjectIndex, symbol: Symbol) -> dict[str, ed.ElispVariable]:
        return ed.analyze_variables(self.load(project, index, symbol), self._globals(index))

    def returns(self, project: Project, index: ProjectIndex, symbol: Symbol) -> list[ed.ElispReturn]:
        return ed.returns_of(self.load(project, index, symbol))

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
        role = {"param": "引数", "local": "局所変数", "global": "グローバル・動的変数"}[info.scope]
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
                text = "戻り値として返す" if flow.target == variable else f"戻り値の式 {flow.target} に含まれる"
                trace.flows.append(TraceFlow("return", flow.line, text))
            elif flow.kind == "attr_store":
                trace.flows.append(TraceFlow("attr_store", flow.line, f"{flow.target} に書き込む（リスト・ベクタ・ハッシュテーブルなどの要素。書き込み先の別名は追えない）", status="エイリアスは追えない"))
        return trace

    def _call_flow(self, project, index, symbol, flow, depth, calls, visited) -> TraceFlow:
        label = f"{flow.call_text} の第{(flow.position or 0) + 1}引数"
        candidates = [r for r in calls.get((symbol.symbol_id, flow.call_line), []) if r.target_name == flow.target]
        reference = candidates[0] if len(candidates) == 1 else None
        if reference is None or reference.resolution_status != ResolutionStatus.RESOLVED or not reference.target_symbol_id:
            if reference is not None and reference.resolution_status == ResolutionStatus.EXTERNAL:
                status = "外部（Emacs本体・他のパッケージ）"
            elif reference is not None:
                status = "呼び出し先を静的に特定できない（同名の定義が複数ある）"
            else:
                status = "外部または特定できない（funcall・apply 経由など）"
            return TraceFlow("call_arg", flow.line, f"{label}として渡す", status=status)
        callee = index.symbols[reference.target_symbol_id]
        try:
            names = self.parameters(project, index, callee)
        except FlowAnalysisError as exc:
            return TraceFlow("call_arg", flow.line, f"{label}として渡す", callee=callee, status=str(exc))
        position = flow.position or 0
        parameter = names[position] if position < len(names) else None
        if parameter is None:
            return TraceFlow("call_arg", flow.line, f"{label}として渡す", callee=callee, status="対応する仮引数が見つからない（&rest など）")
        child = None
        if depth > 0 and (callee.symbol_id, parameter) not in visited:
            child = self._trace(project, index, callee, parameter, depth - 1, calls, visited)
        return TraceFlow("call_arg", flow.line, f"{label}として渡す → {callee.qualified_name} の仮引数 {parameter}（名前の一致による推定）", callee=callee, callee_param=parameter, child=child)

    def scan_risks(self, project: Project, index: ProjectIndex, only_paths: set[str] | None = None) -> tuple[list[Finding], list[str]]:
        """プロジェクト内のEmacs Lispの関数のリスクの手がかり。解析できなかったファイルは、第2の戻り値に記録する。"""

        findings: list[Finding] = []
        skipped: list[str] = []
        for symbol in sorted(index.symbols.values(), key=lambda s: (index.path_of(s.file_id), s.start_line)):
            source_file = index.files.get(symbol.file_id)
            if source_file is None or source_file.language != Language.ELISP or symbol.kind not in (SymbolKind.FUNCTION, SymbolKind.MACRO):
                continue
            path = source_file.relative_path
            if only_paths is not None and path not in only_paths or path in skipped:
                continue
            try:
                definition = self.load(project, index, symbol)
                hits = ed.scan_risks(definition, self._lines.get((project.project_id, path)))
            except FlowAnalysisError:
                skipped.append(path)
                continue
            for hit in hits:
                findings.append(Finding(hit.rule, RULES[hit.rule][0], path, hit.line, symbol.qualified_name, hit.detail))
        return findings, skipped

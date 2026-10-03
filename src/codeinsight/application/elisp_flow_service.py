"""Emacs Lispの関数単位の制御フロー・例外（シグナル）・状態。

保存済みの解析結果（シンボルの位置・呼び出しの解決結果）と、現在のソースのS式を組み合わせる。
ソースが解析後に変更されている場合は、位置がずれて誤った結果になるため実行しない。
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass, field

from codeinsight.analysis import elisp_flow_analysis as ef
from codeinsight.analysis import flow_analysis as fa
from codeinsight.analysis.elisp_analyzer import ElispSyntaxError, Form, _decode, org_elisp_blocks, read_forms
from codeinsight.analysis.language_adapter import FileAnalysis
from codeinsight.application.flow_service import FlowAnalysisError
from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
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
        key = id(index)
        if self._known_key != key:
            self._known = {s.name for s in index.symbols.values() if s.kind == SymbolKind.GLOBAL_VARIABLE and index.files[s.file_id].language == Language.ELISP}
            self._known_key = key
        return ef.analyze_state(self.load(project, index, symbol), self._known)

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

from __future__ import annotations

import re
from dataclasses import dataclass

from codeinsight.analysis.call_graph import (
    CallGraph,
    CallNode,
    Direction,
    strongly_connected_components,
)
from codeinsight.application.freshness_service import FreshnessService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.search_service import (
    MatchMode,
    SymbolHit,
    search_symbols_in_index,
)
from codeinsight.domain import (
    Dependency,
    FileFreshness,
    Project,
    Reference,
    ResolutionStatus,
    Symbol,
    SymbolKind,
)
from codeinsight.infrastructure.analysis_repository import AnalysisRepository

_LOCAL_KINDS = frozenset({SymbolKind.LOCAL_VARIABLE})
_LINE_SUFFIX = re.compile(r"^(.+)@(\d+)$")


class SymbolNotFoundError(LookupError):
    pass


class AmbiguousSymbolError(LookupError):
    """名前が複数のシンボルに一致し、一意に決まらない。"""

    def __init__(self, query: str, candidates: list[SymbolHit]) -> None:
        super().__init__(f"'{query}' は複数のシンボルに一致します")
        self.query = query
        self.candidates = candidates


@dataclass(frozen=True)
class ReferenceHit:
    reference: Reference
    source: Symbol
    path: str  # 参照が書かれているファイル
    target: Symbol | None

    @property
    def location(self) -> str:
        return f"{self.path}:{self.reference.source_location.start_line}"


@dataclass(frozen=True)
class DependencyHit:
    dependency: Dependency
    source_path: str
    target_path: str | None

    @property
    def location(self) -> str:
        return f"{self.source_path}:{self.dependency.evidence_location.start_line}"


@dataclass(frozen=True)
class SourceView:
    path: str
    lines: list[tuple[int, str]]
    freshness: FileFreshness
    highlight_start: int
    highlight_end: int


class NavigationService:
    """定義・参照・呼び出し関係・依存関係をたどるユースケース（3.4, 3.5, 3.6）。

    すべて保存済みの解析結果に基づく。解決できなかった関係（UNRESOLVED/AMBIGUOUS）は
    確定した関係と混ぜずに返し、利用側が「未解決」として表示できるようにする。
    """

    def __init__(
        self, repository: AnalysisRepository, freshness: FreshnessService | None = None
    ) -> None:
        self._repository = repository
        self._freshness = freshness or FreshnessService()

    def load_index(self, project: Project) -> ProjectIndex:
        return ProjectIndex.load(self._repository, project.project_id)

    # --- 定義 ---

    def lookup(
        self,
        index: ProjectIndex,
        query: str,
        *,
        file: str | None = None,
        kinds: set[SymbolKind] | None = None,
    ) -> list[SymbolHit]:
        """名前から定義候補を探す。修飾名の完全一致を優先し、無ければ名前の完全一致。"""

        by_qualified = [
            h
            for h in search_symbols_in_index(
                index, query, kinds=kinds, match=MatchMode.EXACT, file=file
            )
            if h.symbol.qualified_name == query
        ]
        candidates = by_qualified or search_symbols_in_index(
            index, query, kinds=kinds, match=MatchMode.EXACT, file=file
        )
        return [h for h in candidates if h.symbol.kind not in _LOCAL_KINDS] or candidates

    def resolve_symbol(
        self,
        index: ProjectIndex,
        query: str,
        *,
        file: str | None = None,
        kinds: set[SymbolKind] | None = None,
    ) -> SymbolHit:
        """1つのシンボルに特定する。複数あれば、定義を宣言より優先して絞り込む。"""

        line: int | None = None
        match = _LINE_SUFFIX.match(query)
        if match:  # `名前@行番号`: 同名の定義が複数ある場合に、その行を含む定義を選ぶ
            query, line = match.group(1), int(match.group(2))
        candidates = self.lookup(index, query, file=file, kinds=kinds)
        if line is not None:
            candidates = [h for h in candidates if h.symbol.start_line <= line <= h.symbol.end_line]
        if not candidates:
            raise SymbolNotFoundError(query if line is None else f"{query}@{line}")
        if len(candidates) > 1:
            definitions = [
                h for h in candidates if h.symbol.kind != SymbolKind.FUNCTION_DECLARATION
            ]
            if len(definitions) == 1:
                return definitions[0]
            raise AmbiguousSymbolError(query, candidates)
        return candidates[0]

    # --- 参照 ---

    def references_to(self, index: ProjectIndex, symbol: Symbol) -> list[ReferenceHit]:
        """シンボルを指す解決済みの参照（定義と宣言が分かれるCの関数は両方を対象にする）。"""

        target_ids = {symbol.symbol_id}
        if symbol.usr:
            target_ids |= {
                s.symbol_id for s in index.symbols.values() if s.usr == symbol.usr
            }
        hits = [
            self._reference_hit(index, r)
            for r in index.references
            if r.target_symbol_id in target_ids
            and r.resolution_status == ResolutionStatus.RESOLVED
        ]
        return sorted(hits, key=lambda h: (h.path, h.reference.source_location.start_line))

    def unresolved_references(self, index: ProjectIndex) -> list[ReferenceHit]:
        """解決できなかった（UNRESOLVED/AMBIGUOUS）参照。外部（EXTERNAL）は含まない。"""

        hits = [
            self._reference_hit(index, r)
            for r in index.references
            if r.resolution_status in (ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS)
        ]
        return sorted(hits, key=lambda h: (h.path, h.reference.source_location.start_line))

    def _reference_hit(self, index: ProjectIndex, reference: Reference) -> ReferenceHit:
        return ReferenceHit(
            reference=reference,
            source=index.symbols[reference.source_symbol_id],
            path=index.path_of(reference.source_location.file_id),
            target=index.symbols.get(reference.target_symbol_id)
            if reference.target_symbol_id
            else None,
        )

    # --- 呼び出し関係 ---

    def call_graph(self, index: ProjectIndex) -> CallGraph:
        return CallGraph(index.symbols.values(), index.references)

    def callers(self, index: ProjectIndex, symbol: Symbol) -> list[ReferenceHit]:
        graph = self.call_graph(index)
        ids = {symbol.symbol_id}
        if symbol.usr:
            ids |= {s.symbol_id for s in index.symbols.values() if s.usr == symbol.usr}
        references = [r for sid in ids for r in graph.calls_to(sid)]
        return [self._reference_hit(index, r) for r in references]

    def callees(self, index: ProjectIndex, symbol: Symbol) -> list[ReferenceHit]:
        graph = self.call_graph(index)
        return [self._reference_hit(index, r) for r in graph.calls_from(symbol.symbol_id)]

    def call_hierarchy(
        self,
        index: ProjectIndex,
        symbol: Symbol,
        direction: Direction = Direction.CALLEES,
        max_depth: int = 3,
    ) -> CallNode:
        return self.call_graph(index).hierarchy(symbol.symbol_id, direction, max_depth)

    def call_paths(
        self,
        index: ProjectIndex,
        source: Symbol,
        target: Symbol,
        max_depth: int = 8,
        limit: int = 20,
    ) -> list[list[ReferenceHit]]:
        graph = self.call_graph(index)
        paths = graph.find_paths(source.symbol_id, target.symbol_id, max_depth, limit)
        return [[self._reference_hit(index, r) for r in path] for path in paths]

    # --- 依存関係 ---

    def dependencies(
        self, index: ProjectIndex, file: str | None = None, dependents: bool = False
    ) -> list[DependencyHit]:
        """ファイル間の依存関係。file指定時は、そのファイルが依存する先（dependents=Trueなら依存される元）。"""

        selected = index.file_by_path(file) if file else None
        if file and selected is None:
            raise SymbolNotFoundError(file)
        hits: list[DependencyHit] = []
        for dependency in index.dependencies:
            if selected is not None:
                key = dependency.target_file_id if dependents else dependency.source_file_id
                if key != selected.file_id:
                    continue
            hits.append(
                DependencyHit(
                    dependency,
                    index.path_of(dependency.source_file_id),
                    index.path_of(dependency.target_file_id)
                    if dependency.target_file_id
                    else None,
                )
            )
        return sorted(hits, key=lambda h: (h.source_path, h.dependency.evidence_location.start_line))

    def dependency_cycles(self, index: ProjectIndex) -> list[list[str]]:
        """解決済みの依存関係が作る循環（循環import/include）のファイル相対パス一覧。"""

        edges: dict[str, set[str]] = {}
        for dependency in index.dependencies:
            if (
                dependency.resolution_status == ResolutionStatus.RESOLVED
                and dependency.target_file_id
                and dependency.target_file_id != dependency.source_file_id
            ):
                edges.setdefault(dependency.source_file_id, set()).add(dependency.target_file_id)
        components = [c for c in strongly_connected_components(edges) if len(c) > 1]
        return sorted([sorted(index.path_of(fid) for fid in c) for c in components])

    # --- ソース表示 ---

    def show_source(
        self,
        project: Project,
        index: ProjectIndex,
        relative_path: str,
        start: int | None = None,
        end: int | None = None,
        context: int = 3,
    ) -> SourceView:
        """ソースの該当箇所を行番号付きで返す。解析時から変更されていれば古さを付す。"""

        source_file = index.file_by_path(relative_path)
        if source_file is None:
            raise SymbolNotFoundError(relative_path)
        root = project.root_path.resolve()
        path = (root / source_file.relative_path).resolve()
        if root not in path.parents:
            raise SymbolNotFoundError(relative_path)  # プロジェクト外を指すパスは読まない
        data = path.read_bytes()
        freshness = self._freshness.check_file(project, source_file)
        lines = data.decode("utf-8", errors="replace").splitlines()
        first = start or 1
        last = end or first
        low = max(1, first - context) if start else 1
        high = min(len(lines), last + context) if start else len(lines)
        return SourceView(
            path=source_file.relative_path,
            lines=[(n, lines[n - 1]) for n in range(low, high + 1)],
            freshness=freshness,
            highlight_start=first if start else 0,
            highlight_end=last if start else 0,
        )

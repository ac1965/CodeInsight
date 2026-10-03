from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from codeinsight.application.paths import is_test_path
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import ReferenceKind, ResolutionStatus, Symbol, SymbolKind

_USES = (ReferenceKind.CALL, ReferenceKind.NAME_REF, ReferenceKind.FUNCTION_REF, ReferenceKind.TYPE_USE, ReferenceKind.INHERITANCE)
_TESTABLE = (SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS)


@dataclass
class TestReach:
    test: Symbol
    path: str
    line: int
    route: list[str]  # テストから対象までの呼び出し・参照の経路（直接なら対象のみ）

    @property
    def direct(self) -> bool:
        return len(self.route) <= 1


@dataclass
class TestMapping:
    """あるシンボルに、テストがどう届くか（静的な呼び出し・参照の連鎖）。"""

    symbol: Symbol
    reaches: list[TestReach] = field(default_factory=list)
    test_files_importing: list[str] = field(default_factory=list)


class TestMapService:
    """テストとプロダクションコードの対応を、静的な呼び出し・参照から求める。

    「テストから届く」は、解決済みの呼び出し・参照を静的にたどれることを意味し、
    その経路が実際に実行・検証されること（カバレッジ）は意味しない。
    """

    __test__ = False  # pytestがテストクラスとして収集しないようにする

    def _forward_edges(self, index: ProjectIndex) -> dict[str, list[tuple[str, int]]]:
        edges: dict[str, list[tuple[str, int]]] = defaultdict(list)
        for reference in index.references:
            if (
                reference.reference_kind in _USES
                and reference.resolution_status == ResolutionStatus.RESOLVED
                and reference.target_symbol_id
                and reference.target_symbol_id != reference.source_symbol_id
            ):
                edges[reference.source_symbol_id].append((reference.target_symbol_id, reference.source_location.start_line))
        return edges

    def _test_symbols(self, index: ProjectIndex) -> list[Symbol]:
        return [
            s
            for s in index.symbols.values()
            if is_test_path(index.path_of(s.file_id)) and s.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)
        ]

    def tests_for(self, index: ProjectIndex, symbol: Symbol, depth: int = 3) -> TestMapping:
        mapping = TestMapping(symbol)
        edges = self._forward_edges(index)
        for test in self._test_symbols(index):
            route = self._shortest(edges, test.symbol_id, symbol.symbol_id, depth, index)
            if route is not None:
                names, line = route
                mapping.reaches.append(TestReach(test, index.path_of(test.file_id), line, names))
        mapping.reaches.sort(key=lambda r: (not r.direct, len(r.route), r.path, r.line))
        target_file = symbol.file_id
        mapping.test_files_importing = sorted(
            {
                index.path_of(d.source_file_id)
                for d in index.dependencies
                if d.target_file_id == target_file and is_test_path(index.path_of(d.source_file_id))
            }
        )
        return mapping

    def _shortest(self, edges, source: str, target: str, depth: int, index: ProjectIndex):
        """source から target への最短の経路（シンボル名の列と、最初の呼び出しの行）。"""

        queue: deque[tuple[str, list[str], int]] = deque([(source, [], 0)])
        seen = {source}
        while queue:
            current, route, first_line = queue.popleft()
            if len(route) >= depth:
                continue
            for next_id, line in edges.get(current, []):
                if next_id in seen:
                    continue
                seen.add(next_id)
                name = index.symbols[next_id].qualified_name if next_id in index.symbols else next_id
                step_line = first_line or line
                if next_id == target:
                    return [*route, name], step_line
                queue.append((next_id, [*route, name], step_line))
        return None

    def untested(self, index: ProjectIndex, depth: int = 3, min_lines: int = 1) -> list[Symbol]:
        """どのテストからも（深さdepth以内で）静的に届かない、プロダクションコードの関数・クラス。"""

        edges = self._forward_edges(index)
        reached: set[str] = set()
        queue: deque[tuple[str, int]] = deque((t.symbol_id, 0) for t in self._test_symbols(index))
        seen = {sid for sid, _ in queue}
        while queue:
            current, level = queue.popleft()
            if level >= depth:
                continue
            for next_id, _ in edges.get(current, []):
                if next_id not in seen:
                    seen.add(next_id)
                    reached.add(next_id)
                    queue.append((next_id, level + 1))
        # クラスに届いたら、そのメソッド __init__ も届いたものとみなす（生成されるため）
        for symbol in index.symbols.values():
            if symbol.kind == SymbolKind.METHOD and symbol.name == "__init__" and symbol.parent_symbol_id in reached:
                reached.add(symbol.symbol_id)
        result = [
            s
            for s in index.symbols.values()
            if s.kind in _TESTABLE
            and not is_test_path(index.path_of(s.file_id))
            and s.symbol_id not in reached
            and (s.end_line - s.start_line + 1) >= min_lines
        ]
        return sorted(result, key=lambda s: (index.path_of(s.file_id), s.start_line))

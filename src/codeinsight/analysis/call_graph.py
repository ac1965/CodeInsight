from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from codeinsight.domain import Reference, ReferenceKind, ResolutionStatus, Symbol


class Direction(Enum):
    CALLEES = "callees"  # 呼び出し先をたどる
    CALLERS = "callers"  # 呼び出し元をたどる


@dataclass
class CallNode:
    """呼び出し階層の1ノード。

    symbol が None のノードは、呼び出し先を静的に確定できなかった（または
    プロジェクト外の）呼び出しを表す。label にその表記、reference に根拠を持つ。
    """

    symbol: Symbol | None
    label: str
    reference: Reference | None = None  # 親からこのノードへの辺の根拠
    children: list[CallNode] = field(default_factory=list)
    recursive: bool = False  # 祖先に同じシンボルがあり、再帰（循環）になっている
    truncated: bool = False  # 深さ制限で子の展開を打ち切った

    @property
    def status(self) -> ResolutionStatus:
        if self.reference is None:
            return ResolutionStatus.RESOLVED
        return self.reference.resolution_status


class CallGraph:
    """CALL参照から構築する静的な呼び出しグラフ。

    これは「ソースコードから静的に確認できる呼び出し」の集合であり、実行順序や
    実際に到達する経路を示すものではない（AGENTS.md 1.2-1）。条件分岐の内側の
    呼び出しも区別せず含む。解決できなかった呼び出しは、辺ではなく
    「未解決の呼び出し」として保持し、確定した辺と混ぜない。
    """

    def __init__(self, symbols: Iterable[Symbol], references: Iterable[Reference]) -> None:
        self._symbols = {s.symbol_id: s for s in symbols}
        self._calls_from: dict[str, list[Reference]] = defaultdict(list)
        self._calls_to: dict[str, list[Reference]] = defaultdict(list)
        for reference in references:
            if reference.reference_kind != ReferenceKind.CALL:
                continue
            self._calls_from[reference.source_symbol_id].append(reference)
            if (
                reference.resolution_status == ResolutionStatus.RESOLVED
                and reference.target_symbol_id is not None
            ):
                self._calls_to[reference.target_symbol_id].append(reference)

    def symbol(self, symbol_id: str) -> Symbol | None:
        return self._symbols.get(symbol_id)

    def calls_from(self, symbol_id: str) -> list[Reference]:
        """シンボルが行う全ての呼び出し（未解決・外部を含む）。"""

        return sorted(self._calls_from.get(symbol_id, []), key=_reference_order)

    def calls_to(self, symbol_id: str) -> list[Reference]:
        """シンボルへ解決できた呼び出し（呼び出し元の一覧）。"""

        return sorted(self._calls_to.get(symbol_id, []), key=_reference_order)

    def hierarchy(
        self, root_id: str, direction: Direction = Direction.CALLEES, max_depth: int = 3
    ) -> CallNode:
        root = self._symbols[root_id]
        node = CallNode(symbol=root, label=root.qualified_name)
        self._expand(node, direction, max_depth, ancestors={root_id})
        return node

    def _expand(
        self, node: CallNode, direction: Direction, remaining: int, ancestors: set[str]
    ) -> None:
        assert node.symbol is not None
        if remaining <= 0:
            node.truncated = bool(self._edges(node.symbol.symbol_id, direction))
            return
        for reference in self._edges(node.symbol.symbol_id, direction):
            child = self._child_for(reference, direction)
            if child.symbol is not None:
                symbol_id = child.symbol.symbol_id
                if symbol_id in ancestors:
                    child.recursive = True
                else:
                    self._expand(child, direction, remaining - 1, ancestors | {symbol_id})
            node.children.append(child)

    def _edges(self, symbol_id: str, direction: Direction) -> list[Reference]:
        if direction == Direction.CALLEES:
            return self.calls_from(symbol_id)
        return self.calls_to(symbol_id)

    def _child_for(self, reference: Reference, direction: Direction) -> CallNode:
        if direction == Direction.CALLERS:
            caller = self._symbols[reference.source_symbol_id]
            return CallNode(symbol=caller, label=caller.qualified_name, reference=reference)
        target = (
            self._symbols.get(reference.target_symbol_id)
            if reference.target_symbol_id
            and reference.resolution_status == ResolutionStatus.RESOLVED
            else None
        )
        if target is None:
            return CallNode(symbol=None, label=reference.target_name, reference=reference)
        return CallNode(symbol=target, label=target.qualified_name, reference=reference)

    def find_paths(
        self, source_id: str, target_id: str, max_depth: int = 8, limit: int = 20
    ) -> list[list[Reference]]:
        """source から target への、解決済みの呼び出しだけをたどる単純経路（閉路なし）。

        「静的に呼び出し関係を連鎖できる」ことを示すだけで、実行時にその経路を
        通ることは保証しない。
        """

        paths: list[list[Reference]] = []

        def walk(current: str, visited: set[str], trail: list[Reference]) -> None:
            if len(paths) >= limit or len(trail) >= max_depth:
                return
            for reference in self.calls_from(current):
                next_id = reference.target_symbol_id
                if (
                    reference.resolution_status != ResolutionStatus.RESOLVED
                    or next_id is None
                    or next_id in visited
                ):
                    continue
                if next_id == target_id:
                    paths.append([*trail, reference])
                    if len(paths) >= limit:
                        return
                    continue
                walk(next_id, visited | {next_id}, [*trail, reference])

        walk(source_id, {source_id}, [])
        return paths


def _reference_order(reference: Reference) -> tuple:
    location = reference.source_location
    return (location.file_id, location.start_line, reference.target_name)


def strongly_connected_components(edges: dict[str, set[str]]) -> list[list[str]]:
    """有向グラフの強連結成分（Tarjan法、反復版）。要素数2以上のものが循環を表す。"""

    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    nodes = set(edges) | {t for targets in edges.values() for t in targets}
    for start in sorted(nodes):
        if start in index_of:
            continue
        work: list[tuple[str, list[str]]] = [(start, sorted(edges.get(start, ())))]
        index_of[start] = lowlink[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, remaining = work[-1]
            if remaining:
                neighbour = remaining.pop(0)
                if neighbour not in index_of:
                    index_of[neighbour] = lowlink[neighbour] = counter
                    counter += 1
                    stack.append(neighbour)
                    on_stack.add(neighbour)
                    work.append((neighbour, sorted(edges.get(neighbour, ()))))
                elif neighbour in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[neighbour])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[node])
                if lowlink[node] == index_of[node]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    components.append(sorted(component))
    return components

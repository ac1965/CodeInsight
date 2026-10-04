"""制御フロー図の組み立ての共通部分（ノード・辺の作成）。言語ごとの構文の走査は、各ビルダーが行う。"""

from __future__ import annotations

from dataclasses import dataclass

from codeinsight.application.graph_builder import EdgeStyle, GraphEdge, GraphNode

Exit = tuple[str, str]  # (ノードID, 次へ進む辺のラベル)


@dataclass
class Loop:
    head: str
    breaks: list[Exit]


class CfgBase:
    """構文から作る制御フロー図の近似。実行時の到達可能性は保証しない。"""

    def _reset(self, path: str) -> None:
        self._path = path
        self._nodes: list[GraphNode] = []
        self._edges: list[GraphEdge] = []
        self._loops: list[Loop] = []
        self._handlers: list[list[str]] = []
        self._counter = 0
        self._raise_exit: str | None = None

    def _node(self, kind: str, label: str, line: int) -> str:
        node_id = f"n{self._counter}"
        self._counter += 1
        self._nodes.append(GraphNode(node_id, label, kind, self._path, line))
        return node_id

    def _edge(self, source: str, target: str, label: str = "") -> None:
        self._edges.append(
            GraphEdge(source, target, "flow", EdgeStyle.CONFIRMED, (f"{self._path}:{self._nodes[int(source[1:])].line}",), label=label)
        )

    def _connect(self, exits: list[Exit], target: str) -> None:
        for source, label in exits:
            self._edge(source, target, label)

    def _terminate_raise(self, source: str, label: str = "", end_label: str = "例外で終了", edge_label: str = "例外") -> None:
        if self._handlers:
            for handler in self._handlers[-1]:
                self._edge(source, handler, edge_label if not label else label)
        else:
            if self._raise_exit is None:
                self._raise_exit = self._node("terminal", end_label, 0)
            self._edge(source, self._raise_exit, label)

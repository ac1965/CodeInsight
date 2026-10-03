from __future__ import annotations

import enum
from collections import defaultdict, deque
from dataclasses import dataclass, field

from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import (
    Confidence,
    DependencyKind,
    ReferenceKind,
    ResolutionStatus,
    Symbol,
)

STATIC_GRAPH_NOTE = (
    "静的解析で確認できた関係のグラフです。実行順序や実際に通る経路を示すものではありません。"
)


class EdgeStyle(enum.Enum):
    """辺の確からしさ。表示上、確定と推定・未解決を区別するために用いる（3.5）。"""

    CONFIRMED = "confirmed"  # 静的に確定
    INFERRED = "inferred"  # 候補を特定できたが、実行時の挙動で変わりうる
    UNRESOLVED = "unresolved"  # 静的に確定できない（または複数候補）
    EXTERNAL = "external"  # プロジェクト外


class Traversal(enum.Enum):
    OUT = "out"  # 起点から出る辺（呼び出し先・依存先）
    IN = "in"  # 起点へ入る辺（呼び出し元・依存元）
    BOTH = "both"


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    label: str
    kind: str  # function / method / class / module / file / external / unresolved ...
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    kind: str  # call / function_ref / inheritance / import / include
    style: EdgeStyle
    evidence: tuple[str, ...] = ()  # 根拠位置 "path:line"
    note: str = ""
    count: int = 1
    label: str = ""  # 辺に表示する文言（制御フロー図の True/False 等）


@dataclass
class GraphModel:
    title: str
    graph_kind: str
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    meta: dict[str, object] = field(default_factory=dict)


def _style(status: ResolutionStatus, confidence: Confidence) -> EdgeStyle:
    if status == ResolutionStatus.RESOLVED:
        return EdgeStyle.INFERRED if confidence == Confidence.INFERRED else EdgeStyle.CONFIRMED
    if status == ResolutionStatus.EXTERNAL:
        return EdgeStyle.EXTERNAL
    return EdgeStyle.UNRESOLVED


class _Collector:
    """ノードと辺を重複なく集める。同じ端点・種類・確からしさの辺は1本にまとめる。"""

    def __init__(self) -> None:
        self.nodes: dict[str, GraphNode] = {}
        self._edges: dict[tuple, dict] = {}

    def node(self, node: GraphNode) -> None:
        self.nodes.setdefault(node.node_id, node)

    def edge(
        self, source: str, target: str, kind: str, style: EdgeStyle, evidence: str, note: str = ""
    ) -> None:
        entry = self._edges.setdefault(
            (source, target, kind, style), {"evidence": [], "notes": [], "count": 0}
        )
        entry["count"] += 1
        if evidence not in entry["evidence"]:
            entry["evidence"].append(evidence)
        if note and note not in entry["notes"]:
            entry["notes"].append(note)

    def edges(self) -> list[GraphEdge]:
        return [
            GraphEdge(
                source=source,
                target=target,
                kind=kind,
                style=style,
                evidence=tuple(entry["evidence"]),
                note=" / ".join(entry["notes"]),
                count=entry["count"],
            )
            for (source, target, kind, style), entry in self._edges.items()
        ]


def _symbol_node(index: ProjectIndex, symbol: Symbol) -> GraphNode:
    return GraphNode(
        node_id=symbol.symbol_id,
        label=symbol.qualified_name,
        kind=symbol.kind.value,
        path=index.path_of(symbol.file_id),
        line=symbol.start_line,
    )


def _restrict(
    model: GraphModel,
    root_id: str,
    depth: int | None,
    traversal: Traversal,
) -> GraphModel:
    """起点から指定の深さ・方向でたどれる部分グラフだけを残す（3.5: 部分グラフ）。"""

    outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
    incoming: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in model.edges:
        outgoing[edge.source].append(edge)
        incoming[edge.target].append(edge)

    reached = {root_id}
    kept: set[GraphEdge] = set()
    queue: deque[tuple[str, int]] = deque([(root_id, 0)])
    while queue:
        current, level = queue.popleft()
        if depth is not None and level >= depth:
            continue
        candidates: list[tuple[GraphEdge, str]] = []
        if traversal in (Traversal.OUT, Traversal.BOTH):
            candidates += [(e, e.target) for e in outgoing[current]]
        if traversal in (Traversal.IN, Traversal.BOTH):
            candidates += [(e, e.source) for e in incoming[current]]
        for edge, neighbour in candidates:
            kept.add(edge)
            if neighbour not in reached:
                reached.add(neighbour)
                queue.append((neighbour, level + 1))

    result = GraphModel(
        title=model.title,
        graph_kind=model.graph_kind,
        nodes=[n for n in model.nodes if n.node_id in reached],
        edges=[e for e in model.edges if e in kept],
        notes=list(model.notes),
        meta=dict(model.meta),
    )
    result.meta.update({"root": root_id, "depth": depth, "traversal": traversal.value})
    return result


class GraphBuilder:
    """解析結果から、種類別のグラフモデル（ノードと辺）を組み立てる（3.5）。

    出力形式（Mermaid/DOT/JSON/HTML）には依存しない。辺は確定・推定・未解決・外部を
    区別して保持し、根拠となるソース位置を持つ。
    """

    def __init__(self, index: ProjectIndex) -> None:
        self._index = index

    # --- 呼び出しグラフ ---

    def call_graph(
        self,
        root: Symbol | None = None,
        depth: int | None = None,
        traversal: Traversal = Traversal.BOTH,
        include_unresolved: bool = True,
        include_external: bool = True,
    ) -> GraphModel:
        index = self._index
        collector = _Collector()
        for reference in index.references:
            if reference.reference_kind != ReferenceKind.CALL:
                continue
            source = index.symbols.get(reference.source_symbol_id)
            if source is None:
                continue
            style = _style(reference.resolution_status, reference.confidence)
            if style == EdgeStyle.UNRESOLVED and not include_unresolved:
                continue
            if style == EdgeStyle.EXTERNAL and not include_external:
                continue
            collector.node(_symbol_node(index, source))
            evidence = f"{index.path_of(reference.source_location.file_id)}:{reference.source_location.start_line}"
            target = (
                index.symbols.get(reference.target_symbol_id)
                if reference.target_symbol_id and style in (EdgeStyle.CONFIRMED, EdgeStyle.INFERRED)
                else None
            )
            if target is not None:
                collector.node(_symbol_node(index, target))
                target_id = target.symbol_id
            elif style == EdgeStyle.EXTERNAL:
                target_id = f"external:{reference.target_name}"
                collector.node(GraphNode(target_id, reference.target_name, "external"))
            else:
                # 未解決の呼び出しは、呼び出し元ごとの別ノードにして他と混同しない。
                target_id = f"unresolved:{source.symbol_id}:{reference.target_name}"
                collector.node(GraphNode(target_id, reference.target_name, "unresolved"))
            collector.edge(source.symbol_id, target_id, "call", style, evidence, reference.note)

        model = GraphModel(
            title="呼び出しグラフ",
            graph_kind="call",
            nodes=list(collector.nodes.values()),
            edges=collector.edges(),
            notes=[STATIC_GRAPH_NOTE, "関数ポインタ等で呼び出し先を確定できないものは「未解決」の葉として示します。"],
        )
        return self._finish(model, root.symbol_id if root else None, depth, traversal)

    # --- ファイル依存グラフ ---

    def file_dependency_graph(
        self,
        root_file: str | None = None,
        depth: int | None = None,
        traversal: Traversal = Traversal.BOTH,
        include_external: bool = False,
        include_unresolved: bool = True,
    ) -> GraphModel:
        index = self._index
        collector = _Collector()
        root_id: str | None = None
        if root_file is not None:
            root_source = index.file_by_path(root_file)
            if root_source is None:
                raise LookupError(root_file)
            root_id = root_source.file_id
        for dependency in index.dependencies:
            source_file = index.files.get(dependency.source_file_id)
            if source_file is None:
                continue
            style = _style(dependency.resolution_status, dependency.confidence)
            if style == EdgeStyle.EXTERNAL and not include_external:
                continue
            if style == EdgeStyle.UNRESOLVED and not include_unresolved:
                continue
            evidence = f"{source_file.relative_path}:{dependency.evidence_location.start_line}"
            kind = dependency.dependency_kind.value
            collector.node(
                GraphNode(source_file.file_id, source_file.relative_path, "file", source_file.relative_path)
            )
            if dependency.target_file_id and style in (EdgeStyle.CONFIRMED, EdgeStyle.INFERRED):
                target_file = index.files.get(dependency.target_file_id)
                if target_file is None or target_file.file_id == source_file.file_id:
                    continue
                collector.node(
                    GraphNode(target_file.file_id, target_file.relative_path, "file", target_file.relative_path)
                )
                target_id = target_file.file_id
            elif style == EdgeStyle.EXTERNAL:
                target_id = f"external:{dependency.target_name}"
                collector.node(GraphNode(target_id, dependency.target_name, "external"))
            else:
                target_id = f"unresolved:{source_file.file_id}:{dependency.target_name}"
                collector.node(GraphNode(target_id, dependency.target_name, "unresolved"))
            collector.edge(source_file.file_id, target_id, kind, style, evidence, dependency.note)

        notes = ["ファイル間の#include/importの関係です。"]
        if any(d.dependency_kind == DependencyKind.IMPORT for d in index.dependencies):
            notes.append("動的インポートは静的に確定できないため、未解決として示します。")
        model = GraphModel(
            title="ファイル依存グラフ",
            graph_kind="file_dependency",
            nodes=list(collector.nodes.values()),
            edges=collector.edges(),
            notes=notes,
        )
        return self._finish(model, root_id, depth, traversal)

    # --- 継承グラフ ---

    def inheritance_graph(
        self,
        root: Symbol | None = None,
        depth: int | None = None,
        traversal: Traversal = Traversal.BOTH,
    ) -> GraphModel:
        index = self._index
        collector = _Collector()
        for reference in index.references:
            if reference.reference_kind != ReferenceKind.INHERITANCE:
                continue
            source = index.symbols.get(reference.source_symbol_id)
            if source is None:
                continue
            style = _style(reference.resolution_status, reference.confidence)
            collector.node(_symbol_node(index, source))
            evidence = f"{index.path_of(reference.source_location.file_id)}:{reference.source_location.start_line}"
            target = (
                index.symbols.get(reference.target_symbol_id) if reference.target_symbol_id else None
            )
            if target is not None:
                collector.node(_symbol_node(index, target))
                target_id = target.symbol_id
            elif style == EdgeStyle.EXTERNAL:
                target_id = f"external:{reference.target_name}"
                collector.node(GraphNode(target_id, reference.target_name, "external"))
            else:
                target_id = f"unresolved:{source.symbol_id}:{reference.target_name}"
                collector.node(GraphNode(target_id, reference.target_name, "unresolved"))
            collector.edge(source.symbol_id, target_id, "inheritance", style, evidence, reference.note)
        model = GraphModel(
            title="クラス継承グラフ",
            graph_kind="inheritance",
            nodes=list(collector.nodes.values()),
            edges=collector.edges(),
            notes=["矢印は派生クラスから基底クラスへ向かいます。"],
        )
        return self._finish(model, root.symbol_id if root else None, depth, traversal)

    # --- アーキテクチャ（コンポーネント）グラフ ---

    def architecture_graph(self, architecture) -> GraphModel:
        """コンポーネント間の依存グラフ。名前から推定した層の逆向き依存は、推定（破線）で示す。"""

        from codeinsight.application.architecture_service import ROLE_LABELS

        violations = {(v.source, v.target) for v in architecture.violations}
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        for name, component in sorted(architecture.components.items()):
            role = f"（{ROLE_LABELS[component.role]}・推定）" if component.role else ""
            nodes.append(GraphNode(name, f"{name}{role}", "component", None, None))
        for name, component in sorted(architecture.components.items()):
            for target in sorted(component.depends_on):
                parts = []
                if component.imports[target]:
                    parts.append(f"import {component.imports[target]}")
                if component.calls[target]:
                    parts.append(f"call {component.calls[target]}")
                inverted = (name, target) in violations
                edges.append(
                    GraphEdge(
                        name, target, "dependency",
                        EdgeStyle.INFERRED if inverted else EdgeStyle.CONFIRMED,
                        note="層の逆向き依存の候補（名前による推定）" if inverted else "",
                        label=" / ".join(parts) + (" ⚠逆向き?" if inverted else ""),
                    )
                )
        return GraphModel(
            title=f"アーキテクチャ（コンポーネント間の依存、深さ{architecture.depth}）",
            graph_kind="architecture",
            nodes=nodes,
            edges=edges,
            notes=[
                "コンポーネントはディレクトリ単位。辺はimport/includeと解決済みの呼び出しの数です。",
                "役割（括弧内）は名前からの推定です。破線は、層の順に反する可能性がある依存の候補です。",
            ],
            meta={"layers": architecture.layers, "cycles": architecture.cycles},
        )

    # --- 共通 ---

    def _finish(
        self,
        model: GraphModel,
        root_id: str | None,
        depth: int | None,
        traversal: Traversal,
    ) -> GraphModel:
        model.nodes.sort(key=lambda n: (n.kind, n.label))
        model.edges.sort(key=lambda e: (e.source, e.target, e.kind))
        if root_id is not None:
            if root_id not in {n.node_id for n in model.nodes}:
                # 起点に該当する辺が無い場合も、起点そのものは示す。
                symbol = self._index.symbols.get(root_id)
                source_file = self._index.files.get(root_id)
                if symbol is not None:
                    model.nodes.append(_symbol_node(self._index, symbol))
                elif source_file is not None:
                    model.nodes.append(
                        GraphNode(source_file.file_id, source_file.relative_path, "file", source_file.relative_path)
                    )
            return _restrict(model, root_id, depth, traversal)
        return model


__all__ = [
    "EdgeStyle",
    "GraphBuilder",
    "GraphEdge",
    "GraphModel",
    "GraphNode",
    "STATIC_GRAPH_NOTE",
    "Traversal",
]

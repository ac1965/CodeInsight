from __future__ import annotations

import json
import re

from codeinsight.application.graph_builder import EdgeStyle, GraphModel

SCHEMA = "codeinsight.graph/1"
_MAX_LABEL = 80
_SAFE_CLASS = re.compile(r"[^A-Za-z0-9_]")


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_LABEL else text[: _MAX_LABEL - 1] + "…"


def _edge_label(edge) -> str:
    label = edge.label
    if edge.count > 1:
        label = f"{label} ×{edge.count}".strip()
    if edge.style == EdgeStyle.INFERRED:
        label = (label + " 推定").strip()
    elif edge.style == EdgeStyle.UNRESOLVED:
        label = (label + " 未解決").strip()
    elif edge.style == EdgeStyle.EXTERNAL:
        label = (label + " 外部").strip()
    return label


# --- Mermaid ---


def _mermaid_text(text: str) -> str:
    """Mermaidの引用符付きラベルに安全に埋め込めるよう、特殊文字を実体参照にする。"""

    escaped = []
    for char in _short(text):
        if char == '"':
            escaped.append("#quot;")
        elif char == "<":
            escaped.append("#lt;")
        elif char == ">":
            escaped.append("#gt;")
        elif char == "|":
            escaped.append("#124;")
        elif char == "`":
            escaped.append("#96;")
        elif char == "\\":
            escaped.append("#92;")
        elif char in "{}":
            escaped.append(f"#{ord(char)};")
        else:
            escaped.append(char)
    return "".join(escaped)


def to_mermaid(model: GraphModel, direction: str | None = None) -> str:
    direction = direction or ("TD" if model.graph_kind == "flow" else "LR")  # 制御フロー図は上から下へ
    ids = {node.node_id: f"n{i}" for i, node in enumerate(model.nodes)}
    lines = [f"graph {direction}"]
    for note in model.notes:
        lines.append("%% " + " ".join(note.split()))
    if model.focus is not None:
        lines.append("%% 起点を中心に、左が Depended On By（起点に依存している側）、右が Depends On（起点が依存している側）です。")

    def node_line(node, indent: str) -> str:
        if node.kind in ("external", "unresolved", "terminal"):
            shape_open, shape_close = "([", "])"
        elif node.kind == "decision":
            shape_open, shape_close = "{", "}"
        else:
            shape_open, shape_close = "[", "]"
        kind_class = _SAFE_CLASS.sub("_", node.kind)
        return f'{indent}{ids[node.node_id]}{shape_open}"{_mermaid_text(node.label)}"{shape_close}:::{kind_class}'

    groups: dict[str, list] = {}
    if model.focus is not None:
        for node in model.nodes:
            groups.setdefault(node.group, []).append(node)
    for index, (group, members) in enumerate(groups.items()):
        lines.append(f'    subgraph g{index}["{_mermaid_text(group or "(その他)")}"]')
        lines.extend(node_line(node, "        ") for node in members)
        lines.append("    end")
    if model.focus is None:
        lines.extend(node_line(node, "    ") for node in model.nodes)
    elif model.focus in ids:
        lines.append(f"    class {ids[model.focus]} root")
    link_styles: list[str] = []
    position = -1  # MermaidのlinkStyleは、実際に出力した辺の通し番号で指定する
    for edge in model.edges:
        source, target = ids.get(edge.source), ids.get(edge.target)
        if source is None or target is None:
            continue
        position += 1
        label = _edge_label(edge)
        if edge.style == EdgeStyle.CONFIRMED:
            arrow = f'-->|"{label}"|' if label else "-->"
        else:
            arrow = f'-.->|"{label}"|' if label else "-.->"
        lines.append(f"    {source} {arrow} {target}")
        if edge.style in (EdgeStyle.UNRESOLVED, EdgeStyle.EXTERNAL):
            link_styles.append(
                f"    linkStyle {position} stroke:#b45309,stroke-dasharray:2 4"
                if edge.style == EdgeStyle.UNRESOLVED
                else f"    linkStyle {position} stroke:#6b7280,stroke-dasharray:2 4"
            )
    lines.extend(link_styles)
    lines.append("    classDef unresolved fill:#fef3c7,stroke:#b45309,color:#78350f")
    lines.append("    classDef external fill:#f3f4f6,stroke:#6b7280,color:#374151")
    lines.append("    classDef terminal fill:#e5e7eb,stroke:#6b7280,color:#111827")
    lines.append("    classDef decision fill:#dbeafe,stroke:#2563eb,color:#1e3a8a")
    lines.append("    classDef root fill:#fde68a,stroke:#b45309,stroke-width:3px,color:#111827")
    return "\n".join(lines) + "\n"


# --- DOT ---


def _dot_text(text: str) -> str:
    return _short(text).replace("\\", "\\\\").replace('"', '\\"')


_DOT_EDGE_STYLE = {
    EdgeStyle.CONFIRMED: 'style="solid"',
    EdgeStyle.INFERRED: 'style="dashed"',
    EdgeStyle.UNRESOLVED: 'style="dotted", color="#b45309"',
    EdgeStyle.EXTERNAL: 'style="dotted", color="#6b7280"',
}


def to_dot(model: GraphModel) -> str:
    ids = {node.node_id: f"n{i}" for i, node in enumerate(model.nodes)}
    note = _dot_text(" ".join(model.notes))
    lines = [
        "digraph codeinsight {",
        f"    rankdir={'TB' if model.graph_kind == 'flow' else 'LR'};",
        f'    label="{_dot_text(model.title)} — {note}";',
        '    node [shape=box, fontname="Helvetica"];',
    ]
    def node_line(node, indent: str) -> str:
        attributes = f'label="{_dot_text(node.label)}"'
        if node.kind in ("external", "unresolved"):
            color = "#6b7280" if node.kind == "external" else "#b45309"
            attributes += f', shape=ellipse, style="dashed", color="{color}"'
        elif node.kind == "terminal":
            attributes += ", shape=oval"
        elif node.kind == "decision":
            attributes += ", shape=diamond"
        if node.node_id == model.focus:
            attributes += ', style="filled", fillcolor="#fde68a", color="#b45309", penwidth=3'
        return f"{indent}{ids[node.node_id]} [{attributes}];"

    if model.focus is None:
        lines.extend(node_line(node, "    ") for node in model.nodes)
    else:
        # 起点を中心に、起点に依存している側（左）・起点が依存している側（右）を、距離ごとの列に並べ、ファイル等でまとめる。
        lines.append("    compound=true;")
        by_group: dict[str, list] = {}
        for node in model.nodes:
            by_group.setdefault(node.group, []).append(node)
        for index, (group, members) in enumerate(by_group.items()):
            lines.append(f"    subgraph cluster_{index} {{")
            lines.append(f'        label="{_dot_text(group or "(その他)")}"; style="rounded"; color="#9ca3af"; fontsize=10;')
            lines.extend(node_line(node, "        ") for node in members)
            lines.append("    }")
        by_distance: dict[int, list[str]] = {}
        for node in model.nodes:
            if node.distance is not None:
                by_distance.setdefault(node.distance, []).append(ids[node.node_id])
        for members in by_distance.values():
            lines.append("    { rank=same; " + "; ".join(members) + "; }")
    for edge in model.edges:
        source, target = ids.get(edge.source), ids.get(edge.target)
        if source is None or target is None:
            continue
        label = _edge_label(edge)
        label_attr = f', label="{_dot_text(label)}"' if label else ""
        lines.append(f"    {source} -> {target} [{_DOT_EDGE_STYLE[edge.style]}{label_attr}];")
    lines.append("}")
    return "\n".join(lines) + "\n"


# --- JSON ---


def model_to_dict(model: GraphModel) -> dict:
    return {
        "schema": SCHEMA,
        "title": model.title,
        "graph_kind": model.graph_kind,
        "notes": model.notes,
        "meta": model.meta,
        "focus": model.focus,
        "nodes": [
            {
                "id": n.node_id,
                "label": n.label,
                "kind": n.kind,
                "path": n.path,
                "line": n.line,
                "end_line": n.end_line,
                "distance": n.distance,
                "group": n.group,
            }
            for n in model.nodes
        ],
        "edges": [
            {
                "source": e.source,
                "target": e.target,
                "kind": e.kind,
                "style": e.style.value,
                "label": e.label,
                "evidence": list(e.evidence),
                "note": e.note,
                "count": e.count,
            }
            for e in model.edges
        ],
    }


def to_json(model: GraphModel) -> str:
    return json.dumps(model_to_dict(model), ensure_ascii=False, indent=2) + "\n"

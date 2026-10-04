from __future__ import annotations

import json
import re
from pathlib import Path

from codeinsight.application import NavigationService
from codeinsight.application.graph_builder import (
    EdgeStyle,
    GraphBuilder,
    GraphEdge,
    GraphModel,
    GraphNode,
    Traversal,
)
from codeinsight.presentation import (
    build_structure_tree,
    render_html,
    render_tree_text,
    to_dot,
    to_json,
    to_mermaid,
    tree_to_dict,
)


def _index(analyzed, root: Path):
    repo, project, _ = analyzed(root)
    nav = NavigationService(repo)
    return project, nav, nav.load_index(project)


def test_call_graph_separates_confirmed_unresolved_and_external(
    analyzed, c_callgraph_dir: Path
) -> None:
    _, _, index = _index(analyzed, c_callgraph_dir)
    model = GraphBuilder(index).call_graph()
    labels = {n.node_id: n for n in model.nodes}

    def edge(source: str, target_label: str):
        return [
            e
            for e in model.edges
            if labels[e.source].label == source and labels[e.target].label == target_label
        ]

    assert edge("main", "apply")[0].style == EdgeStyle.CONFIRMED
    assert edge("fib", "fib")[0].count == 2  # 同じ端点の辺は1本にまとめ、回数を持つ
    (to_op,) = edge("main", "op")
    assert to_op.style == EdgeStyle.UNRESOLVED
    assert labels[to_op.target].kind == "unresolved"
    (to_printf,) = edge("main", "printf")
    assert to_printf.style == EdgeStyle.EXTERNAL
    assert any("実行" in note for note in model.notes)  # 実行経路ではない旨の注記


def test_unresolved_calls_from_different_callers_are_not_merged(
    analyzed, c_callgraph_dir: Path
) -> None:
    _, _, index = _index(analyzed, c_callgraph_dir)
    model = GraphBuilder(index).call_graph()
    unresolved_ops = [n for n in model.nodes if n.kind == "unresolved" and n.label == "op"]
    assert len(unresolved_ops) == 2  # main内のopとapply内のopは別物


def test_subgraph_respects_root_depth_and_direction(analyzed, c_callgraph_dir: Path) -> None:
    _, nav, index = _index(analyzed, c_callgraph_dir)
    builder = GraphBuilder(index)
    apply_symbol = nav.resolve_symbol(index, "apply").symbol

    out = builder.call_graph(apply_symbol, depth=1, traversal=Traversal.OUT)
    assert {n.label for n in out.nodes} == {"apply", "op"}

    incoming = builder.call_graph(apply_symbol, depth=1, traversal=Traversal.IN)
    assert {n.label for n in incoming.nodes} == {"apply", "main"}

    both = builder.call_graph(apply_symbol, depth=1, traversal=Traversal.BOTH)
    assert {n.label for n in both.nodes} == {"apply", "op", "main"}

    isolated = builder.call_graph(nav.resolve_symbol(index, "helper").symbol, depth=1, traversal=Traversal.OUT)
    assert [n.label for n in isolated.nodes] == ["helper"]  # 辺が無くても起点は示す


def test_file_dependency_and_inheritance_graphs(
    analyzed, c_callgraph_dir: Path, python_pkg_dir: Path
) -> None:
    _, _, c_index = _index(analyzed, c_callgraph_dir)
    deps = GraphBuilder(c_index).file_dependency_graph()
    labels = {n.node_id: n.label for n in deps.nodes}
    assert {(labels[e.source], labels[e.target]) for e in deps.edges} == {
        ("main.c", "ops.h"),
        ("ops.c", "ops.h"),
    }
    with_external = GraphBuilder(c_index).file_dependency_graph(include_external=True)
    assert any(n.kind == "external" and n.label == "stdio.h" for n in with_external.nodes)

    sub = GraphBuilder(c_index).file_dependency_graph("ops.h", depth=1, traversal=Traversal.IN)
    assert {n.label for n in sub.nodes} == {"ops.h", "main.c", "ops.c"}

    _, nav, py_index = _index(analyzed, python_pkg_dir)
    inheritance = GraphBuilder(py_index).inheritance_graph()
    names = {n.node_id: n.label for n in inheritance.nodes}
    assert {(names[e.source], names[e.target]) for e in inheritance.edges} == {
        ("app.models.Derived", "app.models.Base")
    }


def test_exporters_render_styles_and_escape_untrusted_labels() -> None:
    hostile = 'evil"]; </script><img src=x onerror=alert(1)> | {x}\nline2'
    model = GraphModel(
        title="t",
        graph_kind="call",
        nodes=[
            GraphNode("a", hostile, "function"),
            GraphNode("b", "ok", "function"),
            GraphNode("c", "fp", "unresolved"),
        ],
        edges=[
            GraphEdge("a", "b", "call", EdgeStyle.CONFIRMED),
            GraphEdge("a", "c", "call", EdgeStyle.UNRESOLVED),
            GraphEdge("b", "b", "call", EdgeStyle.INFERRED, count=3),
        ],
        notes=["静的グラフ\n改行を含む注記"],
    )

    mermaid = to_mermaid(model)
    assert "-->" in mermaid and "-.->" in mermaid
    assert 'evil"' not in mermaid  # 引用符はエスケープされる
    assert "\n</script>" not in mermaid
    node_lines = [ln for ln in mermaid.splitlines() if ln.strip().startswith("n0[")]
    assert len(node_lines) == 1  # 改行がノード定義を分断しない
    assert all(not ln.startswith("%%") or "\n" not in ln for ln in mermaid.splitlines())

    dot = to_dot(model)
    assert 'style="solid"' in dot and 'style="dashed"' in dot and 'style="dotted"' in dot
    assert '\\"' in dot  # 引用符がエスケープされている

    data = json.loads(to_json(model))
    assert data["schema"] == "codeinsight.graph/1"
    assert [e["style"] for e in data["edges"]] == ["confirmed", "unresolved", "inferred"]
    assert data["nodes"][0]["label"] == hostile  # JSONは元の値をそのまま保持する


def test_mermaid_link_style_indexes_match_emitted_edges() -> None:
    model = GraphModel(
        title="t",
        graph_kind="call",
        nodes=[GraphNode("a", "a", "function"), GraphNode("b", "b", "unresolved")],
        edges=[
            GraphEdge("missing", "a", "call", EdgeStyle.CONFIRMED),  # 出力されない辺
            GraphEdge("a", "b", "call", EdgeStyle.UNRESOLVED),
        ],
    )
    mermaid = to_mermaid(model)
    assert "linkStyle 0 " in mermaid and "linkStyle 1 " not in mermaid


def test_html_viewer_is_self_contained_and_cannot_be_broken_out_of() -> None:
    hostile = "</script><script>alert(1)</script>"
    model = GraphModel(
        title=hostile,
        graph_kind="call",
        nodes=[GraphNode("a", hostile, "function", "x.c", 1)],
        notes=[hostile],
    )
    html = render_html(model)

    assert html.count("</script>") == 2  # データ用とロジック用のscriptの閉じタグのみ
    assert "alert(1)" in html  # 文字列としては含まれるが、タグとしては現れない
    assert "<script>alert" not in html
    assert not re.search(r"""(src|href)\s*=\s*["']https?:""", html)  # 外部リソースを読み込まない
    assert "innerHTML" not in html
    assert "Content-Security-Policy" in html

    payload = re.search(r'<script id="graph-data" type="application/json">(.*?)</script>', html, re.S)
    assert payload is not None
    assert json.loads(payload.group(1))["nodes"][0]["label"] == hostile


def test_structure_tree_nests_symbols_and_marks_failures(tmp_path: Path, analyzed) -> None:
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("class A:\n    def m(self):\n        pass\n\nX = 1\n")
    (root / "bad.py").write_text("def broken(:\n")
    project, nav, index = _index(analyzed, root)

    tree = build_structure_tree(index, project.name)
    text = render_tree_text(tree)

    assert "pkg/" in text and "a.py" in text
    assert "bad.py  [解析失敗]" in text
    assert text.index("A  (class") < text.index("m  (method") < text.index("X  (global_variable")
    as_dict = tree_to_dict(tree)
    assert as_dict["kind"] == "project"
    assert [c["label"] for c in as_dict["children"]] == ["pkg/", "bad.py"]  # ディレクトリが先


def test_focus_graph_places_dependents_left_and_dependencies_right(analyzed, c_callgraph_dir: Path) -> None:
    _, nav, index = _index(analyzed, c_callgraph_dir)
    root = next(s for s in index.symbols.values() if s.name == "apply")
    model = GraphBuilder(index).call_graph(root, 3, Traversal.BOTH)
    assert model.focus == root.symbol_id
    distances = {n.label: n.distance for n in model.nodes}
    assert distances["apply"] == 0
    assert distances["main"] == -1  # apply を呼ぶ側（Depended On By）は負
    assert all(d is None or d >= 0 for label, d in distances.items() if label != "main")
    assert next(n for n in model.nodes if n.label == "main").group.endswith("main.c")  # ファイルごとにまとめる


def test_focus_graph_exports_group_clusters_and_root_highlight(analyzed, c_callgraph_dir: Path) -> None:
    _, _, index = _index(analyzed, c_callgraph_dir)
    root = next(s for s in index.symbols.values() if s.name == "apply")
    model = GraphBuilder(index).call_graph(root, 2, Traversal.BOTH)
    mermaid = to_mermaid(model)
    assert "subgraph g0[" in mermaid and "class " in mermaid and "root" in mermaid and "Depended On By" in mermaid
    dot = to_dot(model)
    assert "subgraph cluster_0" in dot and "rank=same" in dot and "#fde68a" in dot  # 起点を強調
    data = json.loads(to_json(model))
    assert data["focus"] == root.symbol_id and {n["distance"] for n in data["nodes"]} >= {0, -1}
    html = render_html(model)
    assert "Depended On By" in html and "Depends On" in html and '"focus"' in html


def test_graph_without_root_has_no_focus(analyzed, c_callgraph_dir: Path) -> None:
    _, _, index = _index(analyzed, c_callgraph_dir)
    model = GraphBuilder(index).call_graph()
    assert model.focus is None and all(n.distance is None for n in model.nodes)

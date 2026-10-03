from __future__ import annotations

import ast
import json
from pathlib import Path

from codeinsight.application import CfgBuilder
from codeinsight.application.graph_builder import GraphModel
from codeinsight.cli import main
from codeinsight.presentation import render_html, to_dot, to_json, to_mermaid


def _cfg(source: str, name: str = "f") -> GraphModel:
    tree = ast.parse(source)
    function = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    return CfgBuilder().build(function, "m.py", name)


def _edges(model: GraphModel) -> set[tuple[str, str, str]]:
    labels = {n.node_id: n.label for n in model.nodes}
    return {(labels[e.source], labels[e.target], e.label) for e in model.edges}


def test_if_else_produces_true_false_branches_that_rejoin() -> None:
    model = _cfg("def f(x):\n    if x:\n        a = 1\n    else:\n        a = 2\n    return a\n")
    edges = _edges(model)
    kinds = {n.label.split(":")[0]: n.kind for n in model.nodes}

    assert kinds["L2"] == "decision"
    assert ("L2: x", "L3: a = 1", "True") in edges and ("L2: x", "L5: a = 2", "False") in edges
    assert ("L3: a = 1", "L6: return a", "") in edges and ("L5: a = 2", "L6: return a", "") in edges
    assert ("L6: return a", "終了", "") in edges


def test_if_without_else_falls_through_on_false() -> None:
    model = _cfg("def f(x):\n    if x:\n        return 1\n    return 2\n")
    edges = _edges(model)
    assert ("L2: x", "L3: return 1", "True") in edges
    assert ("L2: x", "L4: return 2", "False") in edges
    assert not any(src == "L3: return 1" and dst == "L4: return 2" for src, dst, _ in edges)  # returnの後には進まない


def test_loop_has_back_edge_break_and_exit() -> None:
    model = _cfg(
        "def f(items):\n    for i in items:\n        if i:\n            break\n        use(i)\n    return 0\n"
    )
    edges = _edges(model)
    assert ("L2: for i in items", "L3: i", "繰り返す") in edges
    assert ("L5: use(i)", "L2: for i in items", "次の繰り返し") in edges
    assert ("L4: break", "L6: return 0", "ループ終了") in edges
    assert ("L2: for i in items", "L6: return 0", "終了") in edges


def test_try_except_finally_and_raise_inside_try_go_to_handlers() -> None:
    model = _cfg(
        "def f():\n    try:\n        risky()\n        raise ValueError()\n    except ValueError:\n        handle()\n"
        "    finally:\n        cleanup()\n    return 1\n"
    )
    edges = _edges(model)
    assert ("L2: try", "L5: except ValueError", "例外") in edges
    assert ("L4: raise ValueError()", "L5: except ValueError", "例外") in edges  # tryの中のraiseは捕捉される側へ
    assert ("L5: except ValueError", "L6: handle()", "捕捉") in edges
    assert any(dst == "L8: finally" for _, dst, _ in edges)  # finallyは本体の先頭行で示す
    assert ("L8: cleanup()", "L9: return 1", "") in edges
    assert not any(label == "例外で終了" for _, label, _ in edges)


def test_uncaught_raise_ends_the_function_abnormally() -> None:
    model = _cfg("def f(x):\n    if not x:\n        raise ValueError('bad')\n    return x\n")
    assert ("L3: raise ValueError('bad')", "例外で終了", "") in _edges(model)
    assert any(n.kind == "terminal" and n.label == "例外で終了" for n in model.nodes)


def test_unreachable_statements_after_return_are_omitted_and_simple_statements_grouped() -> None:
    model = _cfg("def f():\n    a = 1\n    b = 2\n    c = 3\n    return c\n    d = 4\n")
    labels = [n.label for n in model.nodes]
    assert "L2: a = 1  (+2文)" in labels  # 連続する単純な文は1ブロック
    assert not any("d = 4" in label for label in labels)


def test_match_statement_branches_per_case() -> None:
    model = _cfg("def f(x):\n    match x:\n        case 1:\n            a = 1\n        case _:\n            a = 2\n    return a\n")
    labels = {e[2] for e in _edges(model)}
    assert {"case 1", "case _", "どれにも一致しない"} <= labels


def test_exporters_render_labelled_flow_charts(python_flow_dir: Path) -> None:
    tree = ast.parse((python_flow_dir / "flow.py").read_text())
    function = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "fetch")
    model = CfgBuilder().build(function, "flow.py", "flow.fetch")

    mermaid = to_mermaid(model)
    assert mermaid.startswith("graph TD") and '-->|"True"|' in mermaid and '{"L27: data is None"}' in mermaid
    dot = to_dot(model)
    assert "rankdir=TB" in dot and "shape=diamond" in dot and 'label="繰り返す"' in dot
    data = json.loads(to_json(model))
    assert data["graph_kind"] == "flow" and any(e["label"] == "例外" for e in data["edges"])
    assert '"graph_kind": "flow"' in render_html(model)


def test_cli_graph_flow(tmp_path: Path, python_flow_dir: Path, capsys) -> None:
    db = str(tmp_path / "g.sqlite")
    main(["analyze", str(python_flow_dir), "--db", db])
    capsys.readouterr()

    code = main(["graph", "flow", "--root", "flow.caller", "--format", "mermaid", "--db", db])
    out = capsys.readouterr().out
    assert code == 0 and "except NotFound" in out and "捕捉" in out

    code = main(["graph", "flow", "--db", db])
    assert code == 1 and "--root" in capsys.readouterr().err
    code = main(["graph", "flow", "--root", "flow.Counter", "--db", db])
    assert code == 1 and "関数/メソッドではありません" in capsys.readouterr().err

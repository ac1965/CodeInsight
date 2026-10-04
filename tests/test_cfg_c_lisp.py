from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeinsight.cli import main

C_SOURCE = """\
#include <stdlib.h>
int classify(int x, int n) {
    int total = 0;
    for (int i = 0; i < n; i++) {
        if (i == 3) continue;
        total += i;
    }
    switch (x) {
    case 1:
        total += 1;
    case 2:
        total += 2;
        break;
    default:
        if (total > 100) exit(1);
        goto done;
    }
    while (total > 10) total -= 10;
done:
    return total;
}
"""
ELISP_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "elisp_sample"


def _flow(capsys, db: Path, project: Path, name: str) -> dict:
    assert main(["graph", "flow", "--root", name, "--format", "json", "--db", str(db), "--project", str(project)]) == 0
    return json.loads(capsys.readouterr().out)


def _edges(graph: dict) -> set[tuple[str, str, str]]:
    labels = {n["id"]: n["label"] for n in graph["nodes"]}
    return {(labels[e["source"]], e["label"], labels[e["target"]]) for e in graph["edges"]}


def _find(edges: set[tuple[str, str, str]], source: str, label: str, target: str) -> bool:
    return any(source in s and label == lab and target in t for s, lab, t in edges)


@pytest.fixture
def c_project(tmp_path: Path, capsys):
    root = tmp_path / "cproj"
    root.mkdir()
    (root / "sw.c").write_text(C_SOURCE, encoding="utf-8")
    db = tmp_path / "c.db"
    assert main(["analyze", str(root), "--db", str(db)]) == 0
    capsys.readouterr()
    return db, root


def test_c_cfg_shows_loops_switch_fallthrough_goto_and_exit(c_project, capsys) -> None:
    db, root = c_project
    edges = _edges(_flow(capsys, db, root, "classify"))
    assert _find(edges, "for (int i", "繰り返す", "if i == 3") and _find(edges, "continue", "次の繰り返し", "for (int i")  # continue はループの先頭へ
    assert _find(edges, "total += 1", "落ち込み", "case 2")  # break なしで次の case へ続く
    assert _find(edges, "switch x", "default", "default") and _find(edges, "break", "ループ・switch を抜ける", "while (total")  # break は switch を抜ける
    assert _find(edges, "exit(1)", "プロセスを終了", "終了") and _find(edges, "goto done", "goto", "done:")
    assert _find(edges, "while (total", "終了", "done:")  # while を抜けた先は、ラベルの文
    assert _find(edges, "return total", "", "終了")


def test_lisp_cfg_routes_signals_to_handlers_and_cleanup(tmp_path: Path, capsys) -> None:
    db = tmp_path / "e.db"
    assert main(["analyze", str(ELISP_FIXTURE), "--db", str(db)]) == 0
    capsys.readouterr()
    graph = _flow(capsys, db, ELISP_FIXTURE, "core-risky")
    edges = _edges(graph)
    assert _find(edges, "(user-error", "シグナル", "捕捉 error")  # シグナルは、囲む condition-case のハンドラへ
    assert _find(edges, "(error", "シグナル", "捕捉 error")
    assert _find(edges, "unwind-protect", "シグナル時も実行", "後始末")  # 後始末は、シグナル時も実行される
    assert _find(edges, "cond (> total 10)", "偽", "cond (= total 0)")  # cond は節ごとの分岐
    assert not any("フロー解析の例" in n["label"] for n in graph["nodes"])  # docstring は処理として示さない
    # シグナルで終わる節の後には、次へ進む辺がない
    assert not any("(error" in s and lab == "真" for s, lab, _ in edges)
    assert _find(edges, "total", "", "終了")


def test_lisp_cfg_without_handler_ends_with_signal(tmp_path: Path, capsys) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "a.el").write_text('(defun a-fn (x)\n  (when x\n    (error "boom"))\n  (message "ok"))\n', encoding="utf-8")
    db = tmp_path / "a.db"
    assert main(["analyze", str(root), "--db", str(db)]) == 0
    capsys.readouterr()
    edges = _edges(_flow(capsys, db, root, "a-fn"))
    assert any("(error" in s and "シグナルで終了" in t for s, _, t in edges)

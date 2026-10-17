"""Cの関数で使われているマクロと、その定義の表示（展開の内部は追えないため、定義のソースを示して読めるようにする）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeinsight.cli import main

SOURCE = """\
#include <stdio.h>
#define LIMIT 10
#define SQUARE(x) ((x) * (x))
#define CHECK(cond) do { if (!(cond)) { return -1; } } while (0)
int compute(int v) {
    CHECK(v < LIMIT);
    if (v == EOF) return 0;
    printf("%d\\n", SQUARE(v));
    return SQUARE(v) + LIMIT;
}
int plain(int v) { return v; }
"""


@pytest.fixture
def db(tmp_path: Path, capsys) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "m.c").write_text(SOURCE, encoding="utf-8")
    path = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", path]) == 0
    capsys.readouterr()
    return path


def test_flow_lists_used_macros_with_their_definitions(db: str, capsys) -> None:
    assert main(["flow", "compute", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "使っているマクロ" in out
    assert "SQUARE ×2" in out and "((x) * (x))" in out  # 使用回数と、定義の本体（ソース上の空白のまま）
    assert "CHECK ×1" in out and "do { if (!(cond))" in out  # 制御を生むマクロも、定義が読める
    assert "LIMIT ×2" in out and "m.c:2" in out  # 定義の位置
    assert "システムヘッダーのマクロ: EOF" in out  # システムのマクロは、名前だけ


def test_json_has_macros_and_functions_without_macros_have_none(db: str, capsys) -> None:
    assert main(["flow", "compute", "--db", db, "--format", "json"]) == 0
    macros = {m["name"]: m for m in json.loads(capsys.readouterr().out)["macros"]}
    assert macros["SQUARE"]["count"] == 2 and macros["SQUARE"]["definition"] == "m.c:3" and not macros["SQUARE"]["system"]
    assert main(["flow", "plain", "--db", db, "--format", "json"]) == 0
    assert [m for m in json.loads(capsys.readouterr().out)["macros"] if not m["system"]] == []


EXPANSION = """\
#define CHECK(c) do { if (!(c)) { return -1; } } while (0)
#define SQUARE(x) ((x) * (x))
#define CALL2(f, a) (f(a) + f(a))
#define ASSIGN_ONE(p) ((p) = 1)
int g(int);
int compute(int v) {
    int r;
    CHECK(v < 10);
    r = SQUARE(v) + CALL2(g, v);
    ASSIGN_ONE(r);
    return r;
}
"""


def test_macro_expansion_structure_shows_hidden_control_and_calls(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "e.c").write_text(EXPANSION, encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    capsys.readouterr()
    assert main(["flow", "compute", "--db", db, "--format", "json"]) == 0
    macros = {m["name"]: m for m in json.loads(capsys.readouterr().out)["macros"]}
    assert macros["CHECK"]["hidden_exit"] is True and {"do", "if", "return"} <= set(macros["CHECK"]["expands_control"])  # return が、マクロの中に隠れている
    assert macros["CALL2"]["expands_calls"] == ["g"] and macros["CALL2"]["hidden_exit"] is False  # 引数で渡した関数の呼び出し
    assert macros["SQUARE"]["expands_control"] == [] and macros["SQUARE"]["hidden_exit"] is False
    capsys.readouterr()
    assert main(["flow", "compute", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "展開後の構造（展開後のASTから）: 制御: do・if・return" in out and "この関数の制御が、ここで変わる" in out

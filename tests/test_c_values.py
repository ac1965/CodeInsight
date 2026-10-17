"""値・条件を考慮した解析の最小版: コンパイル時に決まる条件（マクロ・enum・sizeof・定数式）と、return の定数。確定ではなく近似として示す。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeinsight.cli import main

SOURCE = """\
#define DEBUG 0
#define FAST 1
enum { K = 3 };
int constant_if(int x) {
    if (DEBUG) { return -1; }
    if (K == 3) { x += 1; }
    if (sizeof(int) == 8) { x += 2; }
    if (x > 0) { return 1; }
    return 0;
}
int spins(int x) { while (FAST) { x++; } return x; }
int leaves(int x) { while (1) { if (x > 3) break; x++; } return x; }
int never(int x) { while (0) { x++; } return x; }
int errors(int x) { if (x < 0) return -1; if (x == 0) return -2; return 0; }
int mixed(int x) { if (x) return 7; return x + 1; }
int idiom(int x) { do { x++; } while (0); return x; }
"""


@pytest.fixture
def db(tmp_path: Path, capsys) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "v.c").write_text(SOURCE, encoding="utf-8")
    path = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", path]) == 0
    capsys.readouterr()
    return path


def flow_json(capsys, db: str, name: str) -> dict:
    capsys.readouterr()
    assert main(["flow", name, "--db", db, "--format", "json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_constant_conditions_are_hinted_with_macros_enums_and_sizeof(db: str, capsys) -> None:
    hints = {(h["line"], h["kind"]): h["detail"] for h in flow_json(capsys, db, "constant_if")["hints"]}
    assert "常に偽" in hints[(5, "constant-condition")]  # DEBUG（マクロ）
    assert "常に真" in hints[(6, "constant-condition")]  # enum の K == 3
    assert (7, "constant-condition") in hints  # sizeof(int) == 8 は、処理系による。評価した値で判定する
    assert (8, "constant-condition") not in hints  # 実行時に決まる条件は、定数にしない


def test_infinite_loops_and_dead_loops(db: str, capsys) -> None:
    assert [h["kind"] for h in flow_json(capsys, db, "spins")["hints"]] == ["infinite-loop"]  # while (FAST): 抜ける手段が無い
    assert flow_json(capsys, db, "leaves")["hints"] == []  # break があれば、終わらないとは言わない
    assert [h["kind"] for h in flow_json(capsys, db, "never")["hints"]] == ["constant-condition"]  # while (0)
    assert flow_json(capsys, db, "idiom")["hints"] == []  # do { } while (0) の定型は、対象にしない


def test_return_constants_are_listed_and_non_constant_values_are_flagged(db: str, capsys) -> None:
    errors = flow_json(capsys, db, "errors")
    assert errors["return_constants"] == [-2, -1, 0] and errors["return_has_non_constant"] is False
    mixed = flow_json(capsys, db, "mixed")
    assert mixed["return_constants"] == [7] and mixed["return_has_non_constant"] is True  # x + 1 は定数に決まらない
    capsys.readouterr()
    assert main(["flow", "mixed", "--db", db]) == 0
    assert "ほかに定数に決まらない値" in capsys.readouterr().out


def test_text_output_labels_the_hints(db: str, capsys) -> None:
    capsys.readouterr()
    assert main(["flow", "spins", "--db", db]) == 0
    assert "終わらないループの可能性" in capsys.readouterr().out

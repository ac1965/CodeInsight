"""C/C++ の呼び出し元の実引数の追跡（dataflow --upstream と、読解カードの「呼び出し元が渡す引数」）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeinsight.cli import main

SOURCE = """\
#include <stdio.h>
int scale(int value, int factor) { return value * factor; }
int wrapper(int amount) { return scale(amount, 3); }
int top(int n) { return wrapper(n + 1) + scale(10, 2); }
int variadic(const char *fmt, ...) { return fmt != 0; }
int main(void) { printf("%d\\n", top(5)); return variadic("x", 1, 2); }
"""


@pytest.fixture
def db(tmp_path: Path, capsys) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "up.c").write_text(SOURCE, encoding="utf-8")
    path = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", path]) == 0
    capsys.readouterr()
    return path


def run(capsys, db: str, *argv: str) -> str:
    capsys.readouterr()
    assert main([*argv, "--db", db]) == 0
    return capsys.readouterr().out


def test_upstream_lists_actual_arguments_and_follows_forwarded_parameters(db: str, capsys) -> None:
    out = run(capsys, db, "dataflow", "scale", "value", "--upstream", "--depth", "2")
    assert "amount" in out and "10" in out  # wrapper が渡す amount と、top が渡す 10
    assert "wrapper(n + 1)" not in out  # 引数の式は、その呼び出しの実引数だけ
    assert "n + 1" in out  # amount は wrapper の引数そのものなので、その呼び出し元（top）の実引数 n + 1 までたどる
    factor = run(capsys, db, "dataflow", "scale", "factor", "--upstream")
    assert "3" in factor and "2" in factor


def test_upstream_only_for_parameters_and_unknown_callers_are_not_invented(db: str, capsys) -> None:
    capsys.readouterr()
    assert main(["dataflow", "scale", "value", "--upstream", "--db", db]) == 0
    assert main(["dataflow", "main", "--upstream", "--db", db]) == 0  # 変数名なし（引数の一覧）
    out = run(capsys, db, "dataflow", "top", "n", "--upstream")
    assert "main" in out and "5" in out  # main が top(5) を呼ぶ


def test_understand_card_shows_the_arguments_callers_actually_pass(db: str, capsys) -> None:
    out = run(capsys, db, "understand", "scale")
    assert "value" in out and "amount" in out and "factor" in out

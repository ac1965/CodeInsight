"""C/C++ のフィールド単位の読み書き（fields）。プロジェクト全体で、構造体のフィールドを読み書きしている箇所を、含む関数とともに示す。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeinsight.cli import main

HEADER = "struct Account { int balance; int limit; };\nstruct Other { int balance; };\n"
A_C = """\
#include "model.h"
void deposit(struct Account *a, int n) { a->balance += n; }
void reset(struct Account *a) { a->balance = 0; a->limit = 5; }
int read_balance(const struct Account *a) { return a->balance; }
int bump(struct Account *a) { a->balance++; return a->balance; }
void keep(struct Account *a, int **out) { *out = &a->balance; }
struct Account make(void) { struct Account x = { .balance = 7, .limit = 1 }; return x; }
void arr(struct Account *a) { int v = a->limit; (void)v; }
"""
B_C = """\
#include "model.h"
int other(struct Other *o) { o->balance = 1; return o->balance; }
void second(struct Account *a) { a->balance = 2; }
int unrelated(int balance) { return balance; }
"""


@pytest.fixture
def db(tmp_path: Path, capsys) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    for name, text in {"model.h": HEADER, "a.c": A_C, "b.c": B_C}.items():
        (root / name).write_text(text, encoding="utf-8")
    path = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", path]) == 0
    capsys.readouterr()
    return path


def fields_json(capsys, db: str, name: str) -> dict:
    capsys.readouterr()
    assert main(["fields", name, "--db", db, "--format", "json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_field_accesses_are_classified_with_their_functions(db: str, capsys) -> None:
    data = fields_json(capsys, db, "Account.balance")
    uses = {(u["path"], u["function"], u["mode"]) for u in data["uses"]}
    assert ("a.c", "deposit", "readwrite") in uses  # +=
    assert ("a.c", "reset", "write") in uses
    assert ("a.c", "read_balance", "read") in uses
    assert ("a.c", "bump", "readwrite") in uses and ("a.c", "bump", "read") in uses  # ++ と、return での読み取り
    assert ("a.c", "keep", "address") in uses  # &a->balance は、書き換えられうる
    assert ("a.c", "make", "init") in uses  # .balance = 7
    assert ("b.c", "second", "write") in uses  # 別のファイルの書き込みも、プロジェクト全体で拾う
    assert data["owners"] == ["Account"]


def test_owner_filter_separates_same_named_fields_and_unqualified_name_warns(db: str, capsys) -> None:
    other = fields_json(capsys, db, "Other.balance")
    assert {(u["function"], u["mode"]) for u in other["uses"]} == {("other", "write"), ("other", "read")}  # Account.balance の使用は含めない
    both = fields_json(capsys, db, "balance")
    assert both["owners"] == ["Account", "Other"]
    capsys.readouterr()
    assert main(["fields", "balance", "--db", db]) == 0
    assert "複数の構造体にあります" in capsys.readouterr().out


def test_text_output_groups_writers_and_readers_and_notes_limits(db: str, capsys) -> None:
    capsys.readouterr()
    assert main(["fields", "Account.limit", "--db", db]) == 0
    captured = capsys.readouterr()
    assert "書き込み系 2件 / 読み取り 1件" in captured.out  # reset の書き込み、make の初期化、arr の読み取り
    assert "マクロの内部" in captured.err and "位置による初期化" in captured.err  # 追えないものを明記する


def test_unknown_field_is_reported_not_invented(db: str, capsys) -> None:
    capsys.readouterr()
    assert main(["fields", "Account.no_such", "--db", db]) == 0
    assert "確認できませんでした" in capsys.readouterr().out

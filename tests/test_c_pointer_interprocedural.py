"""関数をまたぐポインタの受け渡し: ポインタ引数に、呼び出し元が渡している先（グローバル・static変数）を、解決済みの呼び出しからたどる。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeinsight.cli import main

SOURCE = """\
int g_total;
static int s_buf[4];
int g_other;
void bump(int *out, int v) { *out += v; }
void relay(int *p, int v) { bump(p, v); }
void top(void) { relay(&g_total, 1); relay(s_buf, 2); }
void pick(int c, int *x) { int *q = c ? &g_other : x; bump(q, 3); }
void unknown_source(int *(*get)(void)) { bump(get(), 4); }
void local_only(void) { int x = 0; bump(&x, 5); }
int main(void) { top(); pick(1, &g_total); local_only(); return 0; }
"""


@pytest.fixture
def db(tmp_path: Path, capsys) -> str:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "p.c").write_text(SOURCE, encoding="utf-8")
    path = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", path]) == 0
    capsys.readouterr()
    return path


def state_json(capsys, db: str, name: str) -> dict:
    capsys.readouterr()
    assert main(["state", name, "--db", db, "--format", "json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_callers_pass_globals_and_forwarded_parameters_are_followed(db: str, capsys) -> None:
    mutation = next(m for m in state_json(capsys, db, "bump")["parameter_mutations"] if m["parameter"] == "out")
    destinations = mutation["caller_destinations"]
    by_caller = {}
    for item in destinations:
        by_caller.setdefault(item["caller"], []).append(item)
    relay_targets = {t["name"] for item in by_caller["top"] for t in item["targets"]}
    assert relay_targets == {"g_total", "s_buf"}  # relay が bump へ転送する p は、top が渡す &g_total と s_buf（2段）
    assert any(item["via"] for item in by_caller["top"])  # 転送の経路を示す
    pick = by_caller["pick"][0]
    assert {t["name"] for t in pick["targets"]} == {"g_other"} and pick["unknown"] is False  # c ? &g_other : x の x は、pick の引数（転送先で別に扱う）


def test_unknown_values_are_flagged_and_local_pointees_are_not_state(db: str, capsys) -> None:
    items = next(m for m in state_json(capsys, db, "bump")["parameter_mutations"] if m["parameter"] == "out")["caller_destinations"]
    unknown = [i for i in items if i["caller"] == "unknown_source"]
    assert unknown and unknown[0]["unknown"] is True and unknown[0]["targets"] == []  # 関数の戻り値は、追えない
    assert not any(i["caller"] == "local_only" for i in items)  # ローカル変数のアドレスは、状態として示さない


def test_text_output_and_understand_card_show_the_destinations(db: str, capsys) -> None:
    capsys.readouterr()
    assert main(["state", "bump", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "に、呼び出し元が渡している先" in out and "g_total（グローバル変数）" in out and "s_buf（静的変数）" in out
    assert main(["understand", "bump", "--db", db]) == 0
    assert "引数 out に呼び出し元が渡している先" in capsys.readouterr().out

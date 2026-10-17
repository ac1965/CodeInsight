"""C++ の関数単位の解析（制御フロー・例外・状態・データフロー・リスク・制御フロー図）。C と同じ Clang AST の近似に、
try/catch/throw・範囲for・this 経由の状態を加える。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeinsight.cli import main

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cpp_flow"


@pytest.fixture(scope="module")
def db(tmp_path_factory) -> str:
    path = str(tmp_path_factory.mktemp("cpp") / "d.sqlite")
    assert main(["analyze", str(FIXTURE), "--db", path]) == 0
    return path


def run(capsys, db: str, *argv: str) -> str:
    capsys.readouterr()
    assert main([*argv, "--db", db]) == 0
    return capsys.readouterr().out


def test_control_flow_covers_range_for_try_catch_and_throw(db: str, capsys) -> None:
    out = run(capsys, db, "flow", "geo::sum_areas")
    assert "for  (auto *s : shapes)" in out and "try" in out
    assert "except  const std::out_of_range &" in out and "except  ..." in out and "raise  throw" in out
    assert "循環的複雑度 4" in out  # for(1) + catch×2 + 1
    scale = run(capsys, db, "flow", "geo::Shape::scale", "--format", "json")
    assert "invalid_argument" in scale  # メソッド（名前空間・クラスの中）も解析できる


def test_exceptions_list_throws_mark_local_catches_and_follow_calls(db: str, capsys) -> None:
    thrower = json.loads(run(capsys, db, "exceptions", "errs::thrower", "--format", "json"))
    assert thrower["language"] == "cpp" and [e["kind"] for e in thrower["exits"]] == ["throw"]
    local = json.loads(run(capsys, db, "exceptions", "errs::local_catch", "--format", "json"))
    assert local["exits"][0]["caught_locally"] is True and "捕捉されうる" in local["exits"][0]["detail"]
    top = json.loads(run(capsys, db, "exceptions", "errs::top", "--format", "json", "--depth", "4"))
    chain = [p for p in top["propagated"] if p["kind"] == "throw"]
    assert chain and [c["symbol"] for c in chain[0]["chain"]] == ["errs::middle", "errs::thrower"]  # 呼び出し先を経由した送出
    rethrow = run(capsys, db, "exceptions", "geo::sum_areas")
    assert "再送出" in rethrow
    guarded = json.loads(run(capsys, db, "exceptions", "errs::guarded", "--format", "json"))
    assert [p for p in guarded["propagated"] if p["kind"] == "throw"]  # 呼び出し元の catch は考慮しない（近似であることを明示している）


def test_state_reports_this_members_globals_and_static_members(db: str, capsys) -> None:
    out = run(capsys, db, "state", "geo::Shape::scale")
    assert "g_total" in out and "w_ *= k" in out and "このオブジェクトのメンバ（this）" in out
    ctor = run(capsys, db, "state", "geo::Shape::Shape")
    assert "count_" in ctor  # 静的メンバ（クラス共有の状態）の書き込み


def test_dataflow_works_inside_cpp_functions(db: str, capsys) -> None:
    out = run(capsys, db, "dataflow", "geo::sum_areas", "total")
    assert "L29 decl_init" in out and "augassign" in out and "戻り値として返す" in out


def test_risks_flag_swallowed_exceptions_in_cpp(db: str, capsys) -> None:
    out = run(capsys, db, "risks", "--format", "json")
    findings = [f for f in json.loads(out)["findings"] if f["path"] == "shape.cpp"]
    # 本体が空の catch だけが対象。catch (...) でも再送出している sum_areas と、本体のある local_catch は対象外
    assert [(f["rule"], f["line"]) for f in findings] == [("swallowed-exception", 52)]


def test_control_flow_graph_has_try_catch_and_throw_edges(db: str, capsys) -> None:
    out = run(capsys, db, "graph", "flow", "--root", "geo::sum_areas", "--format", "mermaid")
    assert "catch (const std::out_of_range &)" in out and "送出されれば" in out and "例外で終了" in out
    assert "(+2文)" not in out  # try・範囲for を、ただの文として1ブロックにまとめない


def test_understand_card_works_for_cpp_methods(db: str, capsys) -> None:
    out = run(capsys, db, "understand", "geo::Shape::scale")
    assert "このオブジェクトのメンバ（this）を変更する" in out and "g_total" in out

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


EXTRA = """\
#include <stdexcept>
struct Cell {
    int w_; int h_; static int n_;
    Cell(int w, int h) : w_(w), h_(h * 2) { n_++; }
    void run(int k) noexcept {
        auto inc = [this, &k](int d) { w_ += d; k++; if (d < 0) throw std::runtime_error("neg"); return d; };
        inc(1);
    }
    int safe() noexcept { throw 1; }
    int guarded() noexcept { try { throw 1; } catch (int) { return 0; } return 1; }
    int plain() { return 3; }
};
int Cell::n_ = 0;
int caller() { Cell c(1, 2); return c.safe(); }
"""


@pytest.fixture(scope="module")
def extra_db(tmp_path_factory) -> str:
    root = tmp_path_factory.mktemp("cpp_extra") / "proj"
    root.mkdir()
    (root / "cell.cpp").write_text(EXTRA, encoding="utf-8")
    path = str(tmp_path_factory.mktemp("cpp_extra_db") / "d.sqlite")
    assert main(["analyze", str(root), "--db", path]) == 0
    return path


def test_constructor_initializer_list_writes_are_state_and_dataflow(extra_db: str, capsys) -> None:
    out = run(capsys, extra_db, "state", "Cell::Cell")
    assert "初期化子リスト: w_(w)" in out and "初期化子リスト: h_(h * 2)" in out and "このオブジェクトのメンバ（this）" in out
    flows = run(capsys, extra_db, "dataflow", "Cell::Cell", "h")
    assert "this->h_" in flows and "使用" in flows  # 引数 h が、初期化子リストで使われ、メンバへ流れる


def test_lambda_bodies_are_not_part_of_the_enclosing_function(extra_db: str, capsys) -> None:
    flow = run(capsys, extra_db, "flow", "Cell::run")
    assert "lambda" in flow and "[this, &k](int d)" in flow
    assert "raise" not in flow and "if" not in flow.split("指標")[0].replace("lambda", "")  # ラムダの throw・if は、run の構造ではない
    state = run(capsys, extra_db, "state", "Cell::run")
    assert "ラムダの中。呼ばれたときに実行" in state and "w_ += d" in state
    exits = json.loads(run(capsys, extra_db, "exceptions", "Cell::run", "--format", "json"))
    throw = next(e for e in exits["exits"] if e["kind"] == "throw")
    assert throw["in_lambda"] is True and "ラムダの中" in throw["detail"]
    assert exits["propagated"] == []  # ラムダの throw は、run を呼んだ側へ出ない


def test_noexcept_throw_becomes_a_terminate_not_a_propagating_exception(extra_db: str, capsys) -> None:
    exits = json.loads(run(capsys, extra_db, "exceptions", "Cell::safe", "--format", "json"))
    kinds = {(e["kind"], e["noexcept"]) for e in exits["exits"]}
    assert ("throw", True) in kinds and any(e["kind"] == "terminate" and "std::terminate" in e["detail"] for e in exits["exits"])
    caller = json.loads(run(capsys, extra_db, "exceptions", "caller", "--format", "json"))
    assert [p["kind"] for p in caller["propagated"]] == ["terminate"]  # 呼び出し元からは、例外ではなく、プロセスの終了として見える（throw としては伝播しない）
    flow = run(capsys, extra_db, "flow", "Cell::safe")
    assert "noexcept の関数の throw" in flow
    assert json.loads(run(capsys, extra_db, "exceptions", "Cell::guarded", "--format", "json"))["exits"][0]["noexcept"] is False  # 同じ関数の catch で捕捉されるものは、外へ出ない

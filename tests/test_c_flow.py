from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from codeinsight.analysis import c_flow_analysis as cf
from codeinsight.analysis.c_analyzer import CAnalyzer
from codeinsight.analysis.language_adapter import FileAnalysis, SourceUnit
from codeinsight.cli import main
from codeinsight.infrastructure.analysis_repository import AnalysisRepository

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "c_flow"


@pytest.fixture(scope="module")
def parsed():
    path = (FIXTURE / "flow.c").resolve()
    data = path.read_bytes()
    unit = SourceUnit(str(uuid.uuid4()), path, "flow.c", data)
    translation_unit = CAnalyzer().parse(unit, FileAnalysis())
    assert translation_unit is not None

    def function(name: str) -> cf.CFunction:
        import clang.cindex as ci

        start = next(c.extent.start.line for c in translation_unit.cursor.get_children()
                     if c.kind == ci.CursorKind.FUNCTION_DECL and c.spelling == name and c.is_definition())
        found = cf.find_function(translation_unit, str(path), name, start, data)
        assert found is not None
        return found

    return function


def test_control_flow_lists_structures_with_lines_and_metrics(parsed) -> None:
    summary = cf.analyze_control_flow(parsed("process"))
    kinds = [(i.kind, i.line, i.depth) for i in summary.items]
    assert ("if", 21, 0) in kinds and ("return", 22, 1) in kinds
    assert ("for", 24, 0) in kinds and ("if", 25, 1) in kinds and ("elif", 28, 1) in kinds and ("else", 30, 1) in kinds
    assert ("switch", 34, 0) in kinds and ("case", 35, 1) in kinds and ("default", 40, 1) in kinds and ("goto", 41, 2) in kinds
    assert ("label", 48, 0) in kinds and ("exit", 50, 0) in kinds
    # 循環的複雑度: if(1+||) + for(1) + if(1+&&) + elif(1) + case×2 + 1 = 9。マクロ(strcpy)が展開する三項演算子は数えない
    assert summary.metrics["cyclomatic"] == 9 and summary.metrics["branches"] == 3 and summary.metrics["gotos"] == 1
    assert {(h.kind, h.line) for h in summary.hints} == {("error-return", 22), ("fallthrough", 37)}


def test_ternary_and_loops_are_counted(parsed) -> None:
    summary = cf.analyze_control_flow(parsed("factorial"))
    assert [i.kind for i in summary.items] == ["while", "do", "return", "ternary"]
    assert summary.metrics["cyclomatic"] == 4 and summary.metrics["loops"] == 2


def test_dataflow_tracks_definitions_uses_and_stores(parsed) -> None:
    variables = cf.analyze_variables(parsed("process"))
    total = variables["total"]
    assert [(d.line, d.how) for d in total.definitions] == [(18, "decl_init"), (26, "augassign"), (36, "assign"), (38, "augassign")]
    assert {(f.kind, f.line) for f in total.flows} >= {("attr_store", 43), ("subscript_store", 44), ("return", 47)}
    assert variables["cfg"].is_param and variables["cfg"].scope == "param"
    assert variables["g_counter"].scope == "global" and variables["s_cache"].scope == "static"
    assert ("call_arg", 23, "strcpy") in {(f.kind, f.line, f.target) for f in variables["input"].flows}  # マクロ展開後の名前ではなく、書かれた名前


def test_state_reports_global_static_and_parameter_writes(parsed) -> None:
    state = cf.analyze_state(parsed("process"))
    writes = {(a.name, a.scope, a.mode, a.line) for a in state.accesses if a.mode == "write"}
    assert writes == {("g_counter", "global", "write", 27), ("s_cache", "static", "write", 44)}
    assert [(m.parameter, m.how, m.line) for m in state.parameter_mutations] == [("cfg", "field_store", 43)]


def test_exits_and_environment(parsed) -> None:
    assert [(e.kind, e.line) for e in cf.analyze_exits(parsed("process"))] == [("error_return", 22), ("terminate", 50)]
    assert cf.environment_reads(parsed("home")) == [(58, "HOME", 'getenv("HOME")')]


def test_risks_detect_unsafe_patterns_without_flagging_clean_code(parsed) -> None:
    rules = {(h.rule, h.line) for h in cf.scan_risks(parsed("process"))}
    assert rules == {("unchecked-alloc", 20), ("unsafe-libc", 23), ("fallthrough", 37), ("format-string", 45)}
    assert {(h.rule, h.line) for h in cf.scan_risks(parsed("run_cmd"))} == {("command-exec", 54)}
    assert cf.scan_risks(parsed("factorial")) == []  # 問題の手がかりが無い関数


def test_parameters_and_return_type(parsed) -> None:
    function = parsed("process")
    assert [(p.name, p.type, p.is_pointer) for p in cf.parameters(function)] == [
        ("cfg", "struct Config *", True), ("input", "const char *", True), ("mode", "int", False)]
    assert cf.return_type(function) == "int" and cf.return_statements(function) == [(22, "-1"), (47, "total")]


# --- CLI ---


@pytest.fixture
def db(tmp_path: Path, capsys) -> str:
    path = str(tmp_path / "c.sqlite")
    assert main(["analyze", str(FIXTURE), "--db", path]) == 0
    capsys.readouterr()
    return path


def _run(capsys, *argv: str):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_flow_dataflow_exceptions_state_for_c(db: str, capsys) -> None:
    code, out, err = _run(capsys, "flow", "process", "--db", db)
    assert code == 0 and "L24" in out and "goto" in out and "循環的複雑度 9" in out and "case の落ち込み" in out
    assert "実行時にどの経路を通るかは示しません" in err  # 静的な構造であることの注記

    code, out, _ = _run(capsys, "dataflow", "process", "--db", db)
    assert "引数" in out and "グローバル変数" in out and "静的変数" in out
    code, out, err = _run(capsys, "dataflow", "process", "total", "--db", db)
    assert "L43 cfg->limit に書き込む" in out and "L47 戻り値として返す" in out
    assert "エイリアス" in err  # 追えない範囲を明示する

    code, out, _ = _run(capsys, "exceptions", "process", "--db", db)
    assert "Cには例外が無い" in out and "return -1" in out and "exit()" in out

    code, out, _ = _run(capsys, "state", "process", "--db", db)
    assert "g_counter" in out and "s_cache" in out and "引数 cfg" in out


def test_cli_dataflow_follows_arguments_into_resolved_callees(db: str, capsys) -> None:
    code, out, _ = _run(capsys, "dataflow", "process", "i", "--db", db)
    assert code == 0 and "check の仮引数 v" in out  # 解決済みの呼び出しは、呼び出し先の仮引数まで追う
    code, out, _ = _run(capsys, "dataflow", "process", "input", "--db", db)
    assert "外部" in out  # strcpy / printf はプロジェクト外


def test_cli_understand_and_risks_for_c(db: str, capsys) -> None:
    code, out, _ = _run(capsys, "understand", "process", "--db", db)
    assert code == 0 and "引数 cfg: struct Config *" in out and "グローバル変数 g_counter を書き込む" in out
    assert "プロセスを終了する呼び出し: L50" in out and "リスクの手がかり: L23" in out
    assert "エイリアス" in out  # 解析の限界を末尾に明示
    code, out, _ = _run(capsys, "risks", "--format", "json", "--db", db)
    rules = {f["rule"] for f in json.loads(out)["findings"]}
    assert {"unsafe-libc", "command-exec", "format-string", "unchecked-alloc", "fallthrough"} <= rules


def test_stale_source_is_refused_for_c_function_analysis(tmp_path: Path, capsys) -> None:
    import shutil

    root = tmp_path / "proj"
    shutil.copytree(FIXTURE, root)
    path = str(tmp_path / "s.sqlite")
    assert main(["analyze", str(root), "--db", path]) == 0
    capsys.readouterr()
    (root / "flow.c").write_text((root / "flow.c").read_text() + "\n/* changed */\n")
    code, _, err = _run(capsys, "flow", "process", "--db", path)
    assert code != 0 and "解析後に変更されています" in err  # 古い位置で誤った結果を出さない


def test_compile_commands_directory_is_saved_with_the_project(tmp_path: Path, capsys) -> None:
    build = tmp_path / "build"
    build.mkdir()
    (build / "compile_commands.json").write_text("[]")
    path = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(FIXTURE), "--compile-commands", str(build), "--db", path]) in (0, 1)
    capsys.readouterr()
    with AnalysisRepository(Path(path)) as repository:
        project = repository.list_projects()[0]
    assert project.configuration.compile_commands_dir == str(build.resolve())  # 関数単位の解析で、解析時と同じ設定を使うため


def test_operators_after_a_macro_call_are_still_counted(parsed) -> None:
    # 条件の先頭がマクロ呼び出し（IS_SPACE(c) || ...）のとき、libclangは演算子のトークンを返さない。ソースから読み取る
    summary = cf.analyze_control_flow(parsed("classify"))
    # if(1) + 条件の || が2つ + 三項演算子（条件がマクロ呼び出し）1 + 1 = 5。マクロの内部の || は数えない
    assert summary.metrics["cyclomatic"] == 5 and summary.metrics["ternaries"] == 1


def test_unary_operators_expanded_from_macros_do_not_crash(parsed) -> None:
    # マクロが展開した単項演算子はトークンが空になる（IndexError の原因だった）。全関数を解析できる
    for name in ("process", "run_cmd", "home", "factorial", "classify"):
        function = parsed(name)
        cf.analyze_control_flow(function)
        cf.analyze_variables(function)
        cf.analyze_state(function)
        cf.scan_risks(function)

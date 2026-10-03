from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codeinsight.application import (
    FlowAnalysisError,
    FlowService,
    NavigationService,
    RiskService,
)
from codeinsight.cli import main


def _setup(analyzed, root: Path):
    repo, project, _ = analyzed(root)
    nav = NavigationService(repo)
    index = nav.load_index(project).materialize()
    return project, nav, index, FlowService(nav)


def _symbol(nav, index, name):
    return nav.resolve_symbol(index, name).symbol


def test_control_flow_through_the_service_places_else_on_its_own_line(
    analyzed, python_flow_dir: Path
) -> None:
    project, nav, index, flow = _setup(analyzed, python_flow_dir)
    summary = flow.control_flow(project, index, _symbol(nav, index, "flow.fetch"))
    else_item = next(i for i in summary.items if i.kind == "else")
    source_line = (python_flow_dir / "flow.py").read_text().splitlines()[else_item.line - 1]
    assert source_line.strip() == "else:"


def test_analysis_refuses_to_run_on_a_stale_source(analyzed, python_flow_dir: Path, tmp_path: Path) -> None:
    root = tmp_path / "copy"
    shutil.copytree(python_flow_dir, root)
    project, nav, index, flow = _setup(analyzed, root)
    symbol = _symbol(nav, index, "flow.fetch")

    (root / "flow.py").write_text("# 変更\n" + (root / "flow.py").read_text())
    with pytest.raises(FlowAnalysisError, match="解析後に変更"):
        flow.control_flow(project, index, symbol)  # 行がずれて誤った関数を解析しないよう、実行しない


def test_flow_analysis_rejects_non_python_symbols(analyzed, c_callgraph_dir: Path) -> None:
    project, nav, index, flow = _setup(analyzed, c_callgraph_dir)
    with pytest.raises(FlowAnalysisError, match="Pythonのみ"):
        flow.control_flow(project, index, _symbol(nav, index, "main"))


def test_variable_trace_follows_arguments_into_callees(analyzed, python_flow_dir: Path) -> None:
    project, nav, index, flow = _setup(analyzed, python_flow_dir)
    trace = flow.trace_variable(project, index, _symbol(nav, index, "flow.fetch"), "data", depth=3)

    call = next(f for f in trace.flows if f.kind == "call_arg")
    assert call.callee_param == "payload" and call.callee.qualified_name == "flow.transform"
    # transform の payload → out（内包表記に含めて代入）→ collect の items → items.append
    (derived,) = [f for f in call.child.flows if f.kind == "derive"]
    assert derived.description.startswith("out へ")
    collect_flow = next(f for f in derived.child.flows if f.kind == "call_arg")
    assert collect_flow.callee_param == "items"
    assert any(f.kind == "method_call" for f in collect_flow.child.flows)
    assert any(f.kind == "return" for f in derived.child.flows)


def test_variable_trace_depth_limits_how_far_it_follows(analyzed, python_flow_dir: Path) -> None:
    project, nav, index, flow = _setup(analyzed, python_flow_dir)
    shallow = flow.trace_variable(project, index, _symbol(nav, index, "flow.fetch"), "data", depth=0)
    call = next(f for f in shallow.flows if f.kind == "call_arg")
    assert call.child is None  # 深さ0では呼び出し先へ入らない


def test_variable_trace_reports_unresolved_callees_honestly(analyzed, python_flow_dir: Path) -> None:
    project, nav, index, flow = _setup(analyzed, python_flow_dir)
    trace = flow.trace_variable(project, index, _symbol(nav, index, "flow.fetch"), "url", depth=2)
    (call,) = [f for f in trace.flows if f.kind == "call_arg"]  # requests.get(url, ...)
    assert call.child is None and call.status  # 外部ライブラリなので、仮引数は追えない


def test_unknown_variable_and_non_parameter_upstream_are_errors(analyzed, python_flow_dir: Path) -> None:
    project, nav, index, flow = _setup(analyzed, python_flow_dir)
    symbol = _symbol(nav, index, "flow.fetch")
    with pytest.raises(FlowAnalysisError, match="見つかりません"):
        flow.trace_variable(project, index, symbol, "nope")
    with pytest.raises(FlowAnalysisError, match="ありません"):
        flow.upstream(project, index, symbol, "nope")


def test_upstream_finds_the_actual_arguments_passed_by_callers(analyzed, python_flow_dir: Path) -> None:
    project, nav, index, flow = _setup(analyzed, python_flow_dir)
    nodes = flow.upstream(project, index, _symbol(nav, index, "flow.transform"), "payload", depth=2)
    arguments = {(n.caller.qualified_name, n.argument) for n in nodes}
    assert arguments == {("flow.fetch", "data"), ("flow.uncaught", "[1]")}


def test_class_and_module_state_through_the_service(analyzed, python_flow_dir: Path) -> None:
    project, nav, index, flow = _setup(analyzed, python_flow_dir)
    accesses = flow.class_state(project, index, _symbol(nav, index, "flow.Counter"))
    assert {(a.attribute, a.method, a.mode) for a in accesses} >= {("count", "incr", "write"), ("items", "incr", "mutate")}
    writes = flow.module_state(project, index, _symbol(nav, index, "flow"))
    assert {w.name for w in writes} == {"REGISTRY", "CACHE"}
    with pytest.raises(FlowAnalysisError, match="クラスではありません"):
        flow.class_state(project, index, _symbol(nav, index, "flow.fetch"))


def test_exception_propagation_respects_handlers_and_class_hierarchy(analyzed, python_flow_dir: Path) -> None:
    project, nav, index, flow = _setup(analyzed, python_flow_dir)

    def propagated(name: str) -> dict[str, list[str]]:
        report = flow.exceptions(project, index, _symbol(nav, index, name))
        return {e.exception: [s.qualified_name for s, _ in e.chain] for e in report.propagated}

    # caller は NotFound を捕捉するが、その基底クラス RetryError（collectが送出）は捕捉しない
    assert propagated("flow.caller") == {
        "RetryError": ["flow.fetch", "flow.transform", "flow.collect"]
    }
    # fetch は自身の raise（NotFound）と、transform→collect 経由の RetryError が出る
    assert set(propagated("flow.fetch")) == {"NotFound", "RetryError"}
    assert set(propagated("flow.uncaught")) == {"RetryError"}
    report = flow.exceptions(project, index, _symbol(nav, index, "flow.fetch"))
    assert [h.types for h in report.handlers if h.swallowed] == [("Exception",)]
    assert report.unresolved_calls >= 1  # resp.json() などは特定できない


def test_exceptions_that_would_be_caught_are_reported_as_caught_inside(tmp_path: Path, analyzed) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "m.py").write_text(
        "def f():\n    try:\n        raise FileNotFoundError('x')\n    except OSError:\n        return 1\n"
    )
    project, nav, index, flow = _setup(analyzed, root)
    report = flow.exceptions(project, index, _symbol(nav, index, "m.f"))
    assert report.propagated == []  # 組み込みの継承関係で OSError が捕捉する
    assert report.caught_inside == [(3, "FileNotFoundError", ("OSError",))]


def test_risk_scan_finds_pattern_based_clues_with_locations(analyzed, python_flow_dir: Path) -> None:
    project, nav, index, _ = _setup(analyzed, python_flow_dir)
    findings, skipped = RiskService().scan(project, index)
    by_rule = {(f.rule, f.path, f.symbol) for f in findings}

    assert skipped == []
    assert ("shell-true", "risky.py", "risky.run") in by_rule
    assert ("eval-exec", "risky.py", "risky.run") in by_rule
    assert ("bare-except", "risky.py", "risky.swallow") in by_rule
    assert ("swallowed-exception", "flow.py", "flow.fetch") in by_rule
    assert ("mutable-default", "flow.py", "flow.fetch") in by_rule
    assert ("blocking-in-async", "risky.py", "risky.wait") in by_rule
    assert ("unsafe-deserialization", "risky.py", "risky.load") in by_rule
    assert ("broad-raise", "risky.py", "risky.load") in by_rule
    assert any(f.rule == "todo-marker" and "検証を追加" in f.detail for f in findings)
    assert findings[0].severity == "high"  # 重大度の高い順
    only, _ = RiskService().scan(project, index, {"eval-exec"})
    assert {f.rule for f in only} == {"eval-exec"}


def test_risk_scan_skips_files_changed_after_analysis(analyzed, python_flow_dir: Path, tmp_path: Path) -> None:
    root = tmp_path / "copy"
    shutil.copytree(python_flow_dir, root)
    project, nav, index, _ = _setup(analyzed, root)
    (root / "risky.py").write_text("x = 1\n")
    findings, skipped = RiskService().scan(project, index)
    assert skipped == ["risky.py"] and all(f.path != "risky.py" for f in findings)


# --- CLI ---


def _run(capsys, db: str, *argv: str):
    code = main([*argv, "--db", db])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def flow_db(tmp_path: Path, python_flow_dir: Path, capsys) -> str:
    db = str(tmp_path / "f.sqlite")
    assert main(["analyze", str(python_flow_dir), "--db", db]) == 0
    capsys.readouterr()
    return db


def test_cli_flow_dataflow_state_exceptions_and_risks(flow_db: str, capsys) -> None:
    code, out, _ = _run(capsys, flow_db, "flow", "flow.fetch")
    assert code == 0 and "for  attempt in range(retries)" in out
    assert "握りつぶし（何もしない）" in out and "タイムアウト指定: requests.get(timeout=10)" in out
    assert "循環的複雑度 6" in out

    code, out, _ = _run(capsys, flow_db, "dataflow", "flow.fetch")
    assert "引数 url" in out and "変数 data" in out

    code, out, _ = _run(capsys, flow_db, "dataflow", "flow.fetch", "data", "--depth", "3")
    assert "flow.transform の仮引数 payload" in out and "flow.collect の仮引数 items" in out

    code, out, _ = _run(capsys, flow_db, "dataflow", "flow.transform", "payload", "--upstream")
    assert "flow.fetch  ← data" in out and "flow.uncaught  ← [1]" in out

    code, out, _ = _run(capsys, flow_db, "state", "flow.Counter")
    assert "count  — 状態が変化する" in out and "変更(破壊的メソッド/添字代入): incr" in out

    code, out, _ = _run(capsys, flow_db, "state", "flow")
    assert "REGISTRY" in out and "global宣言で再代入" in out

    code, out, err = _run(capsys, flow_db, "exceptions", "flow.caller")
    assert "RetryError" in out and "経路: flow.caller →(L50) flow.fetch" in out and "NotFound" not in out
    assert "呼び出し先を特定できない" in err

    code, out, _ = _run(capsys, flow_db, "risks", "--min-severity", "high")
    assert "shell-true" in out and "eval-exec" in out and "bare-except" not in out
    code, out, _ = _run(capsys, flow_db, "risks", "--rule", "todo-marker", "--format", "json")
    assert "todo-marker" in out


def test_cli_reports_clear_errors(flow_db: str, tmp_path: Path, capsys) -> None:
    code, _, err = _run(capsys, flow_db, "state", "flow.fetch")
    assert code == 1 and "state はクラスまたはモジュール" in err
    code, _, err = _run(capsys, flow_db, "dataflow", "flow.fetch", "nope")
    assert code == 1 and "見つかりません" in err
    code, _, err = _run(capsys, flow_db, "dataflow", "flow.fetch", "retries", "--upstream")
    assert "--upstream は引数にのみ" not in err  # retries は引数なので許可される
    code, _, err = _run(capsys, flow_db, "dataflow", "flow.fetch", "data", "--upstream")
    assert code == 1 and "--upstream は引数にのみ" in err

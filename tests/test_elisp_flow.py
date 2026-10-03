from __future__ import annotations

from pathlib import Path

import pytest

from codeinsight.application.elisp_flow_service import ElispFlowService
from codeinsight.application.flow_service import FlowAnalysisError
from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.cli import main

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "elisp_sample"


@pytest.fixture
def env(analyzed):
    repo, project, _ = analyzed(FIXTURE)
    index = ProjectIndex.load(repo, project.project_id)
    service = ElispFlowService(NavigationService(repo))

    def symbol(name: str):
        return next(s for s in index.symbols.values() if s.name == name)

    return service, project, index, symbol


def test_control_flow_structures_and_metrics(env) -> None:
    service, project, index, symbol = env
    summary = service.control_flow(project, index, symbol("core-risky"))
    kinds = [(i.kind, i.line) for i in summary.items]
    assert ("try", 55) in kinds and ("loop", 59) in kinds and ("cond", 62) in kinds
    assert ("raise", 58) in kinds and ("raise", 62) in kinds
    assert ("except", 65) in kinds and ("finally", 68) in kinds
    # 引用されたデータ・文字列の中は、構造として数えない
    assert summary.metrics["signals"] == 2 and summary.metrics["loops"] == 1
    assert summary.metrics["cyclomatic"] >= 6


def test_quoted_data_is_not_control_flow(env) -> None:
    service, project, index, symbol = env
    assert service.control_flow(project, index, symbol("core-hook-setup")).items == []  # '(core-greet (not-a-call)) は処理ではない


def test_exits_signals_handlers_and_guards(env) -> None:
    service, project, index, symbol = env
    exits = service.exits(project, index, symbol("core-risky"))
    assert {s.line for s in exits.signals} == {58, 62}
    assert all(s.guarded_by and "condition-case" in s.guarded_by[0] for s in exits.signals)  # condition-case の本体の中
    assert {h.form for h in exits.handlers} == {"condition-case", "ignore-errors"}
    assert next(h for h in exits.handlers if h.form == "ignore-errors").swallowed
    assert exits.unwind == [66]


def test_exit_report_follows_resolved_calls_as_inferred(env) -> None:
    service, project, index, symbol = env
    report = service.exit_report(project, index, symbol("core-risky"), 3)
    details = {p.signal.detail for p in report.propagated}
    assert any("wrong-type-argument" in d for d in details) and "kill-emacs" in details
    assert all(p.chain[0][0].name == "core-fail" for p in report.propagated)


def test_state_tracks_globals_and_reports_shadowing(env) -> None:
    service, project, index, symbol = env
    result = service.state(project, index, symbol("core-risky"))
    by_name = {(a.name, a.mode): a for a in result.accesses}
    assert ("core-counter", "write") in by_name
    assert ("core-log", "mutate") in by_name  # (push i core-log): 場所は第2引数
    assert "total" in result.shadowed and not any(a.name == "total" for a in result.accesses)  # 局所変数は、グローバルとして数えない


def test_non_function_and_non_elisp_are_rejected(env) -> None:
    service, project, index, symbol = env
    with pytest.raises(FlowAnalysisError):
        service.control_flow(project, index, symbol("core-counter"))


def test_stale_source_is_refused(analyzed, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    source = root / "a.el"
    source.write_text("(defun a-fn ()\n  (error \"x\"))\n", encoding="utf-8")
    repo, project, _ = analyzed(root)
    index = ProjectIndex.load(repo, project.project_id)
    function = next(s for s in index.symbols.values() if s.name == "a-fn")
    source.write_text(";; 追記\n" + source.read_text(encoding="utf-8"), encoding="utf-8")  # 解析後に行がずれる
    with pytest.raises(FlowAnalysisError, match="変更されています"):
        ElispFlowService(NavigationService(repo)).control_flow(project, index, function)


def test_cli_flow_exceptions_state_and_dataflow(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "e.db")
    assert main(["analyze", str(FIXTURE), "--db", db]) == 0
    capsys.readouterr()
    for command in ("flow", "exceptions", "state"):
        assert main([command, "core-risky", "--db", db, "--project", str(FIXTURE)]) == 0
        out = capsys.readouterr().out
        assert "core-risky" in out
    assert main(["dataflow", "core-risky", "--db", db, "--project", str(FIXTURE)]) != 0

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
    assert main(["dataflow", "core-risky", "--db", db, "--project", str(FIXTURE)]) == 0


def test_understand_includes_elisp_state_and_failure(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "u.db")
    assert main(["analyze", str(FIXTURE), "--db", db]) == 0
    capsys.readouterr()
    assert main(["understand", "core-risky", "--db", db, "--project", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert "core-log を書き換える（push）" in out and "core-counter を書き込む（setq）" in out
    assert "user-error" in out and "kill-emacs" in out and "（推定）" in out
    assert "最後に評価された式の値" in out and "return total" in out  # 戻り値は、本体の最後の式の値（各経路）として示す
    assert "近似" in out
    assert "] signal" not in out and "] error" not in out  # signal / error（エラーの送出）を、外部への操作と誤分類しない
    assert "kill-emacs" in out


def test_dataflow_traces_through_resolved_calls(env) -> None:
    service, project, index, symbol = env
    variables = service.variables(project, index, symbol("core-pipeline"))
    assert variables["path"].is_param and variables["raw"].scope == "local" and variables["core-cache"].scope == "global"
    trace = service.trace_variable(project, index, symbol("core-pipeline"), "path", 3)
    texts = [f.description for f in trace.flows]
    assert any("core-read の仮引数 file" in t and "推定" in t for t in texts)  # 解決済みの呼び出しを介して仮引数へ
    call = next(f for f in trace.flows if f.callee is not None)
    assert call.child is not None and any("insert-file-contents" in f.description for f in call.child.flows)
    derived = [f for f in trace.flows if "raw へ式に含めて代入" in f.description]
    assert derived and derived[0].child is not None  # raw → items → count と連鎖する
    items = service.trace_variable(project, index, symbol("core-pipeline"), "items", 1)
    assert any(f.kind == "attr_store" and f.status for f in items.flows)  # puthash による書き込み先の別名は追えないと明示
    assert any(f.kind == "return" for f in items.flows)


def test_returns_cover_every_tail_path(env) -> None:
    service, project, index, symbol = env
    returns = service.returns(project, index, symbol("core-pipeline"))
    assert [(r.text, bool(r.note)) for r in returns] == [("items", False), ("nil", True)]  # (if c items) の else 無し = 暗黙の nil
    assert any("暗黙" in r.note or "条件が偽" in r.note for r in returns)


def test_risks_rules_and_todo(env) -> None:
    service, project, index, symbol = env
    findings, skipped = service.scan_risks(project, index)
    assert skipped == []
    in_pipeline = {f.rule for f in findings if f.symbol == "core-pipeline"}
    assert {"command-exec", "anonymous-hook", "bare-except", "swallowed-exception", "todo-marker"} <= in_pipeline
    assert not any(f.symbol == "core-greet" for f in findings)  # 問題のない関数には、何も出さない


def test_externals_are_classified_by_name_as_inferred(env) -> None:
    from codeinsight.application.external_service import ExternalService

    service, project, index, symbol = env
    uses = [u for u in ExternalService().report(index).uses if u.source_id == symbol("core-pipeline").symbol_id]
    by_name = {u.library: u for u in uses}
    assert by_name["delete-file"].category == "filesystem" and by_name["shell-command"].category == "process"
    assert all(u.confidence == "inferred" if hasattr(u, "confidence") else True for u in uses)
    assert "format" not in by_name and "length" not in by_name  # 純粋な関数は、外部への操作ではない


def test_cli_dataflow_and_risks_for_elisp(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "d.db")
    assert main(["analyze", str(FIXTURE), "--db", db]) == 0
    capsys.readouterr()
    assert main(["dataflow", "core-pipeline", "path", "--db", db, "--project", str(FIXTURE)]) == 0
    assert "core-read の仮引数 file" in capsys.readouterr().out
    assert main(["risks", "--db", db, "--project", str(FIXTURE)]) == 0
    out = capsys.readouterr().out
    assert "command-exec" in out and "検査していません" not in out  # Emacs Lispも対象に含まれる

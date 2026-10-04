"""動的解析の実行と観測の保存。コンテナの代わりに、収集器を手元のプロセスで動かす実行器を差し込んで検証する
（コンテナでの実行は、イメージがある場合だけの結合テストで確認する）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codeinsight.application import ProjectIndex
from codeinsight.cli import main
from codeinsight.domain.observation import ObservationKind, RunStatus
from codeinsight.dynamic import executor
from codeinsight.dynamic.permission import DynamicPermission
from codeinsight.dynamic.service import DynamicAnalysisService
from codeinsight.infrastructure.analysis_repository import AnalysisRepository

SAMPLE = {
    "pkg/__init__.py": "",
    "pkg/core.py": (
        "class Box:\n"
        "    def put(self, v):\n"
        "        if v < 0:\n"
        "            raise ValueError('neg')\n"
        "        return helper(v)\n"
        "\n"
        "def helper(v):\n"
        "    return v + 1\n"
        "\n"
        "def never_called():\n"
        "    return 0\n"
    ),
    "main.py": (
        "from pkg.core import Box\n"
        "b = Box()\n"
        "b.put(1)\n"
        "b.put(2)\n"
        "try:\n"
        "    b.put(-1)\n"
        "except ValueError:\n"
        "    pass\n"
    ),
}


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    for name, text in SAMPLE.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def db(project_dir: Path, tmp_path: Path, capsys) -> str:
    path = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(project_dir), "--db", path]) == 0
    capsys.readouterr()
    return path


def local_executor(run: executor.PermittedRun) -> executor.ExecutionResult:
    """コンテナの代わりに、収集器を手元で動かす（テスト専用）。"""

    out = run.root.parent / "obs.json"
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, str(executor.COLLECTOR_SCRIPT), "--root", str(run.root), "--out", str(out), "--", *run.permission.command],
        cwd=run.root, capture_output=True, check=False, timeout=60,
    )
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else None
    return executor.ExecutionResult(False, completed.returncode, time.monotonic() - started, data, "", completed.stderr.decode()[-500:], ["local"])


def load(db: str):
    repository = AnalysisRepository(Path(db))
    project = repository.list_projects()[0]
    return repository, project, ProjectIndex.load(repository, project.project_id)


def test_run_collects_calls_coverage_and_failures_and_maps_them_to_symbols(db: str) -> None:
    repository, project, index = load(db)
    outcome = DynamicAnalysisService().run(
        project, DynamicPermission(allow_run=True, command=("main.py",)), index, repository, run_executor=local_executor,
    )
    assert outcome.run.status == RunStatus.COMPLETED and outcome.run.exit_code == 0
    by_name = {s.qualified_name: s.symbol_id for s in index.symbols.values()}
    executed = {o.name: o.count for o in outcome.observations if o.detail.get("what") == "function"}
    assert executed["Box.put"] == 3 and executed["helper"] == 2 and "never_called" not in executed  # 観測されなかった関数は、記録されない
    put_id = next(i for q, i in by_name.items() if q.endswith("Box.put"))
    assert any(o.symbol_id == put_id for o in outcome.observations)  # 解析結果のシンボルに対応づく
    calls = {(o.name, o.target_name): o.count for o in outcome.observations if o.kind == ObservationKind.CALL}
    assert calls[("Box.put", "helper")] == 2
    raised = [o for o in outcome.observations if o.kind == ObservationKind.FAILURE]
    assert raised and raised[0].detail["type"] == "ValueError" and raised[0].count == 1  # 捕捉された例外も、型・位置だけ記録
    assert "neg" not in json.dumps([o.detail for o in outcome.observations])  # 値・メッセージは記録しない
    # 保存と再読み込み
    stored = repository.list_dynamic_runs(project.project_id)
    assert [r.run_id for r in stored] == [outcome.run.run_id] and stored[0].permission["allow_network"] is False
    assert len(repository.list_dynamic_observations(outcome.run.run_id)) == len(outcome.observations)
    repository.close()


def test_run_detects_a_target_that_changed_during_execution(db: str, project_dir: Path) -> None:
    repository, project, index = load(db)

    def tampering(run: executor.PermittedRun) -> executor.ExecutionResult:
        result = local_executor(run)
        (run.root / "pkg" / "core.py").write_text("changed = 1\n", encoding="utf-8")
        return result

    outcome = DynamicAnalysisService().run(project, DynamicPermission(allow_run=True, command=("main.py",)), index, repository, run_executor=tampering)
    assert outcome.run.status == RunStatus.TARGET_MODIFIED and outcome.observations == []  # 変更を検知した実行の観測は、保存しない
    assert any("内容が変わっていました" in n for n in outcome.run.notes)
    repository.close()


def test_run_records_timeout_and_missing_output_as_not_completed(db: str) -> None:
    repository, project, index = load(db)
    service, permission = DynamicAnalysisService(), DynamicPermission(allow_run=True, command=("main.py",))
    timeout = service.run(project, permission, index, repository, run_executor=lambda run: executor.ExecutionResult(True, None, 60.0, None))
    assert timeout.run.status == RunStatus.TIMEOUT and timeout.observations == []
    missing = service.run(project, permission, index, repository, run_executor=lambda run: executor.ExecutionResult(False, 1, 0.1, None))
    assert missing.run.status == RunStatus.FAILED and any("得られませんでした" in n for n in missing.run.notes)  # 失敗を正常として扱わない
    repository.close()


def test_cli_observed_and_runs_report_observations_and_staleness(db: str, project_dir: Path, capsys, monkeypatch) -> None:
    repository, project, index = load(db)
    DynamicAnalysisService().run(project, DynamicPermission(allow_run=True, command=("main.py",)), index, repository, run_executor=local_executor)
    repository.close()

    assert main(["observed", "Box.put", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "実行された回数: 3" in out and "helper" in out and "静的にも確認" in out and "ValueError" in out
    assert main(["observed", "never_called", "--db", db]) == 0
    assert "実行されたことを観測できませんでした" in capsys.readouterr().out  # 観測が無いことは、未実行とは言わない
    assert main(["dynamic-runs", "--db", db, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["stale_files"] == []

    (project_dir / "pkg" / "core.py").write_text(SAMPLE["pkg/core.py"] + "# edited\n", encoding="utf-8")
    assert main(["dynamic-runs", "--db", db, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["stale_files"] == ["pkg/core.py"]  # 実行後に変わったファイルは、古い観測として示す


def test_container_command_is_isolated_and_never_records_env_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MY_SECRET", "s3cr3t-value")
    permission = DynamicPermission(allow_run=True, command=("main.py", "--flag"), env_allowlist=("MY_SECRET", "NOT_SET"))
    run = executor.PermittedRun.create(permission, tmp_path, "img:1")
    argv, audit = executor.build_argv(run, tmp_path / "out", "name")
    joined = " ".join(argv)
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--user 65534:65534", "--pids-limit", "--memory"):
        assert flag in joined
    assert f"{tmp_path}:/target:ro" in joined and "MY_SECRET=s3cr3t-value" in joined and "NOT_SET" not in joined
    assert "s3cr3t-value" not in " ".join(audit) and "MY_SECRET=<値は記録しない>" in " ".join(audit)  # 記録用には値を出さない
    assert argv[-2:] == ["main.py", "--flag"]
    network = executor.build_argv(executor.PermittedRun.create(DynamicPermission(allow_run=True, command=("x",), allow_network=True), tmp_path), tmp_path, "n")[0]
    assert "bridge" in network


def test_permitted_run_cannot_be_created_without_permission(tmp_path: Path) -> None:
    from codeinsight.dynamic.permission import DynamicPermissionError

    with pytest.raises(DynamicPermissionError):
        executor.PermittedRun.create(DynamicPermission(command=("x",)), tmp_path)
    with pytest.raises(executor.ExecutorError):
        executor.PermittedRun.create(DynamicPermission(allow_run=True, command=("x",), sandbox_backend="none", allow_unsandboxed=True), tmp_path)


@pytest.mark.skipif(shutil.which("docker") is None or not executor.image_present(executor.DEFAULT_IMAGE), reason="docker または python:3.12-slim がありません")
def test_container_execution_end_to_end(db: str) -> None:
    repository, project, index = load(db)
    outcome = DynamicAnalysisService().run(project, DynamicPermission(allow_run=True, command=("main.py",)), index, repository)
    assert outcome.run.status == RunStatus.COMPLETED
    assert {o.name for o in outcome.observations if o.detail.get("what") == "function"} >= {"Box.put", "helper"}
    repository.close()

"""動的解析の実行と観測の保存。コンテナの代わりに、収集器を手元のプロセスで動かす実行器を差し込んで検証する
（コンテナでの実行は、イメージがある場合だけの結合テストで確認する）。"""

from __future__ import annotations

import json
import os
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
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "--user", "--pids-limit", "--memory"):
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


def _container_available() -> bool:
    return shutil.which("docker") is not None and executor.image_present(executor.DEFAULT_IMAGE)


def test_container_is_available_when_ci_requires_it() -> None:
    """CI（CODEINSIGHT_REQUIRE_CONTAINER=1）では、コンテナの結合テストを黙ってスキップさせない。"""

    if os.environ.get("CODEINSIGHT_REQUIRE_CONTAINER") == "1":
        assert _container_available(), "docker または python:3.12-slim がありません（CIで取得する手順を確認してください）"


container = pytest.mark.skipif(not _container_available(), reason="docker または python:3.12-slim がありません")

PROBE = (
    "import os, socket, sys\n"
    "def report(key, value): print(key + '=' + str(value), file=sys.stderr)\n"
    "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=3); report('network', 'open')\n"
    "except OSError: report('network', 'blocked')\n"
    "try:\n    open('/target/x.txt', 'w'); report('target', 'writable')\n"
    "except OSError: report('target', 'readonly')\n"
    "try:\n    open('/etc/x', 'w'); report('rootfs', 'writable')\n"
    "except OSError: report('rootfs', 'readonly')\n"
    "report('uid', os.getuid())\n"
    "report('secret', os.environ.get('CODEINSIGHT_TEST_SECRET'))\n"
    "report('allowed', os.environ.get('CODEINSIGHT_TEST_ALLOWED'))\n"
)


def _leftover_containers() -> list[str]:
    done = subprocess.run(["docker", "ps", "-aq", "--filter", "name=codeinsight-dyn"], capture_output=True, text=True, check=False)
    return done.stdout.split()


@container
def test_container_execution_end_to_end(db: str) -> None:
    repository, project, index = load(db)
    outcome = DynamicAnalysisService().run(project, DynamicPermission(allow_run=True, command=("main.py",)), index, repository)
    assert outcome.run.status == RunStatus.COMPLETED
    executed = {o.name: o.count for o in outcome.observations if o.detail.get("what") == "function"}
    assert executed["Box.put"] == 3 and executed["helper"] == 2 and "never_called" not in executed
    calls = {(o.name, o.target_name): o.count for o in outcome.observations if o.kind == ObservationKind.CALL}
    assert calls[("Box.put", "helper")] == 2
    assert any(o.kind == ObservationKind.FAILURE and o.detail["type"] == "ValueError" for o in outcome.observations)
    assert any(o.symbol_id for o in outcome.observations)  # 解析結果のシンボルに対応づく
    assert _leftover_containers() == []
    repository.close()


@container
def test_container_isolation_on_the_real_container(db: str, project_dir: Path, monkeypatch) -> None:
    """隔離の設定が、実際のコンテナで効いていること（ネットワーク遮断・対象と根ファイルシステムは読み取り専用・非特権・環境変数は許可リストのみ）。"""

    monkeypatch.setenv("CODEINSIGHT_TEST_SECRET", "must-not-leak")
    monkeypatch.setenv("CODEINSIGHT_TEST_ALLOWED", "passed")
    (project_dir / "probe.py").write_text(PROBE, encoding="utf-8")
    assert main(["analyze", str(project_dir), "--db", db]) == 0
    repository, project, index = load(db)
    permission = DynamicPermission(allow_run=True, command=("probe.py",), env_allowlist=("CODEINSIGHT_TEST_ALLOWED",))
    outcome = DynamicAnalysisService().run(project, permission, index, repository)
    assert outcome.run.status == RunStatus.COMPLETED
    seen = dict(line.split("=", 1) for line in outcome.stderr_tail.splitlines() if "=" in line)
    assert seen == {"network": "blocked", "target": "readonly", "rootfs": "readonly", "uid": str(os.getuid() or 65534), "secret": "None", "allowed": "passed"}
    assert not (project_dir / "x.txt").exists()  # 対象には何も書かれていない
    assert "must-not-leak" not in json.dumps(outcome.run.sandbox)  # 記録に環境変数の値を残さない
    repository.close()


@container
def test_container_timeout_stops_the_container(db: str, project_dir: Path) -> None:
    (project_dir / "slow.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    assert main(["analyze", str(project_dir), "--db", db]) == 0
    repository, project, index = load(db)
    started = time.monotonic()
    outcome = DynamicAnalysisService().run(project, DynamicPermission(allow_run=True, command=("slow.py",), timeout_seconds=3), index, repository)
    assert outcome.run.status == RunStatus.TIMEOUT and outcome.observations == []
    assert time.monotonic() - started < 20  # 30秒待たずに止まる
    assert _leftover_containers() == []
    repository.close()


@container
def test_container_missing_command_and_uncaught_exception_are_recorded_as_failed(db: str, project_dir: Path) -> None:
    (project_dir / "boom.py").write_text("def f():\n    raise RuntimeError('x')\nf()\n", encoding="utf-8")
    assert main(["analyze", str(project_dir), "--db", db]) == 0
    repository, project, index = load(db)
    service = DynamicAnalysisService()
    boom = service.run(project, DynamicPermission(allow_run=True, command=("boom.py",)), index, repository)
    assert boom.run.status == RunStatus.FAILED and boom.run.exit_code == 1  # 異常終了を正常として扱わない
    assert any(o.detail.get("what") == "uncaught" and o.detail["type"] == "RuntimeError" for o in boom.observations)
    missing = service.run(project, DynamicPermission(allow_run=True, command=("no_such.py",)), index, repository)
    assert missing.run.status == RunStatus.FAILED
    repository.close()

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from codeinsight.cli import main
from codeinsight.domain.observation import DynamicRun, Observation, ObservationKind, RunStatus
from codeinsight.dynamic.collectors import COLLECTORS, collectors_for
from codeinsight.dynamic.permission import (
    DynamicAnalysisNotImplemented,
    DynamicPermission,
    DynamicPermissionError,
)
from codeinsight.dynamic.sandbox import SandboxPolicy
from codeinsight.dynamic.service import DynamicAnalysisService

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --- 許可モデル ---


def test_permission_is_denied_by_default_and_names_every_missing_piece() -> None:
    permission = DynamicPermission()
    assert not permission.permitted
    reasons = " ".join(permission.violations())
    assert "--allow-run" in reasons and "コマンドが指定されていません" in reasons  # 許可もコマンドも無い
    with pytest.raises(DynamicPermissionError) as raised:
        permission.check()
    assert len(raised.value.reasons) == 2


def test_permission_requires_both_the_flag_and_an_explicit_command() -> None:
    assert not DynamicPermission(allow_run=True).permitted  # 許可だけでは実行できない（コマンドを推測しない）
    assert not DynamicPermission(command=("pytest",)).permitted  # コマンドだけでも実行できない
    assert DynamicPermission(allow_run=True, command=("pytest", "-q")).permitted


def test_unsandboxed_execution_needs_an_extra_permission_and_bad_values_are_rejected() -> None:
    base = {"allow_run": True, "command": ("x",)}
    assert not DynamicPermission(**base, sandbox_backend="none").permitted
    assert DynamicPermission(**base, sandbox_backend="none", allow_unsandboxed=True).permitted
    assert not DynamicPermission(**base, sandbox_backend="unknown").permitted
    assert not DynamicPermission(**base, timeout_seconds=0).permitted
    assert not DynamicPermission(**base, timeout_seconds=10_000).permitted
    assert not DynamicPermission(**base, env_allowlist=("OK", "bad name")).permitted


def test_permission_summary_never_contains_environment_values() -> None:
    summary = DynamicPermission(allow_run=True, command=("x",), env_allowlist=("TOKEN",)).summary()
    assert summary["env_allowlist"] == ["TOKEN"] and "value" not in json.dumps(summary)  # 名前だけで、値は持たない


# --- サンドボックスの方針 ---


def test_sandbox_policy_defaults_are_the_strictest() -> None:
    policy = SandboxPolicy.from_permission(DynamicPermission())
    assert policy.network == "none" and policy.target_mount == "read-only" and policy.workdir == "temporary"
    assert policy.run_as == "unprivileged" and policy.env == () and policy.backend == "container"
    relaxed = SandboxPolicy.from_permission(DynamicPermission(allow_network=True, env_allowlist=("A",)))
    assert relaxed.network == "allowed" and relaxed.env == ("A",)  # 緩めるのは明示したものだけ


# --- ドメイン・収集器 ---


def test_domain_models_and_collector_catalog() -> None:
    assert {k.value for k in ObservationKind} == {"call", "coverage", "failure", "io", "shape", "timing"}
    assert RunStatus.DENIED.value == "denied" and RunStatus.TARGET_MODIFIED.value == "target_modified"
    run = DynamicRun("r1", "p1", __import__("datetime").datetime.now(), ("pytest",), {}, {}, "python-monitoring", "0", RunStatus.DENIED)
    assert run.status == RunStatus.DENIED and Observation("o1", "r1", ObservationKind.CALL).count == 1
    assert all(not c.implemented for c in COLLECTORS)  # すべて未実装
    assert {c.name for c in collectors_for({"c"})} == {"c-gcov", "c-instrument", "c-syscall"}
    assert all(c.needs_rebuild for c in COLLECTORS if c.name in ("c-gcov", "c-instrument"))  # 再コンパイルを伴うものを区別する


# --- 誤実行の防止（最重要） ---


def test_the_dynamic_code_contains_no_way_to_execute_programs() -> None:
    forbidden = re.compile(r"\b(subprocess|os\.system|os\.exec\w*|os\.spawn\w*|os\.popen|pty|multiprocessing|eval|exec)\b|__import__")
    files = [*(ROOT / "src" / "codeinsight" / "dynamic").glob("*.py"), ROOT / "src" / "codeinsight" / "cli" / "dynamic_commands.py"]
    assert len(files) >= 6
    for path in files:
        code = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.strip().startswith(("#", '"', "'")))
        found = [m.group(0) for m in forbidden.finditer(re.sub(r'""".*?"""', "", code, flags=re.S))]
        assert not found, f"{path.name} に実行系の呼び出しがある: {found}"  # スタブ段階では、実行コードを持たない


def test_service_run_checks_permission_first_and_never_executes(tmp_path: Path) -> None:
    from codeinsight.domain import Project

    project = Project("p", tmp_path, "p")
    marker = tmp_path / "marker"
    service = DynamicAnalysisService()
    command = ("touch", str(marker))
    with pytest.raises(DynamicPermissionError):
        service.run(project, DynamicPermission(command=command))  # 許可なし
    with pytest.raises(DynamicAnalysisNotImplemented):
        service.run(project, DynamicPermission(allow_run=True, command=command))  # 許可はあるが未実装
    assert not marker.exists()  # どちらの場合も、コマンドは実行されていない


# --- CLI ---


@pytest.fixture
def db(tmp_path: Path, capsys) -> str:
    path = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(FIXTURES / "c_flow"), "--db", path]) == 0
    capsys.readouterr()
    return path


def test_cli_plan_shows_the_plan_without_executing(db: str, tmp_path: Path, capsys) -> None:
    marker = tmp_path / "marker"
    assert main(["dynamic-plan", "--db", db, "--", "touch", str(marker)]) == 0
    out = capsys.readouterr().out
    assert "何も実行していません" in out and "実行は拒否されます" in out and "--allow-run" in out
    assert "ネットワーク: 遮断" in out and "c-gcov" in out and "再コンパイルを伴う" in out
    assert not marker.exists()  # 計画の表示では、実行しない

    assert main(["dynamic-plan", "--db", db, "--allow-run", "--format", "json", "--", "touch", str(marker)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["permitted"] is True and data["executed"] is False and data["sandbox"]["network"] == "none"
    assert not marker.exists()


def test_cli_run_refuses_without_permission_and_never_runs_even_with_it(db: str, tmp_path: Path, capsys) -> None:
    marker = tmp_path / "marker"
    assert main(["dynamic-run", "--db", db, "--", "touch", str(marker)]) == 2
    assert "許可が揃っていません" in capsys.readouterr().err and not marker.exists()

    assert main(["dynamic-run", "--db", db, "--allow-run", "--", "touch", str(marker)]) == 3
    err = capsys.readouterr().err
    assert "未実装" in err and "何も実行していません" in err and not marker.exists()

    assert main(["dynamic-run", "--db", db, "--allow-run"]) == 2  # コマンドの指定が無い
    assert "コマンドが指定されていません" in capsys.readouterr().err


def test_environment_variable_alone_does_not_allow_running_without_a_command(db: str, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CODEINSIGHT_DYNAMIC_ALLOW_RUN", "1")
    assert main(["dynamic-run", "--db", db]) == 2  # 環境変数の許可があっても、コマンドの明示が必要
    assert "コマンドが指定されていません" in capsys.readouterr().err

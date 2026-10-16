"""観測と静的解析の差分（dynamic-diff）。観測のみ・静的のみ・解決候補・古いファイルの除外・実行しないこと。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeinsight.cli import main
from codeinsight.dynamic.diff import compare
from codeinsight.dynamic.permission import DynamicPermission
from codeinsight.dynamic.service import DynamicAnalysisService
from test_dynamic_run import load, local_executor

APP = (
    "def a(flag):\n"
    "    if flag:\n"
    "        b()\n"
    "    return table['x']()\n"
    "\n"
    "def b():\n"
    "    return 1\n"
    "\n"
    "def c():\n"
    "    return 2\n"
    "\n"
    "def unused():\n"
    "    return 0\n"
    "\n"
    "table = {'x': c}\n"
    "a(False)\n"
)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    (root / "app.py").write_text(APP, encoding="utf-8")
    return root


@pytest.fixture
def db(project_dir: Path, tmp_path: Path, capsys) -> str:
    path = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(project_dir), "--db", path]) == 0
    capsys.readouterr()
    return path


def run_once(db: str):
    repository, project, index = load(db)
    outcome = DynamicAnalysisService().run(project, DynamicPermission(allow_run=True, command=("app.py",)), index, repository, run_executor=local_executor)
    return repository, index, outcome


def test_compare_separates_observed_only_static_only_and_unexecuted(db: str) -> None:
    repository, index, outcome = run_once(db)
    diff = compare(index, outcome.run, outcome.observations)
    observed_only = {(e.caller, e.callee): e for e in diff.observed_only}
    assert ("app.a", "app.c") in observed_only  # 辞書経由の呼び出しは、静的に確定できないが、実行では観測された
    assert [c.status for c in observed_only[("app.a", "app.c")].candidates] == ["unresolved"]  # 解決候補として示す（確定にはしない）
    assert ("app.a", "app.b") in {(e.caller, e.callee) for e in diff.static_only}  # 呼び出し元は実行されたが、辺は通らなかった
    assert not any(e.callee == "app.unused" for e in diff.static_only)  # 呼び出し元が無いものは、静的のみにしない
    assert diff.not_executed >= 2  # b と unused は実行を観測できていない（「呼ばれない」とは言わない）
    assert any("呼ばれないことを意味しません" in n for n in diff.notes)
    repository.close()


def test_compare_excludes_files_changed_since_the_analysis(db: str, project_dir: Path) -> None:
    repository, index, outcome = run_once(db)
    # 解析結果は実行時のソースのまま。実行記録のハッシュだけが食い違う状況（解析後・実行前に変更）を作る
    outcome.run.source_hashes["app.py"] = "0" * 64
    diff = compare(index, outcome.run, outcome.observations)
    assert diff.stale_files == ["app.py"] and diff.both == [] and diff.observed_only == [] and diff.static_only == []
    assert any("位置が対応しない" in n for n in diff.notes)
    repository.close()


def test_cli_dynamic_diff_text_and_json(db: str, capsys) -> None:
    repository, _index, _outcome = run_once(db)
    repository.close()
    assert main(["dynamic-diff", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "解決候補（確定ではない）" in out and "静的のみ" in out and "観測のみ" in out
    assert main(["dynamic-diff", "--db", db, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["observed_only"] and data["static_only"] and data["notes"]


def test_cli_dynamic_diff_without_runs_says_so_and_never_executes(db: str, capsys) -> None:
    assert main(["dynamic-diff", "--db", db]) == 0
    assert "比較できる観測がありません" in capsys.readouterr().out

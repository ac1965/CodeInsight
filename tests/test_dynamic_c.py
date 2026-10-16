"""Cの動的解析（gcov）。変換は文字列だけで検証し、実行は手元の cc・gcov で収集器（ccollect.sh）を動かす（コンテナの代わり）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from codeinsight.application import ProjectIndex
from codeinsight.cli import main
from codeinsight.domain.observation import ObservationKind, RunStatus
from codeinsight.dynamic import c_observation, executor
from codeinsight.dynamic.diff import compare
from codeinsight.dynamic.permission import DynamicPermission
from codeinsight.dynamic.service import DynamicAnalysisService
from codeinsight.infrastructure.analysis_repository import AnalysisRepository

GCOV_TEXT = """\
==WORK /tmp/ci/src
==GCOV /tmp/ci/src
        -:    0:Source:m.c
        -:    0:Graph:m-m.gcno
        -:    1:#include <stdio.h>
function helper called 3 returned 100% blocks executed 100%
        3:    2:static int helper(int x) { return x + 1; }
function unused called 0 returned 0% blocks executed 0%
    #####:    3:int unused(void) { return 0; }
function main called 1 returned 100% blocks executed 100%
        1:    4:int main(void) {
        4:    6:    for (int i = 0; i < 3; i++) t += helper(i);
branch  0 taken 75%
        -:    9:}
==GCOV /tmp/ci/src/lib
        -:    0:Source:../util.h
function inline_fn called 2 returned 100% blocks executed 100%
        2:    1:static inline int inline_fn(void) { return 1; }
==GCOV /tmp/ci/src
        -:    0:Source:/usr/include/stdio.h
function printf called 1 returned 100% blocks executed 100%
        1:    9:int printf(void);
"""


def test_parse_gcov_text_counts_functions_and_lines_and_ignores_outside_files() -> None:
    data = c_observation.parse(GCOV_TEXT, 0, 1.5)
    functions = {(f["file"], f["qualname"], f["line"]): f["count"] for f in data["functions"]}
    assert functions == {("m.c", "helper", 2): 3, ("m.c", "main", 4): 1, ("util.h", "inline_fn", 1): 2}  # 回数0の関数・システムヘッダーは記録しない
    assert data["lines"] == {"m.c": [2, 4, 6], "util.h": [1]} and data["schema"] == c_observation.SCHEMA
    assert data["calls"] == [] and data["mechanism"] == "gcov" and data["exit_code"] == 0
    summed = c_observation.parse(GCOV_TEXT + GCOV_TEXT, 1)
    assert {(f["qualname"]): f["count"] for f in summed["functions"]}["helper"] == 6  # 同じ関数が複数のオブジェクトにあれば合算


def test_c_container_command_uses_the_shell_collector_and_a_larger_tmpfs(tmp_path: Path) -> None:
    permission = DynamicPermission(allow_run=True, command=("make", "test"), collector="c")
    run = executor.PermittedRun.create(permission, tmp_path)
    assert run.image == executor.DEFAULT_IMAGE_C
    argv, _ = executor.build_argv(run, tmp_path / "out", "n")
    joined = " ".join(argv)
    for flag in ("--network none", "--read-only", "--cap-drop ALL", "no-new-privileges", "size=512m", f"{tmp_path}:/target:ro"):
        assert flag in joined
    assert argv[-2:] == ["make", "test"] and "sh" in argv and executor.CONTAINER_C_COLLECTOR in argv
    assert DynamicPermission(allow_run=True, command=("x",), collector="rust").violations()  # 不正な収集器は拒否


C_SAMPLE = {
    "main.c": '#include <stdio.h>\n#include "lib.h"\nint main(void) {\n    int t = 0;\n    for (int i = 0; i < 3; i++) t += twice(i);\n    printf("%d\\n", t);\n    return 0;\n}\n',
    "lib.c": "#include \"lib.h\"\nint twice(int x) { return x * 2; }\nint never_called(void) { return 1; }\n",
    "lib.h": "int twice(int x);\nint never_called(void);\n",
    "build.sh": 'set -e\n$CC main.c lib.c -o app\n./app\n',
}


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    root.mkdir()
    for name, text in C_SAMPLE.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


def local_c_executor(run: executor.PermittedRun) -> executor.ExecutionResult:
    """コンテナの代わりに、収集器のシェルスクリプトを手元の cc・gcov で動かす（テスト専用）。"""

    out = Path(tempfile.mkdtemp(prefix="ci-c-test-"))
    started = time.monotonic()
    completed = subprocess.run(
        ["sh", str(executor.C_COLLECTOR_SCRIPT), "--root", str(run.root), "--out", str(out), "--", *run.permission.command],
        capture_output=True, check=False, timeout=120,
    )
    observation = executor._read_c_observation(out, time.monotonic() - started)
    shutil.rmtree(out, ignore_errors=True)
    return executor.ExecutionResult(False, completed.returncode, time.monotonic() - started, observation, "", completed.stderr.decode()[-500:], ["local"])


needs_toolchain = pytest.mark.skipif(shutil.which("cc") is None or shutil.which("gcov") is None, reason="cc と gcov が必要")


@needs_toolchain
def test_c_run_records_function_execution_and_never_modifies_the_target(project_dir: Path, tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(project_dir), "--db", db]) == 0
    capsys.readouterr()
    before = {p.name: p.read_bytes() for p in project_dir.iterdir()}
    repository = AnalysisRepository(Path(db))
    project = repository.list_projects()[0]
    index = ProjectIndex.load(repository, project.project_id)
    permission = DynamicPermission(allow_run=True, command=("sh", "build.sh"), collector="c")
    outcome = DynamicAnalysisService().run(project, permission, index, repository, run_executor=local_c_executor)
    assert outcome.run.status == RunStatus.COMPLETED and outcome.run.collector == "c-gcov"
    executed = {o.name: o.count for o in outcome.observations if o.detail.get("what") == "function"}
    assert executed["twice"] == 3 and executed["main"] == 1 and "never_called" not in executed  # 観測されなかった関数は記録しない
    assert any(o.symbol_id for o in outcome.observations if o.name == "twice")  # 解析結果のシンボルに対応づく
    assert not any(o.kind == ObservationKind.CALL for o in outcome.observations)  # gcov は呼び出しの辺を得られない
    assert {p.name: p.read_bytes() for p in project_dir.iterdir()} == before  # 対象に、ビルドの生成物も残らない（複製の中で行う）
    diff = compare(index, outcome.run, outcome.observations)
    assert diff.executed_functions >= 2 and diff.not_executed >= 1  # never_called は「実行を観測できなかった関数」
    repository.close()
    assert main(["observed", "twice", "--db", db]) == 0
    assert "実行された回数: 3" in capsys.readouterr().out


@needs_toolchain
def test_failed_build_is_recorded_as_failed_not_completed(project_dir: Path, tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(project_dir), "--db", db]) == 0
    repository = AnalysisRepository(Path(db))
    project = repository.list_projects()[0]
    index = ProjectIndex.load(repository, project.project_id)
    permission = DynamicPermission(allow_run=True, command=("sh", "-c", "$CC does_not_exist.c -o app"), collector="c")
    outcome = DynamicAnalysisService().run(project, permission, index, repository, run_executor=local_c_executor)
    assert outcome.run.status == RunStatus.FAILED and outcome.run.exit_code != 0  # ビルドの失敗を正常として扱わない
    repository.close()
    json.dumps(outcome.run.permission)

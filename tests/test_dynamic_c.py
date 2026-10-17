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
    assert outcome.run.status == RunStatus.COMPLETED and outcome.run.collector.startswith("c-gcov")
    executed = {o.name: o.count for o in outcome.observations if o.detail.get("what") == "function"}
    assert executed["twice"] == 3 and executed["main"] == 1 and "never_called" not in executed  # 観測されなかった関数は記録しない
    assert any(o.symbol_id for o in outcome.observations if o.name == "twice")  # 解析結果のシンボルに対応づく
    assert not any(o.kind == ObservationKind.CALL for o in outcome.observations)  # gcov は呼び出しの辺を得られない
    assert {p.name: p.read_bytes() for p in project_dir.iterdir()} == before  # 対象に、ビルドの生成物も残らない（複製の中で行う）
    diff = compare(index, outcome.run, outcome.observations)
    assert diff.executed_functions >= 2 and diff.not_executed >= 1  # never_called は「実行を観測できなかった関数」
    assert diff.static_only == [] and any("呼び出しの辺を観測できない" in n for n in diff.notes)  # 辺を観測できない収集器で、「静的のみ」と誤って示さない
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


def _c_container_available() -> bool:
    return shutil.which("docker") is not None and executor.image_present(executor.DEFAULT_IMAGE_C)


def test_c_container_is_available_when_ci_requires_it() -> None:
    """CI（CODEINSIGHT_REQUIRE_CONTAINER=1）では、Cのコンテナの結合テストを黙ってスキップさせない。"""

    import os

    if os.environ.get("CODEINSIGHT_REQUIRE_CONTAINER") == "1":
        assert _c_container_available(), f"docker または {executor.DEFAULT_IMAGE_C} がありません（CIで取得する手順を確認してください）"


c_container = pytest.mark.skipif(not _c_container_available(), reason=f"docker または {executor.DEFAULT_IMAGE_C} がありません")

C_PROBE = r"""
#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>
#include <arpa/inet.h>
#include <sys/socket.h>
static void report(const char *key, const char *value) { fprintf(stderr, "%s=%s\n", key, value); }
int main(void) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a = {0}; a.sin_family = AF_INET; a.sin_port = htons(53); inet_pton(AF_INET, "1.1.1.1", &a.sin_addr);
    report("network", connect(s, (struct sockaddr *)&a, sizeof a) == 0 ? "open" : "blocked");
    FILE *f = fopen("/target/x.txt", "w"); report("target", f ? "writable" : "readonly");
    f = fopen("/etc/x", "w"); report("rootfs", f ? "writable" : "readonly");
    char buf[16]; snprintf(buf, sizeof buf, "%d", (int)(getuid() != 0)); report("unprivileged", buf);
    const char *secret = getenv("CODEINSIGHT_TEST_SECRET"); report("secret", secret ? secret : "None");
    return 0;
}
"""


@c_container
def test_c_container_end_to_end_with_gcov(project_dir: Path, tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(project_dir), "--db", db]) == 0
    capsys.readouterr()
    before = {p.name: p.read_bytes() for p in project_dir.iterdir()}
    repository = AnalysisRepository(Path(db))
    project = repository.list_projects()[0]
    index = ProjectIndex.load(repository, project.project_id)
    permission = DynamicPermission(allow_run=True, command=("sh", "build.sh"), collector="c")
    outcome = DynamicAnalysisService().run(project, permission, index, repository)
    assert outcome.run.status == RunStatus.COMPLETED and outcome.run.collector.startswith("c-gcov"), outcome.stderr_tail
    executed = {o.name: o.count for o in outcome.observations if o.detail.get("what") == "function"}
    assert executed["twice"] == 3 and executed["main"] == 1 and "never_called" not in executed
    assert any(o.symbol_id for o in outcome.observations if o.name == "twice")
    assert {p.name: p.read_bytes() for p in project_dir.iterdir()} == before  # 対象は読み取り専用で、ビルドの生成物は複製の中だけ
    assert subprocess.run(["docker", "ps", "-aq", "--filter", "name=codeinsight-dyn"], capture_output=True, text=True, check=False).stdout.split() == []
    repository.close()


@c_container
def test_c_container_isolation_on_the_real_container(project_dir: Path, tmp_path: Path, monkeypatch) -> None:
    """Cの収集器でも、隔離が実際のコンテナで効いていること（ネットワーク遮断・対象と根ファイルシステムは読み取り専用・非特権・環境変数は許可リストのみ）。"""

    monkeypatch.setenv("CODEINSIGHT_TEST_SECRET", "must-not-leak")
    (project_dir / "probe.c").write_text(C_PROBE, encoding="utf-8")
    (project_dir / "probe.sh").write_text("set -e\n$CC probe.c -o probe\n./probe\n", encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(project_dir), "--db", db]) == 0
    repository = AnalysisRepository(Path(db))
    project = repository.list_projects()[0]
    index = ProjectIndex.load(repository, project.project_id)
    outcome = DynamicAnalysisService().run(project, DynamicPermission(allow_run=True, command=("sh", "probe.sh"), collector="c"), index, repository)
    assert outcome.run.status == RunStatus.COMPLETED, outcome.stderr_tail
    seen = dict(line.split("=", 1) for line in outcome.stderr_tail.splitlines() if "=" in line)
    assert seen == {"network": "blocked", "target": "readonly", "rootfs": "readonly", "unprivileged": "1", "secret": "None"}
    assert not (project_dir / "x.txt").exists() and "must-not-leak" not in json.dumps(outcome.run.sandbox)
    repository.close()


C_IO_SAMPLE = {
    "main.c": '#include <stdio.h>\n#include <stdlib.h>\n#include "lib.h"\nstatic int helper(int x) { return twice(x) + 1; }\nint main(void) {\n    const char *home = getenv("HOME");\n    FILE *f = fopen("/etc/hostname", "r");\n    if (f) fclose(f);\n    FILE *g = fopen("out.txt", "w");\n    if (g) { fputs("secret-value-must-not-be-recorded", g); fclose(g); }\n    int t = 0;\n    for (int i = 0; i < 3; i++) t += helper(i);\n    printf("%d %d\\n", t, home != NULL);\n    return 0;\n}\n',
    "lib.c": "#include \"lib.h\"\nint twice(int x) { return x * 2; }\nint never_called(void) { return 1; }\n",
    "lib.h": "int twice(int x);\nint never_called(void);\n",
    "build.sh": "set -e\n$CC main.c lib.c -o app\n./app\n",
}


@c_container
def test_c_container_observes_call_edges_and_io_kinds_without_values(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    for name, text in C_IO_SAMPLE.items():
        (root / name).write_text(text, encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    capsys.readouterr()
    repository = AnalysisRepository(Path(db))
    project = repository.list_projects()[0]
    index = ProjectIndex.load(repository, project.project_id)
    outcome = DynamicAnalysisService().run(project, DynamicPermission(allow_run=True, command=("sh", "build.sh"), collector="c"), index, repository)
    assert outcome.run.status == RunStatus.COMPLETED and outcome.run.collector == "c-gcov+c-instrument", outcome.stderr_tail
    calls = {(o.name, o.target_name): o.count for o in outcome.observations if o.kind == ObservationKind.CALL}
    assert calls == {("main", "helper"): 3, ("helper", "twice"): 3}  # 呼び出しの辺（回数つき）。libc の関数は、プロジェクトの辺ではない
    io = {(o.name, o.detail["kind"], o.detail["detail"]) for o in outcome.observations if o.kind == ObservationKind.IO}
    assert {("main", "env_read", "HOME"), ("main", "file_read", "system"), ("main", "file_write", "project")} <= io  # 種類・場所の種類・環境変数の名前だけ
    assert "secret-value-must-not-be-recorded" not in json.dumps([o.detail for o in outcome.observations]) and "out.txt" not in json.dumps([o.detail for o in outcome.observations])
    diff = compare(index, outcome.run, outcome.observations)
    assert diff.static_only == [] and diff.observed_only == [] and len(diff.both) == 2  # 辺を観測できるので、静的との比較ができる（両方に2本）
    assert not any("呼び出しの辺を観測できない" in n for n in diff.notes)
    repository.close()
    capsys.readouterr()
    assert main(["observed", "main", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "入出力: 環境変数の読み取り（HOME）" in out and "ファイルの書き込み（project）" in out


INSTRUMENT_TEXT = GCOV_TEXT + """\
==CALLS
2\thelper\t/tmp/ci/src/m.c:2\tmain\t/tmp/ci/src/m.c:6
1\thelper\t/tmp/ci/src/m.c:2\tmain\t/tmp/ci/src/m.c:7
3\tinline_fn\t/tmp/ci/src/lib/../util.h:1\thelper\t/tmp/ci/src/m.c:2
5\tprintf\t/usr/include/stdio.h:9\tmain\t/tmp/ci/src/m.c:6
==IO
1\tfile_read\tmain\t/tmp/ci/src/m.c:6\tsystem
2\tfile_write\tmain\t/tmp/ci/src/m.c:7\tproject
1\tenv_read\tmain\t/tmp/ci/src/m.c:6\tHOME
==DROPPED\t0\t1
"""


def test_parse_instrumentation_sections_merges_edges_and_drops_library_functions() -> None:
    data = c_observation.parse(INSTRUMENT_TEXT, 0)
    assert data["mechanism"] == "gcov+instrument" and data["dropped_edges"] == 0 and data["dropped_io"] == 1
    edges = {(c["caller"]["qualname"], c["callee"]["qualname"]): (c["count"], c["caller"]["line"]) for c in data["calls"]}
    assert edges == {("main", "helper"): (3, 6), ("helper", "inline_fn"): (3, 2)}  # 呼び出し箇所が複数でも1本にまとめ（回数は合算・行は最小）、ライブラリ（printf）の辺は含めない
    assert {(i["kind"], i["detail"], i["count"]) for i in data["io"]} == {("file_read", "system", 1), ("file_write", "project", 2), ("env_read", "HOME", 1)}
    assert c_observation.parse(GCOV_TEXT, 0)["mechanism"] == "gcov"  # 計装の記録が無ければ、gcov だけ（辺は観測できていない）

"""対象のプログラムを、コンテナの中で実行する。**このリポジトリで、外部のプログラムを起動してよい唯一のモジュール。**

実行できるのは、`PermittedRun`（許可が揃っていることを確認済みの実行）だけで、`PermittedRun` は
`DynamicPermission.check()` を通ったときにだけ作られる。許可なしにここへ到達する経路はない（AGENTS.md §10-4）。

コンテナの設定（既定は最も厳しい）: ネットワーク遮断・対象は読み取り専用・root でない利用者・全 capability 削除・
メモリとプロセス数と時間の上限・渡す環境変数は許可リストのみ。収集器（pycollect.py）はコンテナの中で動き、
結果のJSONだけを、実行ごとの一時ディレクトリへ書き出す。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from codeinsight.dynamic.permission import DynamicPermission
from codeinsight.dynamic.sandbox import SandboxPolicy

DEFAULT_IMAGE = "python:3.12-slim"
COLLECTOR_SCRIPT = Path(__file__).parent / "runtime" / "pycollect.py"
CONTAINER_TARGET = "/target"
CONTAINER_COLLECTOR = "/collector/pycollect.py"
CONTAINER_OUT = "/out"
MAX_OUTPUT_BYTES = 20_000_000


class ExecutorError(RuntimeError):
    """実行の準備ができない（コンテナの仕組みがない、など）。対象は実行されていない。"""


@dataclass(frozen=True)
class PermittedRun:
    """許可の確認を通った実行。`create` 以外では作らない。"""

    permission: DynamicPermission
    root: Path
    image: str

    @classmethod
    def create(cls, permission: DynamicPermission, root: Path, image: str = DEFAULT_IMAGE) -> PermittedRun:
        permission.check()
        if permission.sandbox_backend != "container":
            raise ExecutorError("現在の実行機能は、コンテナ（--sandbox container）だけに対応しています。")
        return cls(permission, root.resolve(), image)


@dataclass
class ExecutionResult:
    timed_out: bool
    returncode: int | None
    duration_seconds: float
    observation: dict | None  # 収集器が書いたJSON（無ければ None）
    stdout_tail: str = ""
    stderr_tail: str = ""
    docker_argv: list[str] = field(default_factory=list)  # 監査用（環境変数の値は含めない）


def docker_binary() -> str:
    path = shutil.which("docker")
    if path is None:
        raise ExecutorError("docker が見つかりません。コンテナでの実行には Docker が必要です。")
    return path


def image_present(image: str) -> bool:
    try:
        result = subprocess.run([docker_binary(), "image", "inspect", image], capture_output=True, timeout=30, check=False)
    except (ExecutorError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def container_user() -> str:
    """コンテナ内の利用者。root でない。通常は、実行した利用者と同じ uid:gid（Linux では、他人に読めない対象や作業領域を読み書きするために必要）。
    root で実行している場合だけ、権限のない 65534（nobody）にする。"""

    uid, gid = os.getuid(), os.getgid()
    return f"{uid}:{gid}" if uid != 0 else "65534:65534"


def build_argv(run: PermittedRun, out_dir: Path, container_name: str) -> tuple[list[str], list[str]]:
    """docker run の引数を作る。戻り値は (実行用の引数, 記録用の引数)。環境変数の値は、実行用にだけ渡す。"""

    permission = run.permission
    policy = SandboxPolicy.from_permission(permission)
    base = [
        "run", "--rm", "--name", container_name,
        "--network", "bridge" if policy.network == "allowed" else "none",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", container_user(),
        "--memory", f"{policy.memory_mb}m", "--pids-limit", str(policy.max_processes),
        "--ulimit", f"fsize={policy.max_file_size_mb * 1024 * 1024}",
        "-v", f"{run.root}:{CONTAINER_TARGET}:ro",
        "-v", f"{COLLECTOR_SCRIPT.resolve()}:{CONTAINER_COLLECTOR}:ro",
        "-v", f"{out_dir}:{CONTAINER_OUT}:rw",
        "-w", CONTAINER_TARGET,
        "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=" + CONTAINER_TARGET,
    ]
    audit = list(base)
    env_args: list[str] = []
    for name in permission.env_allowlist:
        if name in os.environ:
            env_args += ["-e", f"{name}={os.environ[name]}"]
            audit += ["-e", f"{name}=<値は記録しない>"]
    tail = [
        run.image, "python", CONTAINER_COLLECTOR, "--root", CONTAINER_TARGET, "--out", f"{CONTAINER_OUT}/observation.json",
        "--", *permission.command,
    ]
    return base + env_args + tail, audit + tail


def execute(run: PermittedRun) -> ExecutionResult:
    """コンテナで実行し、収集器の出力を返す。時間の上限を超えたら、コンテナを止める。"""

    docker = docker_binary()
    if not image_present(run.image):
        raise ExecutorError(
            f"コンテナのイメージ {run.image} がありません。取得（docker pull {run.image}）は自動では行いません。"
            "利用者が取得するか、--image で取得済みのイメージを指定してください。"
        )
    out_dir = Path(tempfile.mkdtemp(prefix="codeinsight-dyn-"))
    out_dir.chmod(0o777)  # コンテナ内の非特権利用者が書けるように
    name = f"codeinsight-dyn-{out_dir.name.rsplit('-', 1)[-1]}"
    argv, audit = build_argv(run, out_dir, name)
    started = time.monotonic()
    timed_out = False
    try:
        try:
            completed = subprocess.run(
                [docker, *argv], capture_output=True, timeout=run.permission.timeout_seconds, check=False,
            )
            returncode, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            subprocess.run([docker, "kill", name], capture_output=True, timeout=30, check=False)
            returncode, stdout, stderr = None, exc.stdout or b"", exc.stderr or b""
        duration = time.monotonic() - started
        observation = None
        output = out_dir / "observation.json"
        if output.is_file() and output.stat().st_size <= MAX_OUTPUT_BYTES:
            try:
                observation = json.loads(output.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                observation = None
        return ExecutionResult(
            timed_out, returncode, duration, observation,
            stdout[-2000:].decode("utf-8", "replace"), stderr[-2000:].decode("utf-8", "replace"), ["docker", *audit],
        )
    finally:
        if not run.permission.keep_workdir:
            shutil.rmtree(out_dir, ignore_errors=True)

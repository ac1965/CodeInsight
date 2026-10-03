"""サンドボックス（隔離）の方針。既定は最も厳しい設定。実行の仕組み（バックエンド）は未実装。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Protocol

from codeinsight.dynamic.permission import DynamicPermission


@dataclass(frozen=True)
class SandboxPolicy:
    network: str = "none"  # none / allowed
    target_mount: str = "read-only"  # 対象のディレクトリは読み取り専用（または複製）
    workdir: str = "temporary"  # 書き込みは、実行ごとの一時ディレクトリに限る
    timeout_seconds: float = 60.0
    memory_mb: int = 1024
    max_processes: int = 64
    max_file_size_mb: int = 100
    env: tuple[str, ...] = ()  # 渡す環境変数の名前（許可リストのみ）
    run_as: str = "unprivileged"  # root で実行しない
    backend: str = "container"

    @classmethod
    def from_permission(cls, permission: DynamicPermission) -> SandboxPolicy:
        return cls(
            network="allowed" if permission.allow_network else "none",
            timeout_seconds=permission.timeout_seconds,
            env=permission.env_allowlist,
            backend=permission.sandbox_backend,
            workdir="kept" if permission.keep_workdir else "temporary",
        )

    def describe(self) -> dict:
        return dict(vars(self) | {"env": list(self.env)})


class SandboxBackend(Protocol):
    """隔離して実行する仕組み。実装時は、この Protocol を満たす。"""

    name: str

    def available(self) -> bool: ...


class ContainerBackend:
    """コンテナ（Docker/Podman）。第一候補。現時点では実行できない（利用可能かの確認のみ）。"""

    name = "container"

    def available(self) -> bool:
        return shutil.which("docker") is not None or shutil.which("podman") is not None


class BubblewrapBackend:
    """Linux の bubblewrap。現時点では実行できない（利用可能かの確認のみ）。"""

    name = "bwrap"

    def available(self) -> bool:
        return shutil.which("bwrap") is not None


BACKENDS: dict[str, SandboxBackend] = {"container": ContainerBackend(), "bwrap": BubblewrapBackend()}

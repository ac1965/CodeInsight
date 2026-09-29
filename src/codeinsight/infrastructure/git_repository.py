from __future__ import annotations

import subprocess
from pathlib import Path


class GitRepository:
    """対象ディレクトリのGitリポジトリ識別・リビジョン取得を行う。

    読み取り専用のgitコマンド（rev-parse）のみを実行し、対象リポジトリの
    状態を変更するコマンドは実行しない（AGENTS.md 4.4: 対象ソースコードを
    無断で変更しない）。
    """

    def is_git_repository(self, path: Path) -> bool:
        output = self._run(path, ["rev-parse", "--is-inside-work-tree"])
        return output is not None and output.strip() == "true"

    def current_revision(self, path: Path) -> str | None:
        if not self.is_git_repository(path):
            return None
        return self._run(path, ["rev-parse", "HEAD"])

    def _run(self, path: Path, args: list[str]) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Commit:
    hash: str
    author: str
    date: str
    subject: str
    files: tuple[str, ...] = ()


def _parse_commits(output: str) -> list[Commit]:
    commits = []
    for line in output.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4 and len(parts[0]) >= 7:
            commits.append(Commit(parts[0], parts[1], parts[2], parts[3]))
    return commits


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

    def file_is_modified(self, root: Path, relative_path: str) -> bool:
        """作業ツリーのファイルが、HEADから変更されているか（未コミットの変更があるか）。"""

        output = self._run(root, ["status", "--porcelain", "--", relative_path])
        return bool(output)

    def commits_for_lines(
        self, root: Path, relative_path: str, start: int, end: int, limit: int = 10, timeout: int = 20
    ) -> list[Commit] | None:
        """指定の行範囲に触れたコミット（新しい順）。履歴を取得できなければ None。

        `git log -L` による読み取り専用の取得。行範囲は HEAD のファイル内容に対するものなので、
        作業ツリーで変更されている場合は呼び出し側で対応のずれに注意する。
        """

        output = self._run(
            root,
            ["log", f"-L{start},{end}:{relative_path}", "-s", f"-n{limit}",
             "--date=short", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s"],
            timeout=timeout,
        )
        return None if output is None else _parse_commits(output)

    def file_history(self, root: Path, max_commits: int = 2000, timeout: int = 60) -> list[Commit] | None:
        """最近のコミットと、それぞれが変更したファイル（読み取り専用）。"""

        output = self._run(
            root,
            ["log", f"-n{max_commits}", "--name-only", "--date=short",
             "--pretty=format:%x1e%H%x1f%an%x1f%ad%x1f%s"],
            timeout=timeout,
        )
        if output is None:
            return None
        commits = []
        # _run は出力の前後の空白を除くが、先頭の区切り文字(\x1e)は残る前提。先頭の空要素は捨てる。
        for block in [b for b in output.split("\x1e") if b.strip()]:
            header, _, files = block.partition("\n")
            commit = _parse_commits(header)
            if commit:
                commits.append(Commit(commit[0].hash, commit[0].author, commit[0].date, commit[0].subject, tuple(f for f in files.splitlines() if f.strip())))
        return commits

    def _run(self, path: Path, args: list[str], timeout: int = 10) -> str | None:
        try:
            completed = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout.strip()

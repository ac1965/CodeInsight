from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pathspec

from codeinsight.domain.project import Project


@dataclass(frozen=True)
class ScannedFile:
    """走査によって列挙されたファイル1件を表す。"""

    absolute_path: Path
    relative_path: str


class FileScanner:
    """プロジェクトルート配下を走査し、解析対象ファイル候補を列挙する。

    * `.gitignore` を尊重する（Project.configuration.respect_gitignore）。
    * `Project.configuration` の既定除外ディレクトリ・ファイルを適用する。
    * シンボリックリンクは、プロジェクトルート外を指すものと、既に訪問済み
      の実体（循環参照）を指すものを辿らない。
    """

    def scan(self, project: Project) -> list[ScannedFile]:
        root = project.root_path.resolve()
        spec = (
            self._load_gitignore(root)
            if project.configuration.respect_gitignore
            else None
        )
        results: list[ScannedFile] = []
        visited_real_dirs: set[Path] = set()
        self._walk(
            root=root,
            current=root,
            spec=spec,
            exclude_dirs=set(project.configuration.exclude_dirs),
            exclude_files=set(project.configuration.exclude_files),
            visited_real_dirs=visited_real_dirs,
            results=results,
        )
        return results

    def _load_gitignore(self, root: Path) -> pathspec.PathSpec | None:
        gitignore_path = root / ".gitignore"
        if not gitignore_path.is_file():
            return None
        lines = gitignore_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return pathspec.PathSpec.from_lines("gitignore", lines)

    def _walk(
        self,
        root: Path,
        current: Path,
        spec: pathspec.PathSpec | None,
        exclude_dirs: set[str],
        exclude_files: set[str],
        visited_real_dirs: set[Path],
        results: list[ScannedFile],
    ) -> None:
        try:
            real_current = current.resolve()
        except OSError:
            return
        if current.is_symlink() and not self._is_within(real_current, root):
            return
        if real_current in visited_real_dirs:
            return
        visited_real_dirs.add(real_current)

        try:
            entries = sorted(current.iterdir())
        except OSError:
            return

        for entry in entries:
            relative = entry.relative_to(root).as_posix()
            if entry.is_dir():
                if entry.name in exclude_dirs:
                    continue
                if spec is not None and spec.match_file(relative + "/"):
                    continue
                self._walk(
                    root, entry, spec, exclude_dirs, exclude_files, visited_real_dirs, results
                )
            elif entry.is_file():
                if entry.name in exclude_files:
                    continue
                if entry.is_symlink():
                    try:
                        resolved = entry.resolve()
                    except OSError:
                        continue
                    if not self._is_within(resolved, root):
                        continue
                if spec is not None and spec.match_file(relative):
                    continue
                results.append(ScannedFile(absolute_path=entry, relative_path=relative))

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ProjectConfiguration:
    """プロジェクトごとの解析設定。"""

    exclude_dirs: tuple[str, ...] = (
        ".git",
        "build",
        "dist",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
    )
    exclude_files: tuple[str, ...] = ()
    respect_gitignore: bool = True


@dataclass
class Project:
    """解析対象リポジトリを表すドメインモデル。"""

    project_id: str
    root_path: Path
    name: str
    repository_revision: str | None = None
    configuration: ProjectConfiguration = field(default_factory=ProjectConfiguration)

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
        "vendor",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
    )
    exclude_files: tuple[str, ...] = ()
    respect_gitignore: bool = True
    compile_commands_dir: str | None = None  # 解析に使った compile_commands.json のあるディレクトリ（Cの関数単位の解析で再利用する）
    compile_commands_hash: str | None = None  # 前回の解析時の compile_commands.json の内容のハッシュ（変わったら、C/C++ を解析し直す）。未記録は None、無しは空文字


@dataclass
class Project:
    """解析対象リポジトリを表すドメインモデル。"""

    project_id: str
    root_path: Path
    name: str
    repository_revision: str | None = None
    configuration: ProjectConfiguration = field(default_factory=ProjectConfiguration)

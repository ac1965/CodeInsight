from __future__ import annotations

from pathlib import Path

from codeinsight.domain.project import Project, ProjectConfiguration
from codeinsight.infrastructure.file_scanner import FileScanner


def _make_project(root: Path, configuration: ProjectConfiguration | None = None) -> Project:
    return Project(
        project_id="test-project",
        root_path=root,
        name="test",
        configuration=configuration or ProjectConfiguration(),
    )


def test_scans_files_recursively(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "src" / "b.c").write_text("int x;\n", encoding="utf-8")

    files = FileScanner().scan(_make_project(tmp_path))
    relative_paths = {f.relative_path for f in files}
    assert relative_paths == {"src/a.py", "src/b.c"}


def test_respects_default_exclude_dirs(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "generated.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    files = FileScanner().scan(_make_project(tmp_path))
    relative_paths = {f.relative_path for f in files}
    assert relative_paths == {"main.py"}


def test_respects_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.py\n*.log\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "kept.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "debug.log").write_text("log\n", encoding="utf-8")

    files = FileScanner().scan(_make_project(tmp_path))
    relative_paths = {f.relative_path for f in files}
    # .gitignore自体はignored.py/*.logのいずれにも一致しないため、
    # 走査結果には含まれる（言語フィルタリングは呼び出し側の責務）。
    assert relative_paths == {"kept.py", ".gitignore"}


def test_avoids_symlink_cycle(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "real" / "loop").symlink_to(tmp_path, target_is_directory=True)

    files = FileScanner().scan(_make_project(tmp_path))
    relative_paths = {f.relative_path for f in files}
    assert relative_paths == {"real/a.py"}


def test_ignores_symlink_escaping_root(tmp_path: Path, tmp_path_factory) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)

    files = FileScanner().scan(_make_project(tmp_path))
    assert files == []


def test_configuration_can_disable_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("x = 1\n", encoding="utf-8")

    project = _make_project(tmp_path, ProjectConfiguration(respect_gitignore=False))
    files = FileScanner().scan(project)
    relative_paths = {f.relative_path for f in files}
    assert relative_paths == {"ignored.py", ".gitignore"}

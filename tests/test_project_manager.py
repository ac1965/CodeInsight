from __future__ import annotations

from pathlib import Path

import pytest

from codeinsight.application.project_manager import ProjectManager
from codeinsight.infrastructure.analysis_repository import AnalysisRepository


def test_register_and_list_projects(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()

    with AnalysisRepository(tmp_path / "db.sqlite") as repo:
        manager = ProjectManager(repo)
        project = manager.register(project_root, name="demo")

        assert project.name == "demo"
        assert project.root_path == project_root.resolve()

        projects = manager.list()
        assert [p.project_id for p in projects] == [project.project_id]


def test_register_rejects_missing_directory(tmp_path: Path) -> None:
    with AnalysisRepository(tmp_path / "db.sqlite") as repo:
        manager = ProjectManager(repo)
        with pytest.raises(NotADirectoryError):
            manager.register(tmp_path / "does_not_exist")


def test_remove_project(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()

    with AnalysisRepository(tmp_path / "db.sqlite") as repo:
        manager = ProjectManager(repo)
        project = manager.register(project_root)
        manager.remove(project.project_id)
        assert manager.get(project.project_id) is None

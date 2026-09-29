from __future__ import annotations

import uuid
from pathlib import Path

from codeinsight.domain.project import Project, ProjectConfiguration
from codeinsight.infrastructure.analysis_repository import AnalysisRepository
from codeinsight.infrastructure.git_repository import GitRepository


class ProjectManager:
    """プロジェクト（解析対象リポジトリ）の登録・削除・一覧を担当する（3.1）。"""

    def __init__(
        self,
        repository: AnalysisRepository,
        git_repository: GitRepository | None = None,
    ) -> None:
        self._repository = repository
        self._git_repository = git_repository or GitRepository()

    def register(
        self,
        root_path: Path,
        name: str | None = None,
        configuration: ProjectConfiguration | None = None,
    ) -> Project:
        root_path = root_path.resolve()
        if not root_path.is_dir():
            raise NotADirectoryError(f"プロジェクトルートが存在しません: {root_path}")

        project = Project(
            project_id=str(uuid.uuid4()),
            root_path=root_path,
            name=name or root_path.name,
            repository_revision=self._git_repository.current_revision(root_path),
            configuration=configuration or ProjectConfiguration(),
        )
        self._repository.save_project(project)
        return project

    def get(self, project_id: str) -> Project | None:
        return self._repository.get_project(project_id)

    def list(self) -> list[Project]:
        return self._repository.list_projects()

    def remove(self, project_id: str) -> None:
        self._repository.delete_project(project_id)

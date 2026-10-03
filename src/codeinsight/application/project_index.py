from __future__ import annotations

from dataclasses import dataclass, field

from codeinsight.domain import Dependency, Reference, SourceFile, Symbol
from codeinsight.infrastructure.analysis_repository import AnalysisRepository


@dataclass
class ProjectIndex:
    """1プロジェクト分の解析結果を、検索・ナビゲーション用にメモリへ読み込んだもの。

    各サービスが同じ読み込み・索引化の処理を重複して持たないための共通部品。
    保存済みの解析結果のスナップショットであり、ソースが変更されても更新されない
    （古さは FreshnessService で判定する）。
    """

    project_id: str
    files: dict[str, SourceFile]
    symbols: dict[str, Symbol]
    references: list[Reference]
    dependencies: list[Dependency]
    _paths: dict[str, SourceFile] = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, repository: AnalysisRepository, project_id: str) -> "ProjectIndex":
        files = {f.file_id: f for f in repository.list_source_files(project_id)}
        return cls(
            project_id=project_id,
            files=files,
            symbols={s.symbol_id: s for s in repository.list_symbols_for_project(project_id)},
            references=repository.list_references_for_project(project_id),
            dependencies=[
                d
                for d in repository.list_dependencies_for_project(project_id)
                if d.is_visible
            ],
            _paths={f.relative_path: f for f in files.values()},
        )

    def path_of(self, file_id: str) -> str:
        source_file = self.files.get(file_id)
        return source_file.relative_path if source_file else "<不明>"

    def file_by_path(self, relative_path: str) -> SourceFile | None:
        return self._paths.get(relative_path)

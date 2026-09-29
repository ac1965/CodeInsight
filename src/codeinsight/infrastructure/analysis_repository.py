from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from codeinsight.domain import (
    AnalysisFileStatus,
    AnalysisResult,
    AnalysisStatus,
    Language,
    Project,
    SourceFile,
    Symbol,
    SymbolKind,
)
from codeinsight.domain.project import ProjectConfiguration

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    name TEXT NOT NULL,
    repository_revision TEXT,
    exclude_dirs TEXT NOT NULL,
    exclude_files TEXT NOT NULL,
    respect_gitignore INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS source_files (
    file_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    relative_path TEXT NOT NULL,
    language TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    last_analyzed_at TEXT,
    analysis_status TEXT NOT NULL,
    UNIQUE(project_id, relative_path)
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES source_files(file_id),
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    parent_symbol_id TEXT,
    decorators TEXT NOT NULL,
    base_classes TEXT NOT NULL,
    is_async INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_file_id ON symbols(file_id);

CREATE TABLE IF NOT EXISTS analysis_results (
    analysis_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    analyzer_version TEXT NOT NULL,
    analysis_timestamp TEXT NOT NULL,
    status TEXT NOT NULL,
    warnings TEXT NOT NULL,
    errors TEXT NOT NULL
);
"""


class AnalysisRepository:
    """解析結果のSQLiteへの永続化を担当する（3.10: 解析結果の保存）。

    対象リポジトリの非侵襲性を保つため、DBファイルは呼び出し側が指定する
    パス（既定では対象リポジトリの外部）に保存することを前提とする。
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(db_path))
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "AnalysisRepository":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- Project ---

    def save_project(self, project: Project) -> None:
        cfg = project.configuration
        self._connection.execute(
            """
            INSERT INTO projects (project_id, root_path, name, repository_revision,
                                   exclude_dirs, exclude_files, respect_gitignore)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                root_path=excluded.root_path,
                name=excluded.name,
                repository_revision=excluded.repository_revision,
                exclude_dirs=excluded.exclude_dirs,
                exclude_files=excluded.exclude_files,
                respect_gitignore=excluded.respect_gitignore
            """,
            (
                project.project_id,
                str(project.root_path),
                project.name,
                project.repository_revision,
                json.dumps(list(cfg.exclude_dirs)),
                json.dumps(list(cfg.exclude_files)),
                int(cfg.respect_gitignore),
            ),
        )
        self._connection.commit()

    def get_project(self, project_id: str) -> Project | None:
        row = self._connection.execute(
            "SELECT project_id, root_path, name, repository_revision, exclude_dirs, "
            "exclude_files, respect_gitignore FROM projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return self._project_from_row(row) if row is not None else None

    def list_projects(self) -> list[Project]:
        rows = self._connection.execute(
            "SELECT project_id, root_path, name, repository_revision, exclude_dirs, "
            "exclude_files, respect_gitignore FROM projects"
        ).fetchall()
        return [self._project_from_row(row) for row in rows]

    def delete_project(self, project_id: str) -> None:
        self._connection.execute(
            "DELETE FROM symbols WHERE file_id IN "
            "(SELECT file_id FROM source_files WHERE project_id = ?)",
            (project_id,),
        )
        self._connection.execute("DELETE FROM source_files WHERE project_id = ?", (project_id,))
        self._connection.execute(
            "DELETE FROM analysis_results WHERE project_id = ?", (project_id,)
        )
        self._connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        self._connection.commit()

    @staticmethod
    def _project_from_row(row: tuple) -> Project:
        return Project(
            project_id=row[0],
            root_path=Path(row[1]),
            name=row[2],
            repository_revision=row[3],
            configuration=ProjectConfiguration(
                exclude_dirs=tuple(json.loads(row[4])),
                exclude_files=tuple(json.loads(row[5])),
                respect_gitignore=bool(row[6]),
            ),
        )

    # --- SourceFile ---

    def save_source_file(self, source_file: SourceFile) -> None:
        self._connection.execute(
            """
            INSERT INTO source_files (file_id, project_id, relative_path, language,
                                       content_hash, last_analyzed_at, analysis_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                relative_path=excluded.relative_path,
                language=excluded.language,
                content_hash=excluded.content_hash,
                last_analyzed_at=excluded.last_analyzed_at,
                analysis_status=excluded.analysis_status
            """,
            (
                source_file.file_id,
                source_file.project_id,
                source_file.relative_path,
                source_file.language.value,
                source_file.content_hash,
                source_file.last_analyzed_at.isoformat()
                if source_file.last_analyzed_at
                else None,
                source_file.analysis_status.value,
            ),
        )
        self._connection.commit()

    def get_source_file_by_path(self, project_id: str, relative_path: str) -> SourceFile | None:
        row = self._connection.execute(
            "SELECT file_id, project_id, relative_path, language, content_hash, "
            "last_analyzed_at, analysis_status FROM source_files "
            "WHERE project_id = ? AND relative_path = ?",
            (project_id, relative_path),
        ).fetchone()
        return self._source_file_from_row(row) if row is not None else None

    def list_source_files(self, project_id: str) -> list[SourceFile]:
        rows = self._connection.execute(
            "SELECT file_id, project_id, relative_path, language, content_hash, "
            "last_analyzed_at, analysis_status FROM source_files WHERE project_id = ?",
            (project_id,),
        ).fetchall()
        return [self._source_file_from_row(row) for row in rows]

    @staticmethod
    def _source_file_from_row(row: tuple) -> SourceFile:
        return SourceFile(
            file_id=row[0],
            project_id=row[1],
            relative_path=row[2],
            language=Language(row[3]),
            content_hash=row[4],
            last_analyzed_at=datetime.fromisoformat(row[5]) if row[5] else None,
            analysis_status=AnalysisFileStatus(row[6]),
        )

    # --- Symbol ---

    def replace_symbols_for_file(self, file_id: str, symbols: list[Symbol]) -> None:
        """指定ファイルのシンボルを洗い替える（再解析時に古いシンボルを残さない）。"""

        self._connection.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        self._connection.executemany(
            """
            INSERT INTO symbols (symbol_id, file_id, name, qualified_name, kind,
                                  start_line, end_line, parent_symbol_id, decorators,
                                  base_classes, is_async)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    s.symbol_id,
                    s.file_id,
                    s.name,
                    s.qualified_name,
                    s.kind.value,
                    s.start_line,
                    s.end_line,
                    s.parent_symbol_id,
                    json.dumps(list(s.decorators)),
                    json.dumps(list(s.base_classes)),
                    int(s.is_async),
                )
                for s in symbols
            ],
        )
        self._connection.commit()

    def list_symbols_for_file(self, file_id: str) -> list[Symbol]:
        rows = self._connection.execute(
            "SELECT symbol_id, file_id, name, qualified_name, kind, start_line, "
            "end_line, parent_symbol_id, decorators, base_classes, is_async "
            "FROM symbols WHERE file_id = ?",
            (file_id,),
        ).fetchall()
        return [self._symbol_from_row(row) for row in rows]

    def list_symbols_for_project(self, project_id: str) -> list[Symbol]:
        rows = self._connection.execute(
            "SELECT s.symbol_id, s.file_id, s.name, s.qualified_name, s.kind, "
            "s.start_line, s.end_line, s.parent_symbol_id, s.decorators, "
            "s.base_classes, s.is_async FROM symbols s "
            "JOIN source_files f ON s.file_id = f.file_id WHERE f.project_id = ?",
            (project_id,),
        ).fetchall()
        return [self._symbol_from_row(row) for row in rows]

    @staticmethod
    def _symbol_from_row(row: tuple) -> Symbol:
        return Symbol(
            symbol_id=row[0],
            file_id=row[1],
            name=row[2],
            qualified_name=row[3],
            kind=SymbolKind(row[4]),
            start_line=row[5],
            end_line=row[6],
            parent_symbol_id=row[7],
            decorators=tuple(json.loads(row[8])),
            base_classes=tuple(json.loads(row[9])),
            is_async=bool(row[10]),
        )

    # --- AnalysisResult ---

    def save_analysis_result(self, result: AnalysisResult) -> None:
        self._connection.execute(
            """
            INSERT INTO analysis_results (analysis_id, project_id, analyzer_version,
                                           analysis_timestamp, status, warnings, errors)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.analysis_id,
                result.project_id,
                result.analyzer_version,
                result.analysis_timestamp.isoformat(),
                result.status.value,
                json.dumps(list(result.warnings)),
                json.dumps(list(result.errors)),
            ),
        )
        self._connection.commit()

    def list_analysis_results(self, project_id: str) -> list[AnalysisResult]:
        rows = self._connection.execute(
            "SELECT analysis_id, project_id, analyzer_version, analysis_timestamp, "
            "status, warnings, errors FROM analysis_results WHERE project_id = ? "
            "ORDER BY analysis_timestamp DESC",
            (project_id,),
        ).fetchall()
        return [
            AnalysisResult(
                analysis_id=row[0],
                project_id=row[1],
                analyzer_version=row[2],
                analysis_timestamp=datetime.fromisoformat(row[3]),
                status=AnalysisStatus(row[4]),
                warnings=tuple(json.loads(row[5])),
                errors=tuple(json.loads(row[6])),
            )
            for row in rows
        ]

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from codeinsight.domain import (
    AnalysisFileStatus,
    AnalysisResult,
    AnalysisStatus,
    Confidence,
    Dependency,
    DependencyKind,
    Language,
    Project,
    ProjectConfiguration,
    Reference,
    ReferenceKind,
    ResolutionStatus,
    SourceFile,
    SourceLocation,
    Symbol,
    SymbolKind,
)
from codeinsight.infrastructure.schema import (
    BASE_SCHEMA,
    SCHEMA_VERSION,
    V2_COLUMNS,
    V2_TABLES,
)

_PROJECT_COLUMNS = (
    "project_id, root_path, name, repository_revision, exclude_dirs, "
    "exclude_files, respect_gitignore"
)
_FILE_COLUMNS = (
    "file_id, project_id, relative_path, language, content_hash, "
    "last_analyzed_at, analysis_status, analyzer_version"
)
_SYMBOL_COLUMNS = (
    "symbol_id, file_id, name, qualified_name, kind, start_line, end_line, "
    "parent_symbol_id, decorators, base_classes, is_async, usr, summary"
)
_REFERENCE_COLUMNS = (
    "reference_id, file_id, source_symbol_id, target_name, target_key, target_symbol_id, "
    "reference_kind, start_line, end_line, resolution_status, confidence, note"
)
_DEPENDENCY_COLUMNS = (
    "dependency_id, source_file_id, target_name, target_key, target_file_id, "
    "dependency_kind, start_line, end_line, resolution_status, confidence, note, is_candidate"
)
_RESULT_COLUMNS = (
    "analysis_id, project_id, analyzer_version, analysis_timestamp, status, "
    "warnings, errors, repository_revision"
)


class AnalysisRepository:
    """解析結果のSQLiteへの永続化を担当する（3.10: 解析結果の保存）。

    対象リポジトリの非侵襲性を保つため、DBファイルは呼び出し側が指定する
    パス（既定では対象リポジトリの外部）に保存することを前提とする。

    書き込みは既定で個別にコミットする。`transaction()` の内側では、ブロックの
    終了時にまとめてコミットし、例外時はロールバックする（解析途中の不整合な
    保存状態を残さない）。
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(db_path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._transaction_depth = 0
        self._initialize_schema()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "AnalysisRepository":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- スキーマ・トランザクション ---

    def _initialize_schema(self) -> None:
        connection = self._connection
        has_tables = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'"
            ).fetchone()
            is not None
        )
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"DBのスキーマ(version {version})がこのバージョンより新しいため開けません。"
            )
        if not has_tables:
            connection.executescript(BASE_SCHEMA)
        for table, column, definition in V2_COLUMNS:
            existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        connection.executescript(V2_TABLES)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._transaction_depth += 1
        try:
            yield
        except BaseException:
            self._transaction_depth -= 1
            if self._transaction_depth == 0:
                self._connection.rollback()
            raise
        else:
            self._transaction_depth -= 1
            if self._transaction_depth == 0:
                self._connection.commit()

    def _commit(self) -> None:
        if self._transaction_depth == 0:
            self._connection.commit()

    # --- Project ---

    def save_project(self, project: Project) -> None:
        cfg = project.configuration
        self._connection.execute(
            f"""
            INSERT INTO projects ({_PROJECT_COLUMNS})
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
        self._commit()

    def get_project(self, project_id: str) -> Project | None:
        row = self._connection.execute(
            f"SELECT {_PROJECT_COLUMNS} FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        return _project_from_row(row) if row is not None else None

    def list_projects(self) -> list[Project]:
        rows = self._connection.execute(f"SELECT {_PROJECT_COLUMNS} FROM projects").fetchall()
        return [_project_from_row(row) for row in rows]

    def delete_project(self, project_id: str) -> None:
        file_ids = [
            row["file_id"]
            for row in self._connection.execute(
                "SELECT file_id FROM source_files WHERE project_id = ?", (project_id,)
            )
        ]
        self._delete_files(file_ids)
        self._connection.execute("DELETE FROM analysis_results WHERE project_id = ?", (project_id,))
        self._connection.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
        self._commit()

    # --- SourceFile ---

    def save_source_file(self, source_file: SourceFile) -> None:
        self._connection.execute(
            f"""
            INSERT INTO source_files ({_FILE_COLUMNS})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                relative_path=excluded.relative_path,
                language=excluded.language,
                content_hash=excluded.content_hash,
                last_analyzed_at=excluded.last_analyzed_at,
                analysis_status=excluded.analysis_status,
                analyzer_version=excluded.analyzer_version
            """,
            (
                source_file.file_id,
                source_file.project_id,
                source_file.relative_path,
                source_file.language.value,
                source_file.content_hash,
                source_file.last_analyzed_at.isoformat() if source_file.last_analyzed_at else None,
                source_file.analysis_status.value,
                source_file.analyzer_version,
            ),
        )
        self._commit()

    def get_source_file_by_path(self, project_id: str, relative_path: str) -> SourceFile | None:
        row = self._connection.execute(
            f"SELECT {_FILE_COLUMNS} FROM source_files WHERE project_id = ? AND relative_path = ?",
            (project_id, relative_path),
        ).fetchone()
        return _source_file_from_row(row) if row is not None else None

    def get_source_file(self, file_id: str) -> SourceFile | None:
        row = self._connection.execute(
            f"SELECT {_FILE_COLUMNS} FROM source_files WHERE file_id = ?", (file_id,)
        ).fetchone()
        return _source_file_from_row(row) if row is not None else None

    def list_source_files(self, project_id: str) -> list[SourceFile]:
        rows = self._connection.execute(
            f"SELECT {_FILE_COLUMNS} FROM source_files WHERE project_id = ? "
            "ORDER BY relative_path",
            (project_id,),
        ).fetchall()
        return [_source_file_from_row(row) for row in rows]

    def delete_source_files(self, file_ids: Iterable[str]) -> None:
        """ファイルとそのシンボル・参照・依存関係を削除する（ソースが無くなった場合など）。"""

        self._delete_files(list(file_ids))
        self._commit()

    def _delete_files(self, file_ids: list[str]) -> None:
        for file_id in file_ids:
            self._clear_file_facts(file_id)
            self._connection.execute("DELETE FROM source_files WHERE file_id = ?", (file_id,))

    # --- 解析結果（シンボル・参照・依存関係） ---

    def _clear_file_facts(self, file_id: str) -> None:
        self._connection.execute("DELETE FROM references_ WHERE file_id = ?", (file_id,))
        self._connection.execute("DELETE FROM dependencies WHERE source_file_id = ?", (file_id,))
        self._connection.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))

    def clear_file_facts(self, file_id: str) -> None:
        """解析に失敗したファイルの古い解析結果を残さないために全て削除する。"""

        self._clear_file_facts(file_id)
        self._commit()

    def replace_file_facts(
        self,
        file_id: str,
        symbols: list[Symbol],
        references: list[Reference] | None = None,
        dependencies: list[Dependency] | None = None,
    ) -> None:
        """指定ファイルのシンボル・参照・依存関係を洗い替える。"""

        self._clear_file_facts(file_id)
        self._insert_symbols(symbols)
        self._connection.executemany(
            f"INSERT INTO references_ ({_REFERENCE_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.reference_id,
                    r.source_location.file_id,
                    r.source_symbol_id,
                    r.target_name,
                    r.target_key,
                    r.target_symbol_id,
                    r.reference_kind.value,
                    r.source_location.start_line,
                    r.source_location.end_line,
                    r.resolution_status.value,
                    r.confidence.value,
                    r.note,
                )
                for r in references or []
            ],
        )
        self._connection.executemany(
            f"INSERT INTO dependencies ({_DEPENDENCY_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    d.dependency_id,
                    d.source_file_id,
                    d.target_name,
                    d.target_key,
                    d.target_file_id,
                    d.dependency_kind.value,
                    d.evidence_location.start_line,
                    d.evidence_location.end_line,
                    d.resolution_status.value,
                    d.confidence.value,
                    d.note,
                    int(d.is_candidate),
                )
                for d in dependencies or []
            ],
        )
        self._commit()

    def _insert_symbols(self, symbols: list[Symbol]) -> None:
        self._connection.executemany(
            f"INSERT INTO symbols ({_SYMBOL_COLUMNS}) VALUES ({', '.join('?' * 13)})",
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
                    s.usr,
                    s.summary,
                )
                for s in symbols
            ],
        )

    def replace_symbols_for_file(self, file_id: str, symbols: list[Symbol]) -> None:
        """指定ファイルのシンボルのみを洗い替える（参照・依存関係は保持する）。"""

        self._connection.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        self._insert_symbols(symbols)
        self._commit()

    def update_resolutions(
        self, references: Iterable[Reference], dependencies: Iterable[Dependency]
    ) -> None:
        """ReferenceResolverによる解決結果を保存する。"""

        self._connection.executemany(
            "UPDATE references_ SET target_symbol_id = ?, resolution_status = ?, "
            "confidence = ?, note = ? WHERE reference_id = ?",
            [
                (
                    r.target_symbol_id,
                    r.resolution_status.value,
                    r.confidence.value,
                    r.note,
                    r.reference_id,
                )
                for r in references
            ],
        )
        self._connection.executemany(
            "UPDATE dependencies SET target_file_id = ?, resolution_status = ?, "
            "confidence = ?, note = ? WHERE dependency_id = ?",
            [
                (
                    d.target_file_id,
                    d.resolution_status.value,
                    d.confidence.value,
                    d.note,
                    d.dependency_id,
                )
                for d in dependencies
            ],
        )
        self._commit()

    def list_symbols_for_file(self, file_id: str) -> list[Symbol]:
        rows = self._connection.execute(
            f"SELECT {_SYMBOL_COLUMNS} FROM symbols WHERE file_id = ? ORDER BY start_line",
            (file_id,),
        ).fetchall()
        return [_symbol_from_row(row) for row in rows]

    def list_symbols_for_project(self, project_id: str) -> list[Symbol]:
        rows = self._connection.execute(
            "SELECT " + ", ".join(f"s.{c.strip()}" for c in _SYMBOL_COLUMNS.split(","))
            + " FROM symbols s JOIN source_files f ON s.file_id = f.file_id "
            "WHERE f.project_id = ?",
            (project_id,),
        ).fetchall()
        return [_symbol_from_row(row) for row in rows]

    def get_symbol(self, symbol_id: str) -> Symbol | None:
        row = self._connection.execute(
            f"SELECT {_SYMBOL_COLUMNS} FROM symbols WHERE symbol_id = ?", (symbol_id,)
        ).fetchone()
        return _symbol_from_row(row) if row is not None else None

    def list_references_for_project(self, project_id: str) -> list[Reference]:
        rows = self._connection.execute(
            "SELECT " + ", ".join(f"r.{c.strip()}" for c in _REFERENCE_COLUMNS.split(","))
            + " FROM references_ r JOIN source_files f ON r.file_id = f.file_id "
            "WHERE f.project_id = ?",
            (project_id,),
        ).fetchall()
        return [_reference_from_row(row) for row in rows]

    def list_dependencies_for_project(self, project_id: str) -> list[Dependency]:
        rows = self._connection.execute(
            "SELECT " + ", ".join(f"d.{c.strip()}" for c in _DEPENDENCY_COLUMNS.split(","))
            + " FROM dependencies d JOIN source_files f ON d.source_file_id = f.file_id "
            "WHERE f.project_id = ?",
            (project_id,),
        ).fetchall()
        return [_dependency_from_row(row) for row in rows]

    # --- AnalysisResult ---

    def save_analysis_result(self, result: AnalysisResult) -> None:
        self._connection.execute(
            f"INSERT INTO analysis_results ({_RESULT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.analysis_id,
                result.project_id,
                result.analyzer_version,
                result.analysis_timestamp.isoformat(),
                result.status.value,
                json.dumps(list(result.warnings)),
                json.dumps(list(result.errors)),
                result.repository_revision,
            ),
        )
        self._commit()

    def list_analysis_results(self, project_id: str) -> list[AnalysisResult]:
        rows = self._connection.execute(
            f"SELECT {_RESULT_COLUMNS} FROM analysis_results WHERE project_id = ? "
            "ORDER BY analysis_timestamp DESC",
            (project_id,),
        ).fetchall()
        return [
            AnalysisResult(
                analysis_id=row["analysis_id"],
                project_id=row["project_id"],
                analyzer_version=row["analyzer_version"],
                analysis_timestamp=datetime.fromisoformat(row["analysis_timestamp"]),
                status=AnalysisStatus(row["status"]),
                warnings=tuple(json.loads(row["warnings"])),
                errors=tuple(json.loads(row["errors"])),
                repository_revision=row["repository_revision"],
            )
            for row in rows
        ]


def _project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        project_id=row["project_id"],
        root_path=Path(row["root_path"]),
        name=row["name"],
        repository_revision=row["repository_revision"],
        configuration=ProjectConfiguration(
            exclude_dirs=tuple(json.loads(row["exclude_dirs"])),
            exclude_files=tuple(json.loads(row["exclude_files"])),
            respect_gitignore=bool(row["respect_gitignore"]),
        ),
    )


def _source_file_from_row(row: sqlite3.Row) -> SourceFile:
    return SourceFile(
        file_id=row["file_id"],
        project_id=row["project_id"],
        relative_path=row["relative_path"],
        language=Language(row["language"]),
        content_hash=row["content_hash"],
        last_analyzed_at=(
            datetime.fromisoformat(row["last_analyzed_at"]) if row["last_analyzed_at"] else None
        ),
        analysis_status=AnalysisFileStatus(row["analysis_status"]),
        analyzer_version=row["analyzer_version"],
    )


def _symbol_from_row(row: sqlite3.Row) -> Symbol:
    return Symbol(
        symbol_id=row["symbol_id"],
        file_id=row["file_id"],
        name=row["name"],
        qualified_name=row["qualified_name"],
        kind=SymbolKind(row["kind"]),
        start_line=row["start_line"],
        end_line=row["end_line"],
        parent_symbol_id=row["parent_symbol_id"],
        decorators=tuple(json.loads(row["decorators"])),
        base_classes=tuple(json.loads(row["base_classes"])),
        is_async=bool(row["is_async"]),
        usr=row["usr"],
        summary=row["summary"],
    )


def _reference_from_row(row: sqlite3.Row) -> Reference:
    return Reference(
        reference_id=row["reference_id"],
        source_symbol_id=row["source_symbol_id"],
        target_name=row["target_name"],
        target_key=row["target_key"],
        reference_kind=ReferenceKind(row["reference_kind"]),
        source_location=SourceLocation(row["file_id"], row["start_line"], row["end_line"]),
        resolution_status=ResolutionStatus(row["resolution_status"]),
        target_symbol_id=row["target_symbol_id"],
        confidence=Confidence(row["confidence"]),
        note=row["note"],
    )


def _dependency_from_row(row: sqlite3.Row) -> Dependency:
    return Dependency(
        dependency_id=row["dependency_id"],
        source_file_id=row["source_file_id"],
        target_name=row["target_name"],
        target_key=row["target_key"],
        dependency_kind=DependencyKind(row["dependency_kind"]),
        evidence_location=SourceLocation(
            row["source_file_id"], row["start_line"], row["end_line"]
        ),
        resolution_status=ResolutionStatus(row["resolution_status"]),
        target_file_id=row["target_file_id"],
        confidence=Confidence(row["confidence"]),
        note=row["note"],
        is_candidate=bool(row["is_candidate"]),
    )

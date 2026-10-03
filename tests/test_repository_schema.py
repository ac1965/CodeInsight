from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codeinsight.domain import Project
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.infrastructure.schema import BASE_SCHEMA, SCHEMA_VERSION


def test_new_database_gets_current_schema_version(tmp_path: Path) -> None:
    db = tmp_path / "new.sqlite"
    with AnalysisRepository(db):
        pass
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_phase1_database_is_migrated_and_forces_reanalysis(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite"
    legacy = sqlite3.connect(db)
    legacy.executescript(BASE_SCHEMA)  # バージョン未設定（Phase1）の構造
    legacy.execute(
        "INSERT INTO projects VALUES ('p', '/tmp/x', 'x', NULL, '[]', '[]', 1)"
    )
    legacy.execute(
        "INSERT INTO source_files VALUES ('f', 'p', 'a.py', 'python', 'h', NULL, 'analyzed')"
    )
    legacy.commit()
    legacy.close()

    with AnalysisRepository(db) as repo:
        (source_file,) = repo.list_source_files("p")
        # 解析器バージョンが空 → 次回の解析で必ず再解析される
        assert source_file.analyzer_version == ""
        assert repo.list_references_for_project("p") == []
    assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    db = tmp_path / "future.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError):
        AnalysisRepository(db)


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    with AnalysisRepository(tmp_path / "db.sqlite") as repo:
        with pytest.raises(ValueError):
            with repo.transaction():
                repo.save_project(Project(project_id="p", root_path=tmp_path, name="p"))
                raise ValueError("途中で失敗")
        assert repo.get_project("p") is None

        with repo.transaction():
            repo.save_project(Project(project_id="q", root_path=tmp_path, name="q"))
        assert repo.get_project("q") is not None

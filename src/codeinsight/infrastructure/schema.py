from __future__ import annotations

# スキーマのバージョン。変更時は analysis_repository.py の _MIGRATIONS に移行処理を追加する。
SCHEMA_VERSION = 2

# Phase1（バージョン未設定=0）と共通のテーブル。
BASE_SCHEMA = """
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

# バージョン2で追加された列・テーブル（新規DBにも移行時にも同じ定義を適用する）。
V2_COLUMNS = (
    ("symbols", "usr", "TEXT"),
    ("source_files", "analyzer_version", "TEXT NOT NULL DEFAULT ''"),
    ("analysis_results", "repository_revision", "TEXT"),
)

V2_TABLES = """
CREATE TABLE IF NOT EXISTS references_ (
    reference_id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL REFERENCES source_files(file_id),
    source_symbol_id TEXT NOT NULL,
    target_name TEXT NOT NULL,
    target_key TEXT,
    target_symbol_id TEXT,
    reference_kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    resolution_status TEXT NOT NULL,
    confidence TEXT NOT NULL,
    note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_references_file_id ON references_(file_id);
CREATE INDEX IF NOT EXISTS idx_references_source ON references_(source_symbol_id);
CREATE INDEX IF NOT EXISTS idx_references_target ON references_(target_symbol_id);

CREATE TABLE IF NOT EXISTS dependencies (
    dependency_id TEXT PRIMARY KEY,
    source_file_id TEXT NOT NULL REFERENCES source_files(file_id),
    target_name TEXT NOT NULL,
    target_key TEXT,
    target_file_id TEXT,
    dependency_kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    resolution_status TEXT NOT NULL,
    confidence TEXT NOT NULL,
    note TEXT NOT NULL,
    is_candidate INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dependencies_source ON dependencies(source_file_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_target ON dependencies(target_file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qualified_name ON symbols(qualified_name);
"""

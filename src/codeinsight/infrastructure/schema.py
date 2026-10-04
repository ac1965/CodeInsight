from __future__ import annotations

# スキーマのバージョン。変更時は analysis_repository.py の _MIGRATIONS に移行処理を追加する。
SCHEMA_VERSION = 8

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

# バージョン2以降で追加された列・テーブル（新規DBにも移行時にも同じ定義を適用する）。
V2_COLUMNS = (
    ("symbols", "usr", "TEXT"),
    ("symbols", "summary", "TEXT NOT NULL DEFAULT ''"),
    ("source_files", "analyzer_version", "TEXT NOT NULL DEFAULT ''"),
    ("analysis_results", "repository_revision", "TEXT"),
    ("projects", "compile_commands_dir", "TEXT"),  # v5: Cの関数単位の解析で、解析時と同じコンパイル設定を使うため
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

# バージョン4: AI解説。解析結果（事実）のテーブルとは分離し、モデル・入力のハッシュ・根拠ファイルの
# ハッシュ・検証結果を保持する（AGENTS.md §1.2-2, §10.3）。
# バージョン6: 外部ツール（SARIF）の指摘。解析結果（事実）のテーブルとは分離し、ツール名・版・取り込み時の
# ファイルのハッシュを保持する。再解析しても消えず、ソースが変わった場合は古い指摘として判定する。
V6_TABLES = """
CREATE TABLE IF NOT EXISTS external_findings (
    finding_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    tool TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_external_findings_project ON external_findings(project_id, path, start_line);
"""

# バージョン7: 外部のコード索引（SCIP）。事実のテーブルとは分離し、ツール名・版・取り込み時のファイルのハッシュを保持する。
V7_TABLES = """
CREATE TABLE IF NOT EXISTS external_indexes (
    index_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    tool TEXT NOT NULL,
    tool_version TEXT NOT NULL,
    project_root TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    documents INTEGER NOT NULL,
    occurrences INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS external_index_files (
    index_id TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (index_id, path)
);
CREATE TABLE IF NOT EXISTS external_index_occurrences (
    index_id TEXT NOT NULL,
    path TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    start_char INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    roles INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_index_occ ON external_index_occurrences(index_id, path, start_line);
CREATE TABLE IF NOT EXISTS external_index_symbols (
    index_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    display_name TEXT NOT NULL,
    kind INTEGER NOT NULL,
    documentation TEXT NOT NULL
);
"""

V4_TABLES = """
CREATE TABLE IF NOT EXISTS explanations (
    explanation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    target_kind TEXT NOT NULL,
    target TEXT NOT NULL,
    created_at TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    context_hash TEXT NOT NULL,
    source_hashes TEXT NOT NULL,
    repository_revision TEXT,
    analyzer_version TEXT NOT NULL,
    text TEXT NOT NULL,
    validation TEXT NOT NULL,
    status TEXT NOT NULL,
    ai_generated INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_explanations_project ON explanations(project_id, target_kind, target);
"""

# バージョン8: 動的解析（実行して観測した結果）。事実・外部ツールの結果とは別に、実行の許可の内容・収集器・実行時の
# ファイルのハッシュとともに保存する。観測は「この実行で起きたこと」であり、観測されなかったことは意味しない。
V8_TABLES = """
CREATE TABLE IF NOT EXISTS dynamic_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    started_at TEXT NOT NULL,
    command TEXT NOT NULL,
    permission TEXT NOT NULL,
    sandbox TEXT NOT NULL,
    collector TEXT NOT NULL,
    collector_version TEXT NOT NULL,
    status TEXT NOT NULL,
    revision TEXT,
    source_hashes TEXT NOT NULL,
    exit_code INTEGER,
    duration_seconds REAL,
    notes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dynamic_runs_project ON dynamic_runs(project_id, started_at);
CREATE TABLE IF NOT EXISTS dynamic_observations (
    observation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES dynamic_runs(run_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    symbol_id TEXT,
    target_path TEXT NOT NULL,
    target_name TEXT NOT NULL,
    target_symbol_id TEXT,
    count INTEGER NOT NULL,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dynamic_obs_run ON dynamic_observations(run_id, kind);
CREATE INDEX IF NOT EXISTS idx_dynamic_obs_symbol ON dynamic_observations(symbol_id);
"""

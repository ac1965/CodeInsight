from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class Language(enum.Enum):
    """解析対象言語。未対応言語は UNKNOWN として扱い、解析対象から除外する。"""

    C = "c"
    PYTHON = "python"
    UNKNOWN = "unknown"


class AnalysisFileStatus(enum.Enum):
    """ファイル単位の解析状況。"""

    PENDING = "pending"
    ANALYZED = "analyzed"
    FAILED = "failed"
    SKIPPED = "skipped"


class FileFreshness(enum.Enum):
    """保存済み解析結果と現在のソースコードの対応状況（3.10: 古い結果の明示）。"""

    FRESH = "fresh"  # 現在のハッシュが解析時と一致
    STALE = "stale"  # ソースが解析後に変更されている
    MISSING = "missing"  # ファイルが存在しない
    UNANALYZED = "unanalyzed"  # 解析に成功していない


@dataclass
class SourceFile:
    """プロジェクト内の1ソースファイルを表すドメインモデル。"""

    file_id: str
    project_id: str
    relative_path: str
    language: Language
    content_hash: str
    last_analyzed_at: datetime | None = None
    analysis_status: AnalysisFileStatus = AnalysisFileStatus.PENDING
    analyzer_version: str = ""

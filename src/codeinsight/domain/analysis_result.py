from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class AnalysisStatus(enum.Enum):
    """プロジェクト単位の解析実行結果。"""

    SUCCESS = "success"  # 全ファイルが正常に解析できた
    PARTIAL = "partial"  # 一部のファイルでエラー/警告が発生した
    FAILED = "failed"  # 解析そのものが実行できなかった


@dataclass
class AnalysisResult:
    """1回の解析実行の記録を表すドメインモデル。

    解析エラーは握りつぶさず、warnings/errors に記録する（AGENTS.md 10.6）。
    """

    analysis_id: str
    project_id: str
    analyzer_version: str
    analysis_timestamp: datetime
    status: AnalysisStatus
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
    repository_revision: str | None = None

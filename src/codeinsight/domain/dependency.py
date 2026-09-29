from __future__ import annotations

import enum
from dataclasses import dataclass


class DependencyKind(enum.Enum):
    """ファイル・モジュール間の依存種別。Phase1では未使用（Phase2で実装予定）。"""

    INCLUDE = "include"  # C: #include
    IMPORT = "import"  # Python: import / from import


@dataclass
class Dependency:
    """ファイル・モジュール間の依存関係を表すドメインモデル（Phase2で実装予定）。"""

    dependency_id: str
    source_id: str
    target_id: str
    dependency_kind: DependencyKind
    evidence_location: str
    confidence: float  # 1.0=構文解析で確定, 1.0未満=推定（表示上区別する）

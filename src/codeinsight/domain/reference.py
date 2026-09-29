from __future__ import annotations

import enum
from dataclasses import dataclass


class ReferenceKind(enum.Enum):
    """シンボル間の参照種別。Phase1では未使用（Phase2で参照解決を実装する）。"""

    CALL = "call"
    IMPORT = "import"
    INHERITANCE = "inheritance"
    TYPE_USE = "type_use"


class ResolutionStatus(enum.Enum):
    """参照解決の状態。関数ポインタや動的呼び出しなど、静的に確定できない
    参照先は UNRESOLVED として明示し、推測による確定を行わない（AGENTS.md 10.2）。
    """

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"


@dataclass
class Reference:
    """シンボル間の参照関係を表すドメインモデル（Phase2で実装予定）。"""

    reference_id: str
    source_symbol_id: str
    target_symbol_id: str | None
    reference_kind: ReferenceKind
    source_location: str
    resolution_status: ResolutionStatus

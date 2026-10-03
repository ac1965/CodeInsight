from __future__ import annotations

import enum
from dataclasses import dataclass

from codeinsight.domain.location import Confidence, SourceLocation


class ReferenceKind(enum.Enum):
    """シンボル間の参照種別。"""

    CALL = "call"  # 関数呼び出し（呼び出し式）
    FUNCTION_REF = "function_ref"  # 関数名の参照（呼び出しを伴わない。アドレス取得など）
    VARIABLE_REF = "variable_ref"  # グローバル/static変数の参照
    INHERITANCE = "inheritance"
    TYPE_USE = "type_use"
    IMPORT = "import"


class ResolutionStatus(enum.Enum):
    """参照解決の状態。関数ポインタや動的呼び出しなど、静的に確定できない
    参照先は UNRESOLVED として明示し、推測による確定を行わない（AGENTS.md 10.2）。

    RESOLVED:   プロジェクト内のシンボルに解決できた。
    UNRESOLVED: 静的に参照先を確定できない（動的呼び出し、型不明など）。
    AMBIGUOUS:  複数の候補があり一意に定まらない。
    EXTERNAL:   プロジェクト外（標準/外部ライブラリ、システムヘッダー）を指す。
    """

    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    EXTERNAL = "external"


@dataclass
class Reference:
    """シンボル間の参照関係を表すドメインモデル。

    target_name は人が読む参照先の表記、target_key は解決に用いる照合キー
    （C: libclangのUSR、Python: 修飾名候補）。解決前は target_symbol_id が None。
    """

    reference_id: str
    source_symbol_id: str
    target_name: str
    target_key: str | None
    reference_kind: ReferenceKind
    source_location: SourceLocation
    resolution_status: ResolutionStatus = ResolutionStatus.UNRESOLVED
    target_symbol_id: str | None = None
    confidence: Confidence = Confidence.CONFIRMED
    note: str = ""

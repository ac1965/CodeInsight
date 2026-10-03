from __future__ import annotations

import enum
from dataclasses import dataclass


class Confidence(enum.Enum):
    """解決結果の確からしさ。確定と推定を区別して表示するために用いる（3.5）。

    CONFIRMED: 構文・シンボル表から静的に確定できた。
    INFERRED:  静的に候補を特定できたが、実行時の挙動（継承によるオーバーライド、
               デコレータによる置換など）で結果が変わる可能性がある。

    解決できなかった関係（UNRESOLVED/AMBIGUOUS）には意味を持たない。
    """

    CONFIRMED = "confirmed"
    INFERRED = "inferred"


@dataclass(frozen=True)
class SourceLocation:
    """解析根拠となるソースコード上の位置（ファイルと行範囲）。"""

    file_id: str
    start_line: int
    end_line: int

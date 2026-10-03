from __future__ import annotations

import enum
from dataclasses import dataclass

from codeinsight.domain.location import Confidence, SourceLocation
from codeinsight.domain.reference import ResolutionStatus


class DependencyKind(enum.Enum):
    """ファイル・モジュール間の依存種別。"""

    INCLUDE = "include"  # C: #include
    IMPORT = "import"  # Python: import / from import


@dataclass
class Dependency:
    """ファイル・モジュール間の依存関係を表すドメインモデル。

    source_file_id は依存元ファイル、target_file_id は解決できた依存先ファイル
    （プロジェクト外・未解決なら None）。evidence_location が根拠となる記述位置。
    """

    dependency_id: str
    source_file_id: str
    target_name: str
    target_key: str | None
    dependency_kind: DependencyKind
    evidence_location: SourceLocation
    resolution_status: ResolutionStatus = ResolutionStatus.UNRESOLVED
    target_file_id: str | None = None
    confidence: Confidence = Confidence.CONFIRMED
    note: str = ""
    # `from X import name` の name がサブモジュールかどうかは、他ファイルを見ないと
    # 分からない。そのため候補として記録し、モジュールに解決できた場合のみ表示する。
    is_candidate: bool = False

    @property
    def is_visible(self) -> bool:
        """利用者に表示する依存関係か（解決できなかった候補は表示しない）。"""

        return not self.is_candidate or self.resolution_status == ResolutionStatus.RESOLVED

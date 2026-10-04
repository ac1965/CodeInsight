"""動的解析（実行して観測した結果）のドメインモデル。設計は DYNAMIC_ANALYSIS.md。

観測は、静的解析の事実（確定/推定/未解決/外部）とは別の区分として保存・表示する。
「この実行でこうだった」という存在の証拠であり、観測されなかったことは、存在しないことを意味しない。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class ObservationKind(enum.Enum):
    CALL = "call"  # 実際に起きた関数呼び出し
    COVERAGE = "coverage"  # 実行された関数・行
    FAILURE = "failure"  # 例外・終了コード・シグナル・exit/abort
    IO = "io"  # ファイル・ネットワーク・プロセス・環境変数のアクセス
    SHAPE = "shape"  # 引数・戻り値の型/形（値そのものは保存しない）
    TIMING = "timing"  # 呼び出し回数・時間


class RunStatus(enum.Enum):
    COMPLETED = "completed"
    FAILED = "failed"  # 実行は開始したが、異常終了した
    TIMEOUT = "timeout"
    DENIED = "denied"  # 許可が揃わず、実行しなかった
    TARGET_MODIFIED = "target_modified"  # 実行後に対象のハッシュが変わっていた（対象を変更しない原則に反する）


@dataclass
class DynamicRun:
    """動的解析の1回の実行の記録（許可の内容を含む監査用の記録）。"""

    run_id: str
    project_id: str
    started_at: datetime
    command: tuple[str, ...]
    permission: dict  # 許可の内容（ネットワーク・環境変数・サンドボックス）
    sandbox: dict
    collector: str
    collector_version: str
    status: RunStatus
    revision: str | None = None
    source_hashes: dict[str, str] = field(default_factory=dict)  # 実行時のファイルのハッシュ（古い観測の判定に使う）
    exit_code: int | None = None
    duration_seconds: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Observation:
    """実行中に観測された1件。ソースの位置（ファイル・行）とシンボルに結びつける。"""

    observation_id: str
    run_id: str
    kind: ObservationKind
    file_id: str | None = None
    symbol_id: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    target_symbol_id: str | None = None  # call: 呼び出し先
    target_name: str = ""
    path: str = ""  # 観測された位置のファイル（相対パス）。解析結果と対応づけられなくても残る
    name: str = ""  # 観測された関数の修飾名（収集器が報告したもの）
    target_path: str = ""
    count: int = 1
    detail: dict = field(default_factory=dict)

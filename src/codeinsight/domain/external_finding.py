"""外部ツール（SARIFを出力する静的解析ツール）の指摘。

CodeInsight自身の解析結果（事実）ではなく、外部ツールが報告した内容を、出どころ付きで保持する。
CodeInsightは、外部ツールの指摘を確定した事実として扱わず、ツール名・版・規則名・取り込んだ時点の
ファイルの内容ハッシュとともに保存し、ソースが変更された場合は古い指摘として示す（AGENTS.md §1.2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ExternalFinding:
    finding_id: str
    project_id: str
    tool: str  # 例: cppcheck
    tool_version: str
    rule_id: str
    level: str  # error / warning / note / none（SARIFの level。指定がなければ warning）
    message: str
    path: str  # プロジェクトのルートからの相対パス
    start_line: int
    end_line: int
    content_hash: str  # 取り込み時点の、対象ファイルの内容ハッシュ（古い指摘の判定に使う）
    imported_at: datetime
    source_name: str  # 取り込んだSARIFファイルの名前
    source_sha256: str  # 取り込んだSARIFファイルのハッシュ（同じ内容の再取り込みを置き換えるため）

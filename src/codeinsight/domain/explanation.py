from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class ExplanationStatus(enum.Enum):
    """AI解説の検証状態。解析器の出力（事実）とは別に管理する（AGENTS.md §1.2-2）。

    VERIFIED:   引用がすべて検証でき、根拠のない主張・存在しない識別子がない。
    PARTIAL:    引用に誤りはないが、根拠の示されていない主張が含まれる。
    UNVERIFIED: 引用の誤り・存在しない識別子がある、または根拠が一つも示されていない。
    """

    VERIFIED = "verified"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"


@dataclass
class Explanation:
    """AIが生成した解説。解析結果（事実）ではなく、解析結果を入力として生成した「解説」。

    どのモデルが、どの入力（コンテキストとプロンプトのハッシュ）から、どのソースの内容
    （ファイルのハッシュ）を元に生成したかを保持し、ソースが変更された場合に古い解説だと
    判定できるようにする（AGENTS.md §1.2-3）。
    """

    explanation_id: str
    project_id: str
    target_kind: str  # symbol / file / path / question
    target: str
    created_at: datetime
    provider: str
    model: str
    prompt_hash: str
    context_hash: str
    source_hashes: dict[str, str]  # 解説の根拠として渡したファイル -> 解析時の内容ハッシュ
    text: str
    validation: dict
    status: ExplanationStatus
    repository_revision: str | None = None
    analyzer_version: str = ""
    ai_generated: bool = field(default=True, init=False)

"""解決状態の表示ラベル（CLI・TUIで共用する）。"""

from __future__ import annotations

from codeinsight.domain import ResolutionStatus

STATUS_LABEL = {
    ResolutionStatus.RESOLVED: "解決",
    ResolutionStatus.UNRESOLVED: "未解決",
    ResolutionStatus.AMBIGUOUS: "曖昧",
    ResolutionStatus.EXTERNAL: "外部",
}

STATIC_NOTE = "注意: 静的に確認できた関係であり、実行順序や実際に通る経路を示すものではありません。"


def status_text(status: ResolutionStatus, confidence_inferred: bool) -> str:
    """解決状態の表示。解決でも、推定のものは確定と区別する（AGENTS.md 3.5.1）。"""

    label = STATUS_LABEL[status]
    return f"{label}(推定)" if status == ResolutionStatus.RESOLVED and confidence_inferred else label

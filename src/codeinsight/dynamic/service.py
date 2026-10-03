"""動的解析のユースケース（スタブ）。`plan` は実行しない計画を作る。`run` は許可を確認するだけで、実行しない。"""

from __future__ import annotations

from dataclasses import dataclass, field

from codeinsight.domain import Project
from codeinsight.dynamic.collectors import CollectorSpec, collectors_for
from codeinsight.dynamic.permission import DynamicAnalysisNotImplemented, DynamicPermission
from codeinsight.dynamic.sandbox import BACKENDS, SandboxPolicy


@dataclass
class DynamicPlan:
    """実行したらどうなるかの計画。**これを作るときも、何も実行しない。**"""

    project: str
    permission: dict
    permitted: bool
    denied_reasons: list[str]
    sandbox: dict
    sandbox_available: bool | None  # 隔離の仕組みが、この計算機で利用できるか（None: 隔離なし/不明）
    collectors: list[CollectorSpec] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class DynamicAnalysisService:
    def plan(self, project: Project, permission: DynamicPermission, languages: set[str]) -> DynamicPlan:
        policy = SandboxPolicy.from_permission(permission)
        backend = BACKENDS.get(permission.sandbox_backend)
        reasons = permission.violations()
        notes = ["この計画は表示のみで、対象のプログラムは実行していません。"]
        if permission.sandbox_backend == "none":
            notes.append("隔離なしの実行は、対象の環境に影響する恐れがあります（設計では既定で不可）。")
        if permission.allow_network:
            notes.append("ネットワークが許可されています。実行が外部へ通信する可能性があります。")
        if any(c.needs_rebuild for c in collectors_for(languages)):
            notes.append("Cの収集器には再コンパイルを伴うものがあります（対象の外の作業領域で行い、ビルドの許可とは別に実行の許可も必要）。")
        notes.append("動的解析の実行は未実装です（設計とスタブのみ。DYNAMIC_ANALYSIS.md）。")
        return DynamicPlan(
            project.name, permission.summary(), not reasons, reasons, policy.describe(),
            backend.available() if backend else None, collectors_for(languages), notes,
        )

    def run(self, project: Project, permission: DynamicPermission) -> None:
        """許可を確認する。揃っていなければ例外。揃っていても、実行は未実装のため、何も実行せず例外にする。"""

        permission.check()
        raise DynamicAnalysisNotImplemented("動的解析の実行は未実装です（設計とスタブのみ）。対象は実行されていません。")

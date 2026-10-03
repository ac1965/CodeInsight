"""動的解析の許可モデル。

対象のプログラムの実行は、利用者の明示的な許可がある場合に限る（AGENTS.md §3.11・§10-4）。
許可は、実行ごとに、コマンド・ネットワーク・環境変数・サンドボックスの内容を含めて明示し、記録する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ENV_ALLOW_RUN = "CODEINSIGHT_DYNAMIC_ALLOW_RUN"
MAX_TIMEOUT_SECONDS = 3600.0
SANDBOX_BACKENDS = ("container", "bwrap", "none")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DynamicPermissionError(Exception):
    """動的解析の許可が揃っていない。理由の一覧を持つ。"""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("動的解析を実行できません: " + " / ".join(reasons))
        self.reasons = reasons


class DynamicAnalysisNotImplemented(NotImplementedError):
    """動的解析の実行は、まだ実装されていない（設計とスタブのみ）。対象は実行されていない。"""


@dataclass(frozen=True)
class DynamicPermission:
    """実行の許可。既定はすべて拒否で、必要なものを個別に明示する。"""

    allow_run: bool = False  # 実行そのものの許可（--allow-run）
    command: tuple[str, ...] = ()  # 実行するコマンド（argv。シェルを介さない）。CodeInsight は推測して選ばない
    allow_network: bool = False  # ネットワークの許可（既定は遮断）
    env_allowlist: tuple[str, ...] = ()  # 渡す環境変数の名前。空から始め、ここにあるものだけ渡す
    sandbox_backend: str = "container"  # container / bwrap / none
    allow_unsandboxed: bool = False  # 隔離なしの実行の許可（sandbox_backend="none" のときに必要）
    timeout_seconds: float = 60.0
    keep_workdir: bool = False

    def violations(self) -> list[str]:
        """許可が揃っていない理由（空なら、許可が揃っている）。"""

        reasons: list[str] = []
        if not self.allow_run:
            reasons.append("実行の許可（--allow-run）がありません")
        if not self.command:
            reasons.append("実行するコマンドが指定されていません（-- の後に指定。CodeInsight はコマンドを推測して選びません）")
        if self.sandbox_backend not in SANDBOX_BACKENDS:
            reasons.append(f"サンドボックスの種類が不正です: {self.sandbox_backend}（{', '.join(SANDBOX_BACKENDS)}）")
        elif self.sandbox_backend == "none" and not self.allow_unsandboxed:
            reasons.append("隔離なしの実行には、追加の許可（--allow-unsandboxed）が必要です")
        if not 0 < self.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            reasons.append(f"時間の上限は 0 秒より大きく {int(MAX_TIMEOUT_SECONDS)} 秒以下にしてください")
        bad = [name for name in self.env_allowlist if not _ENV_NAME.match(name)]
        if bad:
            reasons.append(f"環境変数の名前が不正です: {', '.join(bad)}")
        return reasons

    @property
    def permitted(self) -> bool:
        return not self.violations()

    def check(self) -> None:
        """許可が揃っていなければ、理由つきで例外にする。実行の直前に必ず呼ぶ。"""

        reasons = self.violations()
        if reasons:
            raise DynamicPermissionError(reasons)

    def summary(self) -> dict:
        """記録・表示用の内容（環境変数は名前だけで、値は含めない）。"""

        return {
            "allow_run": self.allow_run, "command": list(self.command), "allow_network": self.allow_network,
            "env_allowlist": list(self.env_allowlist), "sandbox_backend": self.sandbox_backend,
            "allow_unsandboxed": self.allow_unsandboxed, "timeout_seconds": self.timeout_seconds, "keep_workdir": self.keep_workdir,
        }

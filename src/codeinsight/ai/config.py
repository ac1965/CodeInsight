from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlparse

from codeinsight.infrastructure.config import default_data_dir

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_CONTAINER_HOST = "host.docker.internal"
_TRUE = frozenset({"1", "true", "yes", "on"})
DEFAULT_BASE_URL = "http://localhost:11434/v1"  # Ollama の OpenAI 互換エンドポイント


class ConsentError(Exception):
    """ソースコード等をAIへ送信する許可が、明示的に与えられていない。"""


@dataclass(frozen=True)
class AIConfig:
    """AI利用の設定。ソースコードを外部（AI）へ送るため、既定では何も送信しない。

    * allow_send: 解析対象のソース・解析結果をAIへ送信してよいという明示的な許可（既定: False）。
    * allow_remote: 送信先が、この計算機（localhost/loopback）以外でもよいという追加の許可（既定: False）。
    * api_key は、ログ・表示・例外メッセージに出さない（repr/表示から除外）。
    """

    base_url: str = DEFAULT_BASE_URL
    model: str | None = None
    api_key: str | None = field(default=None, repr=False)
    allow_send: bool = False
    allow_remote: bool = False
    timeout: float = 600.0
    max_context_chars: int = 14000
    max_tokens: int | None = 2000
    temperature: float = 0.1
    include_source: bool = True  # False: 生のソース行を送らず、解析結果の事実（名前・位置・件数）だけを送る

    @property
    def host(self) -> str:
        return (urlparse(self.base_url).hostname or "").lower()

    @property
    def is_local(self) -> bool:
        """この計算機の中か。コンテナの中（CODEINSIGHT_IN_CONTAINER=1）では、ホストの Ollama を指す host.docker.internal も、この計算機とみなす
        （ホストとコンテナは同じ計算機。それ以外の名前は、これまでどおり外部）。"""

        if self.host == _CONTAINER_HOST and os.environ.get("CODEINSIGHT_IN_CONTAINER") == "1":
            return True
        return self.host in _LOCAL_HOSTS

    def check_consent(self, require_model: bool = True) -> None:
        """送信前の確認。許可が無い、または送信先が外部で追加の許可が無い場合は送信しない。"""

        if not self.allow_send:
            raise ConsentError(
                "ソースコードをAIへ送信する許可が設定されていません。送信内容は --dry-run で確認できます。"
                "送信してよい場合は --allow-send を付けるか、環境変数 CODEINSIGHT_AI_ALLOW_SEND=1 を設定してください。"
            )
        if not self.is_local and not self.allow_remote:
            raise ConsentError(
                f"送信先 {self.host} はこの計算機の外です。外部へソースコードを送信してよい場合に限り、"
                "--allow-remote（または CODEINSIGHT_AI_ALLOW_REMOTE=1）を追加してください。"
                "ローカルLLM（Ollama等）の利用では、送信はこの計算機の中で完結します。"
            )
        if require_model and not self.model:
            raise ConsentError("モデルが指定されていません。--ai-model または CODEINSIGHT_AI_MODEL を設定してください。")

    def redacted(self) -> dict[str, object]:
        """表示・ログ用の設定（APIキーは含めない）。"""

        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key": "設定あり" if self.api_key else "なし",
            "allow_send": self.allow_send,
            "allow_remote": self.allow_remote,
            "is_local": self.is_local,
            "include_source": self.include_source,
            "max_context_chars": self.max_context_chars,
        }


def load_ai_config(
    overrides: Mapping[str, object] | None = None,
    env: Mapping[str, str] | None = None,
    file_path: Path | None = None,
) -> AIConfig:
    """設定を、優先順位（コマンドライン > 環境変数 > 設定ファイル > 既定値）で解決する。

    設定ファイルは `~/.codeinsight/config.toml` の `[ai]` テーブル（CODEINSIGHT_DATA_DIR で場所を変更可）。
    """

    env = os.environ if env is None else env
    config = AIConfig()
    path = file_path or (default_data_dir() / "config.toml")
    try:
        data = dict(tomllib.loads(path.read_text(encoding="utf-8")).get("ai", {}))
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    data.pop("api_key", None)  # 秘密情報はファイルから読まない（環境変数のみ）。config_file_warnings が警告する
    config = _apply(config, data)

    env_values: dict[str, object] = {}
    for key, name in (
        ("base_url", "CODEINSIGHT_AI_BASE_URL"), ("model", "CODEINSIGHT_AI_MODEL"), ("api_key", "CODEINSIGHT_AI_API_KEY"),
        ("allow_send", "CODEINSIGHT_AI_ALLOW_SEND"), ("allow_remote", "CODEINSIGHT_AI_ALLOW_REMOTE"),
        ("timeout", "CODEINSIGHT_AI_TIMEOUT"), ("max_context_chars", "CODEINSIGHT_AI_MAX_CONTEXT"),
    ):
        if env.get(name):
            env_values[key] = env[name]
    config = _apply(config, env_values)
    return _apply(config, {k: v for k, v in (overrides or {}).items() if v is not None})


def _apply(config: AIConfig, values: Mapping[str, object]) -> AIConfig:
    changes: dict[str, object] = {}
    for key, value in values.items():
        if key in ("allow_send", "allow_remote", "include_source"):
            changes[key] = value if isinstance(value, bool) else str(value).lower() in _TRUE
        elif key in ("timeout", "temperature"):
            changes[key] = float(value)  # type: ignore[arg-type]
        elif key in ("max_context_chars", "max_tokens"):
            changes[key] = int(value)  # type: ignore[call-overload]
        elif key in ("base_url", "model", "api_key"):
            changes[key] = str(value)
    return replace(config, **changes)  # type: ignore[arg-type]


def config_file_warnings(file_path: Path | None = None) -> list[str]:
    """設定ファイルに、読み込まない秘密情報（api_key）が書かれている場合の警告。"""

    path = file_path or (default_data_dir() / "config.toml")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("ai", {})
    except (OSError, tomllib.TOMLDecodeError):
        return []
    if "api_key" in data:
        return [
            f"{path} の [ai] api_key は使われません（秘密情報をファイルに置かないため）。"
            "環境変数 CODEINSIGHT_AI_API_KEY を使い、ファイルからは削除してください。"
        ]
    return []

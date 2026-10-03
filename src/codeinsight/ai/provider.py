from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class AIProviderError(Exception):
    """AIプロバイダーとの通信・応答の失敗。メッセージにAPIキーは含めない。"""


@dataclass(frozen=True)
class Message:
    role: str  # system / user / assistant
    content: str


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class AIProvider(Protocol):
    """AIプロバイダーの共通インターフェース。解析基盤から独立し、切り替えられる（AGENTS.md §3.9）。"""

    name: str
    model: str

    def complete(self, messages: list[Message], *, temperature: float = 0.1, max_tokens: int | None = None) -> Completion:
        """メッセージ列から、AIの応答を1回生成する。失敗したら AIProviderError。"""
        ...


class OpenAICompatibleProvider:
    """OpenAI互換のChat Completions API（Ollama、LM Studio、vLLM、OpenAI等）。

    標準ライブラリのみで通信する。ローカルのOllamaでは、送信はこの計算機の中で完結する。
    """

    name = "openai-compatible"

    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout: float = 600.0) -> None:
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._timeout = timeout

    def complete(self, messages: list[Message], *, temperature: float = 0.1, max_tokens: int | None = None) -> Completion:
        body: dict[str, object] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        data = self._request("/chat/completions", json.dumps(body).encode("utf-8"))
        try:
            choice = data["choices"][0]["message"]
            text = choice.get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise AIProviderError("AIの応答の形式が想定と異なります（choices[0].message.content がありません）。") from exc
        usage = data.get("usage") or {}
        return Completion(
            text=_THINK.sub("", text).strip(),  # 思考過程（<think>）は回答に含めない
            model=str(data.get("model") or self.model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )

    def check(self) -> list[str]:
        """接続の確認（モデル一覧の取得）。ソースコードは送らない。"""

        data = self._request("/models", None)
        try:
            return [str(m["id"]) for m in data["data"]]
        except (KeyError, TypeError) as exc:
            raise AIProviderError("モデル一覧の形式が想定と異なります。") from exc

    def _request(self, path: str, payload: bytes | None) -> dict:
        request = urllib.request.Request(
            self._base_url + path,
            data=payload,
            method="POST" if payload is not None else "GET",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if self._api_key:
            request.add_header("Authorization", f"Bearer {self._api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(300).decode("utf-8", errors="replace").replace(self._api_key or "\0", "***")
            raise AIProviderError(f"AIがエラーを返しました（HTTP {exc.code}）: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise AIProviderError(
                f"AIに接続できません（{self._base_url}）: {reason}。"
                "AIを使わない機能（understand 等）は、そのまま利用できます。"
            ) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AIProviderError("AIの応答がJSONとして読み取れません。") from exc

"""ローカルのHTTPサーバー（標準ライブラリ）。読み取り専用のGETのみを受け付ける。

安全のための制約:
* ループバック（127.0.0.1・::1）にだけバインドする。外部のホストへ公開する指定は受け付けない。
* Hostヘッダーを検査する（DNSリバインディング対策）。
* 起動ごとのランダムなトークンを要求する（ページ: URLのクエリ、API: ヘッダー）。トークンはログに出さない。
* GET以外は拒否する。CORSは許可しない。書き込み・解析の実行・外部通信・AIへの送信のエンドポイントは、存在しない。
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from codeinsight.web.api import ApiError, BinaryResponse, ViewerApi
from codeinsight.web.app_page import content_security_policy, render_app

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
ENV_IN_CONTAINER = "CODEINSIGHT_IN_CONTAINER"


def bindable(host: str) -> bool:
    """バインドしてよいアドレス。ループバックのみ。ただし、コンテナの中（CODEINSIGHT_IN_CONTAINER=1。公式の Dockerfile が設定する）では、
    ホスト側へ公開するために `0.0.0.0` を許可する。この場合も、ホスト側では 127.0.0.1 にだけ公開すること（compose.yaml がそうする）。
    トークンとHostの検査は、そのまま有効。"""

    return host in LOOPBACK_HOSTS or (host == "0.0.0.0" and os.environ.get(ENV_IN_CONTAINER) == "1")
TOKEN_HEADER = "X-CodeInsight-Token"
MAX_URL = 4096


class ViewerServer(HTTPServer):
    def __init__(self, host: str, port: int, api: ViewerApi, token: str | None = None) -> None:
        if not bindable(host):
            raise ValueError(f"ループバック以外にはバインドできません: {host}（127.0.0.1・localhost・::1 のみ。コンテナ内に限り 0.0.0.0）")
        self.address_family = socket.AF_INET6 if host == "::1" else socket.AF_INET
        super().__init__((host, port), _Handler)
        self.api = api
        self.token = token or secrets.token_urlsafe(32)
        self.allowed_hosts = {f"{name}:{self.server_address[1]}" for name in ("127.0.0.1", "localhost")} | {f"[::1]:{self.server_address[1]}"}
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        host = "[::1]" if self.server_address[0] == "::1" else "127.0.0.1"  # 0.0.0.0（コンテナ内）の場合も、ホストから開く先は 127.0.0.1
        return f"http://{host}:{self.server_address[1]}/?token={self.token}"


class _Handler(BaseHTTPRequestHandler):
    server: ViewerServer
    timeout = 10
    server_version = "CodeInsightViewer"
    sys_version = ""

    # --- 応答 ---

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def log_message(self, format: str, *args: object) -> None:
        """アクセスログ。クエリ（トークン・検索語）は出さず、メソッドとパスだけを記録する。"""

        try:
            path = urlparse(self.path).path
        except ValueError:
            path = "?"
        print(f"{self.command} {path[:120]} → {args[1] if len(args) > 1 else ''}", file=sys.stderr)

    # --- 検査 ---

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def _token_ok(self, supplied: str | None) -> bool:
        return supplied is not None and hmac.compare_digest(supplied.encode(), self.server.token.encode())

    # --- メソッド ---

    def do_GET(self) -> None:
        if len(self.path) > MAX_URL:
            self._json(414, {"error": "URLが長すぎます"})
            return
        if not self._host_ok():
            self._json(403, {"error": "このHostでは受け付けません"})
            return
        try:
            parsed = urlparse(self.path)
        except ValueError:
            self._json(400, {"error": "不正なURLです"})
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/":
            tokens = query.get("token")
            if not self._token_ok(tokens[0] if tokens else None):
                self._json(401, {"error": "トークンが必要です（serve が表示したURLを開いてください）"})
                return
            nonce = secrets.token_urlsafe(16)
            page = render_app(self.server.token, nonce)
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8", {"Content-Security-Policy": content_security_policy(nonce)})
            return
        if not parsed.path.startswith("/api/"):
            self._json(404, {"error": "存在しないページです"})
            return
        if not self._token_ok(self.headers.get(TOKEN_HEADER)):
            self._json(401, {"error": "トークンが必要です"})
            return
        try:
            with self.server.lock:
                payload = self.api_call(parsed.path, query)
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message, **exc.extra})
            return
        except Exception:  # noqa: BLE001 - 内部の詳細（パス・スタックトレース）を、応答に出さない
            self._json(500, {"error": "内部エラーが発生しました"})
            return
        if isinstance(payload, BinaryResponse):
            self._send(200, payload.data, payload.content_type)
        else:
            self._json(200, payload)

    def api_call(self, path: str, query: dict[str, list[str]]) -> dict | BinaryResponse:
        return self.server.api.handle(path, query)

    def _method_not_allowed(self) -> None:
        self._send(405, b'{"error": "GET\\u4ee5\\u5916\\u306f\\u53d7\\u3051\\u4ed8\\u3051\\u307e\\u305b\\u3093"}', "application/json; charset=utf-8", {"Allow": "GET"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = _method_not_allowed

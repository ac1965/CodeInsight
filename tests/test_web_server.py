from __future__ import annotations

import http.client
import json
import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from codeinsight.application import NavigationService
from codeinsight.cli import main
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.web.api import ViewerApi
from codeinsight.web.server import LOOPBACK_HOSTS, TOKEN_HEADER, ViewerServer

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def server(analyzed, tmp_path: Path) -> Iterator[tuple[ViewerServer, Path]]:
    root = tmp_path / "proj"
    shutil.copytree(FIXTURES / "c_callgraph", root)
    _, project, _ = analyzed(root)
    repo = AnalysisRepository(tmp_path / "db0.sqlite", check_same_thread=False)  # サーバーのスレッドから使う（リクエストは、ロックで1件ずつ処理する）
    httpd = ViewerServer("127.0.0.1", 0, ViewerApi(repo, project))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, root
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def _request(httpd: ViewerServer, path: str, method: str = "GET", headers: dict[str, str] | None = None, host: str | None = None):
    port = httpd.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    sent = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    connection.request(method, path, headers=sent)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, dict(response.getheaders()), body


def _api(httpd: ViewerServer, path: str):
    status, _, body = _request(httpd, path, headers={TOKEN_HEADER: httpd.token})
    return status, json.loads(body)


def test_rejects_non_loopback_hosts(analyzed, tmp_path: Path) -> None:
    root = tmp_path / "p"
    shutil.copytree(FIXTURES / "c_callgraph", root)
    repo, project, _ = analyzed(root)
    with pytest.raises(ValueError):
        ViewerServer("0.0.0.0", 0, ViewerApi(repo, project))
    assert "0.0.0.0" not in LOOPBACK_HOSTS
    db = str(tmp_path / "x.db")
    assert main(["analyze", str(root), "--db", db]) == 0
    assert main(["serve", "--host", "0.0.0.0", "--db", db, "--project", str(root)]) != 0  # CLIも、外部への公開を拒否する


def test_requires_token_and_valid_host(server) -> None:
    httpd, _ = server
    assert _request(httpd, "/")[0] == 401  # トークンなし
    assert _request(httpd, "/?token=wrong")[0] == 401
    assert _request(httpd, "/api/project")[0] == 401  # APIは、ヘッダーのトークンが必要
    assert _request(httpd, f"/api/project?token={httpd.token}")[0] == 401  # クエリのトークンは、APIでは受け付けない
    assert _request(httpd, "/api/project", headers={TOKEN_HEADER: "wrong"})[0] == 401
    # DNSリバインディング: 別のHostを名乗るリクエストは、トークンが正しくても拒否する
    assert _request(httpd, f"/?token={httpd.token}", host="evil.example:80")[0] == 403
    assert _request(httpd, "/api/project", headers={TOKEN_HEADER: httpd.token}, host="evil.example")[0] == 403


def test_page_has_csp_and_no_token_in_logs_or_cors(server) -> None:
    httpd, _ = server
    status, headers, body = _request(httpd, f"/?token={httpd.token}")
    assert status == 200 and b"renderGraph" in body
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "connect-src 'self'" in csp and "unsafe-eval" not in csp
    assert "Access-Control-Allow-Origin" not in headers and headers["Cache-Control"] == "no-store"
    assert headers["Referrer-Policy"] == "no-referrer"
    # グラフとソース・切り出しの境界は、ドラッグ・キー操作で動かせる（キーボードで操作でき、ダブルクリックで元に戻る）
    assert b'id="splitter"' in body and b'role="separator"' in body and b"tabindex" in body and b"onpointerdown" in body and b"ArrowUp" in body and b"ondblclick" in body


def test_only_get_is_allowed(server) -> None:
    httpd, _ = server
    for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
        status, headers, _ = _request(httpd, "/api/project", method=method, headers={TOKEN_HEADER: httpd.token})
        assert status == 405 and headers["Allow"] == "GET", method


def test_api_project_symbols_graph_source_and_extract(server) -> None:
    httpd, _ = server
    status, info = _api(httpd, "/api/project")
    assert status == 200 and info["files"] >= 3 and "call" in info["graph_kinds"]
    status, found = _api(httpd, "/api/symbols?q=apply")
    assert status == 200 and any(s["name"] == "apply" for s in found["symbols"])
    status, graph = _api(httpd, "/api/graph?kind=call&root=apply&direction=both&depth=1")
    assert status == 200 and graph["focus"] and {n["label"] for n in graph["nodes"]} >= {"apply", "main"}
    apply_node = next(n for n in graph["nodes"] if n["label"] == "apply")
    status, source = _api(httpd, f"/api/source?path={apply_node['path']}&start={apply_node['line']}&end={apply_node['end_line']}&context=0")
    assert status == 200 and source["freshness"] == "fresh" and source["lines"][0]["n"] == apply_node["line"]
    status, extract = _api(httpd, "/api/extract?root=main&depth=1")
    assert status == 200 and "# 切り出し: main" in extract["markdown"] and extract["data"]["root"]["symbol"] == "main"
    status, flow = _api(httpd, "/api/graph?kind=flow&root=apply")
    assert status == 200 and flow["graph_kind"] == "flow"


def test_api_validates_input_and_does_not_leak_files(server) -> None:
    httpd, root = server
    assert _api(httpd, "/api/graph?kind=nope")[0] == 400
    assert _api(httpd, "/api/graph?kind=call&depth=999")[0] == 400
    assert _api(httpd, "/api/graph?kind=flow")[0] == 400  # 起点が必要
    assert _api(httpd, "/api/graph?kind=call&root=" + "x" * 500)[0] == 400
    assert _api(httpd, "/api/symbols")[0] == 400
    assert _api(httpd, "/api/source?path=main.c&start=abc")[0] == 400
    assert _api(httpd, "/api/nothing")[0] == 404
    assert _api(httpd, "/api/graph?kind=call&root=no_such_symbol")[0] == 404
    # 解析対象でないファイル・ルートの外は読めない
    (root.parent / "secret.txt").write_text("secret", encoding="utf-8")
    for path in ("../secret.txt", "/etc/passwd", "%2e%2e/secret.txt", "..%2fsecret.txt"):
        status, body = _api(httpd, f"/api/source?path={path}")
        assert status == 404 and "secret" not in json.dumps(body), path


def test_server_does_not_modify_the_target(server) -> None:
    httpd, root = server
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    for path in ("/api/project", "/api/graph?kind=deps", "/api/extract?root=main", "/api/refresh"):
        assert _api(httpd, path)[0] == 200
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before


def test_api_marks_stale_files_after_refresh(server) -> None:
    httpd, root = server
    (root / "main.c").write_text("/* 追記 */\n" + (root / "main.c").read_text(encoding="utf-8"), encoding="utf-8")
    assert _api(httpd, "/api/refresh")[1]["stale_files"] == ["main.c"]
    status, source = _api(httpd, "/api/source?path=main.c&start=1&end=3")
    assert status == 200 and source["freshness"] == "stale"  # 古さを、利用者に示す
    graph = _api(httpd, "/api/graph?kind=deps")[1]
    assert graph["meta"]["stale_files"] == ["main.c"] and any("古い可能性" in n for n in graph["notes"])
    assert _api(httpd, "/api/extract?root=main")[1]["data"]["stale_files"] == ["main.c"]


def test_navigation_is_read_only_api_object(analyzed, tmp_path: Path) -> None:
    root = tmp_path / "p"
    shutil.copytree(FIXTURES / "c_callgraph", root)
    repo, project, _ = analyzed(root)
    api = ViewerApi(repo, project)
    assert isinstance(api._navigation, NavigationService)
    assert not any(name.startswith(("save", "delete", "replace")) for name in api._routes)  # 書き込み系のルートは存在しない


@pytest.fixture
def reading_server(analyzed, tmp_path: Path) -> Iterator[tuple[ViewerServer, Path]]:
    root = tmp_path / "proj"
    shutil.copytree(FIXTURES / "c_callgraph", root)
    _, project, _ = analyzed(root)
    out = tmp_path / "out"
    (out / "functions").mkdir(parents=True)
    (out / "logs").mkdir()
    (out / "README.md").write_text("# 資料\n\n* [全体像](overview.txt)\n", encoding="utf-8")
    (out / "overview.txt").write_text("main.c:8-12 に main がある\n", encoding="utf-8")
    (out / "functions" / "main.txt").write_text("main\n", encoding="utf-8")
    (out / "logs" / "secret.txt").write_text("ログ", encoding="utf-8")
    (out / "report.pdf").write_bytes(b"%PDF")
    (tmp_path / "outside.txt").write_text("外", encoding="utf-8")
    (out / "link.txt").symlink_to(tmp_path / "outside.txt")  # ルートの外を指すシンボリックリンク
    repo = AnalysisRepository(tmp_path / "db0.sqlite", check_same_thread=False)
    httpd = ViewerServer("127.0.0.1", 0, ViewerApi(repo, project, out))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, out
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_reading_api_lists_only_safe_materials_and_reads_them(reading_server) -> None:
    httpd, out = reading_server
    status, listing = _api(httpd, "/api/reading")
    names = [f["name"] for f in listing["files"]]
    assert status == 200 and listing["available"] and names[:2] == ["README.md", "overview.txt"]
    assert "functions/main.txt" in names
    assert not any(n.startswith("logs/") or n.endswith((".pdf", "link.txt")) for n in names)  # ログ・PDF・シンボリックリンクは出さない
    status, doc = _api(httpd, "/api/reading/file?name=overview.txt")
    assert status == 200 and doc["kind"] == "text" and "main.c:8-12" in doc["text"]
    assert _api(httpd, "/api/reading/file?name=README.md")[1]["kind"] == "markdown"


def test_reading_file_api_rejects_anything_not_listed(reading_server) -> None:
    httpd, out = reading_server
    for name in ("../outside.txt", "logs/secret.txt", "link.txt", "report.pdf", "/etc/passwd", "%2e%2e/outside.txt", "nothing.txt"):
        status, body = _api(httpd, f"/api/reading/file?name={name}")
        assert status == 404 and "外" not in json.dumps(body, ensure_ascii=False) and "ログ" not in json.dumps(body, ensure_ascii=False), name
    assert _api(httpd, "/api/reading/file")[0] == 400


def test_reading_is_unavailable_without_a_reading_dir_and_guide_images_are_whitelisted(server) -> None:
    httpd, _ = server
    status, listing = _api(httpd, "/api/reading")
    assert status == 200 and listing["available"] is False and listing["files"] == []
    assert _api(httpd, "/api/reading/file?name=README.md")[0] == 404
    for name in ("../../server.py", "..%2f..%2fserver.py", "a/b.png", "x.py", "nothing.png"):
        assert _api(httpd, f"/api/guide/image?name={name}")[0] == 404, name

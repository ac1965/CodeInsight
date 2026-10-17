"""GUI（Webビューアー）の読み取り専用のAPI: 読解カード・欄外の印・未解決の受信箱・フィールド・設定・境界・解析の状態・AIの推測の一覧。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from codeinsight.cli import main
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.web.api import ViewerApi
from codeinsight.web.server import TOKEN_HEADER, ViewerServer
from test_web_server import _api, _request

SOURCE = """\
#include <stdlib.h>
struct Account { int balance; int limit; };
static int add(int a, int b) { return a + b; }
static int sub(int a, int b) { return a - b; }
int apply(int (*f)(int, int), int x) { return f(x, 2); }
int via_two(int c, int x) { int (*op)(int, int) = c ? add : sub; return op(x, 1); }
int pick(int x) { int (*h)(int, int) = *(int (**)(int, int))&x; return h(1, 2); }
void deposit(struct Account *a, int n) { a->balance += n; }
int main(void) {
    struct Account acc = {.balance = 0, .limit = 5};
    const char *home = getenv("HOME");
    deposit(&acc, 1);
    return apply(add, 1) + via_two(1, 2) + pick(3) + (home != 0);
}
"""


@pytest.fixture
def gui(tmp_path: Path) -> Iterator[tuple[ViewerServer, Path, str]]:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.c").write_text(SOURCE, encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    repo = AnalysisRepository(Path(db), check_same_thread=False)
    project = repo.list_projects()[0]
    httpd = ViewerServer("127.0.0.1", 0, ViewerApi(repo, project))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, root, db
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_card_is_the_eight_questions_with_the_resolution_of_each_item(gui) -> None:
    httpd, _, _ = gui
    status, card = _api(httpd, "/api/card?symbol=deposit")
    assert status == 200 and card["symbol"]["qualified_name"] == "deposit" and card["language"] == "c"
    assert card["parameters"][0]["name"] == "a" and "引数 a を通じて" in card["parameter_mutations"][0]
    assert "確定ではありません" in card["note"]  # 推定・未解決を、確定と読ませない
    assert _api(httpd, "/api/card?symbol=nothing")[0] == 404
    assert _api(httpd, "/api/card")[0] == 400


def test_marks_show_unresolved_ambiguous_and_inferred_calls_from_stored_facts(gui) -> None:
    httpd, _, _ = gui
    status, data = _api(httpd, "/api/marks?path=app.c")
    kinds = {(m["function"], m["label"]): m["kind"] for m in data["marks"]}
    assert status == 200 and data["stale"] is False
    assert kinds[("via_two", "op")] == "ambiguous"  # add か sub
    assert kinds[("pick", "h")] == "unresolved"  # 追えない値
    assert kinds[("apply", "f")] == "inferred"  # 呼び出し元が渡す add だけ
    assert _api(httpd, "/api/marks?path=../etc/passwd")[0] == 404  # 解析対象のファイルだけ
    assert _api(httpd, "/api/marks")[0] == 400


def test_unresolved_inbox_groups_by_function_and_filters(gui) -> None:
    httpd, _, _ = gui
    _, data = _api(httpd, "/api/unresolved")
    names = [r["function"] for r in data["functions"]]
    assert {"via_two", "pick"} <= set(names) and data["total_calls"] >= 2
    row = next(r for r in data["functions"] if r["function"] == "via_two")
    assert row["ambiguous"] == 1 and row["inference"] is None and row["calls"][0]["name"] == "op" and "add" in row["calls"][0]["note"]
    assert [r["function"] for r in _api(httpd, "/api/unresolved?status=unresolved")[1]["functions"]] == ["pick"]
    assert [r["function"] for r in _api(httpd, "/api/unresolved?q=via")[1]["functions"]] == ["via_two"]
    assert _api(httpd, "/api/unresolved?status=bogus")[0] == 400


def test_fields_config_and_boundaries(gui) -> None:
    httpd, _, _ = gui
    _, fields = _api(httpd, "/api/fields?name=Account.balance")
    assert {(u["function"], u["mode"]) for u in fields["uses"]} >= {("deposit", "readwrite"), ("main", "init")}
    assert _api(httpd, "/api/fields?name=a;b")[0] == 400  # 識別子だけ
    _, config = _api(httpd, "/api/config")
    assert any(i["kind"] == "env" and i["name"] == "HOME" for i in config["items"]) and "env" in config["kinds"]
    assert _api(httpd, "/api/config?kind=env&q=hom")[1]["total"] == 1
    _, boundaries = _api(httpd, "/api/boundaries")
    assert any(i["kind"] == "entry" and i["owner"] == "main" for i in boundaries["items"])


def test_status_reports_the_latest_analysis_and_groups_failures_with_hints(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "ok.c").write_text("int fine(void) { return 1; }\n", encoding="utf-8")
    (root / "broken.c").write_text('#include "missing_generated.h"\nint x(void) { return 1; }\n', encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0  # 一部のファイルが失敗しても、全体は partial（成功したファイルの結果は使える）
    repo = AnalysisRepository(Path(db), check_same_thread=False)
    api = ViewerApi(repo, repo.list_projects()[0])
    status = api.handle("/api/status", {})
    assert status["latest"]["status"] == "partial" and status["files_by_language"]["c"] == {"analyzed": 1, "failed": 1}
    group = next(g for g in status["error_groups"] if "file not found" in g["cause"])
    assert group["count"] >= 1 and "configure" in group["hint"]  # 失敗を隠さず、原因と対処を示す
    (root / "ok.c").write_text("int fine(void) { return 2; }\n", encoding="utf-8")
    api.refresh()
    assert api.handle("/api/status", {})["stale_files"] == ["ok.c"]  # 解析後に変わったファイル
    assert api.handle("/api/marks", {"path": ["ok.c"]})["stale"] is True


def test_inference_list_and_detail_are_read_only_views_of_saved_explanations(gui, monkeypatch) -> None:
    httpd, _, db = gui
    from codeinsight.ai import provider as provider_module

    class Fake:
        name, model = "fake", "fake-model"

        def __init__(self, *a, **k) -> None:
            pass

        def complete(self, messages, *, temperature=0.1, max_tokens=None):
            return provider_module.Completion("## 処理の詳細\n【推論】`op` は `add` か `sub`。[app.c:6-6]", "fake-model")

    monkeypatch.setattr("codeinsight.ai.service.OpenAICompatibleProvider", Fake)
    assert main(["infer", "via_two", "--db", db, "--ai-model", "m"]) == 0
    listing = _api(httpd, "/api/inferences")[1]["inferences"]
    assert [i["function"] for i in listing] == ["via_two"] and listing[0]["stale"] is False
    detail = _api(httpd, f"/api/inference?id={listing[0]['id']}")[1]
    assert detail["ai_generated"] is True and "確定ではありません" in detail["note"] and "【推論】" in detail["text"] and detail["latest"] is True
    unresolved = _api(httpd, "/api/unresolved")[1]
    assert next(r for r in unresolved["functions"] if r["function"] == "via_two")["inference"]["id"] == listing[0]["id"]  # 受信箱に、保存済みの推測を示す
    assert _api(httpd, "/api/inference?id=nope")[0] == 404


def test_new_endpoints_require_the_token_and_only_accept_get(gui) -> None:
    httpd, _, _ = gui
    for path in ("/api/card?symbol=main", "/api/marks?path=app.c", "/api/unresolved", "/api/status", "/api/config", "/api/boundaries", "/api/inferences"):
        assert _request(httpd, path)[0] == 401
        assert _request(httpd, path, method="POST", headers={TOKEN_HEADER: httpd.token})[0] == 405  # 操作モードでない既定は、GET以外を受け付けない


def test_page_has_the_new_views_and_never_uses_innerhtml(gui) -> None:
    httpd, _, _ = gui
    status, _, body = _request(httpd, f"/?token={httpd.token}")
    page = body.decode("utf-8")
    assert status == 200
    for needle in ("view-unresolved", "view-explore", "view-status", "tab-card", "/api/marks", "/api/card", "/api/unresolved", "/api/status"):
        assert needle in page
    assert "innerHTML" not in page and "insertAdjacentHTML" not in page  # 解析対象由来の文字列は、DOM（textContent）だけで反映する
    assert "eval(" not in page and "document.write" not in page

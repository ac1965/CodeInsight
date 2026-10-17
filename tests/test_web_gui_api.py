"""GUI（Webビューアー）の読み取り専用のAPI: 読解カード・欄外の印・未解決の受信箱・フィールド・設定・境界・解析の状態・AIの推測の一覧。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC
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


def _by_name(index):
    return {s.qualified_name: s for s in index.symbols.values()}


def test_graph_overlays_are_off_by_default_and_kept_apart_from_static_edges(gui, monkeypatch) -> None:
    httpd, _, db = gui
    from datetime import datetime

    from codeinsight.ai import provider as provider_module
    from codeinsight.domain.observation import DynamicRun, Observation, ObservationKind, RunStatus

    repo = AnalysisRepository(Path(db))
    project = repo.list_projects()[0]
    from codeinsight.application import NavigationService

    index = NavigationService(repo).load_index(project)
    ids = {n: s.symbol_id for n, s in _by_name(index).items()}
    hashes = {f.relative_path: f.content_hash for f in index.files.values()}
    run = DynamicRun("r1", project.project_id, datetime.now(UTC), ("x",), {}, {}, "c-gcov+c-instrument", "1", RunStatus.COMPLETED, source_hashes=hashes)
    observations = [
        Observation("o1", "r1", ObservationKind.CALL, symbol_id=ids["main"], target_symbol_id=ids["apply"], count=3),
        Observation("o2", "r1", ObservationKind.CALL, symbol_id=ids["main"], target_symbol_id=ids["add"], count=2),  # 静的な確定済みの辺には無い
    ]
    repo.save_dynamic_run(run, observations)
    repo.close()

    plain = _api(httpd, "/api/graph?kind=call")[1]
    assert "overlay" not in plain and not any(e.get("observed") for e in plain["edges"])  # 既定は重ねない
    data = _api(httpd, "/api/graph?kind=call&overlay=observed")[1]
    edges = {(e["source"], e["target"]): e for e in data["edges"]}
    assert edges[(ids["main"], ids["apply"])]["observed"] == 3 and edges[(ids["main"], ids["apply"])]["style"] != "observed_only"
    only = edges[(ids["main"], ids["add"])]
    assert only["style"] == "observed_only" and "静的な確定済みの辺には無い" in only["note"]
    assert data["overlay"]["observed"] is True and "呼ばれないことを意味しません" in " ".join(data["overlay"]["notes"])
    assert _api(httpd, "/api/graph?kind=call")[1]["edges"] == plain["edges"]  # 重ねは、保存された解析結果を変えない

    class Fake:
        name, model = "fake", "fake-model"

        def __init__(self, *a, **k) -> None:
            pass

        def complete(self, messages, *, temperature=0.1, max_tokens=None):
            return provider_module.Completion("## 処理の詳細\n【推論】`op` は `add` か `sub`。[app.c:6-6]", "fake-model")

    monkeypatch.setattr("codeinsight.ai.service.OpenAICompatibleProvider", Fake)
    assert main(["infer", "via_two", "--db", db, "--ai-model", "m"]) == 0
    ai = _api(httpd, "/api/graph?kind=call&overlay=ai")[1]
    ai_edges = [e for e in ai["edges"] if e["style"] == "ai"]
    assert {(e["source"], e["target"]) for e in ai_edges} == {(ids["via_two"], ids["add"])}  # sub はグラフに無い（辺の両端がグラフにある場合だけ重ねる）
    assert all("確定ではありません" in e["note"] for e in ai_edges)
    assert not any(e["style"] == "ai" for e in _api(httpd, "/api/graph?kind=call")[1]["edges"])


def test_stale_observation_is_not_overlaid(gui) -> None:
    httpd, root, db = gui
    from datetime import datetime

    from codeinsight.application import NavigationService
    from codeinsight.domain.observation import DynamicRun, Observation, ObservationKind, RunStatus

    repo = AnalysisRepository(Path(db))
    project = repo.list_projects()[0]
    index = NavigationService(repo).load_index(project)
    ids = {n: s.symbol_id for n, s in _by_name(index).items()}
    run = DynamicRun("r2", project.project_id, datetime.now(UTC), ("x",), {}, {}, "c-gcov", "1", RunStatus.COMPLETED, source_hashes={"app.c": "0" * 64})  # 実行後にソースが変わった
    repo.save_dynamic_run(run, [Observation("o9", "r2", ObservationKind.CALL, symbol_id=ids["main"], target_symbol_id=ids["add"], count=1)])
    repo.close()
    data = _api(httpd, "/api/graph?kind=call&overlay=observed")[1]
    assert not any(e["style"] == "observed_only" for e in data["edges"]) and "重ねられる観測がありません" in " ".join(data["overlay"]["notes"])


def test_external_tool_results_are_shown_apart_and_compared_without_adopting(gui, tmp_path: Path) -> None:
    import json

    httpd, _, db = gui
    assert _api(httpd, "/api/external")[1]["sarif"]["total"] == 0 and _api(httpd, "/api/external")[1]["binary"]["comparison"] is None  # 取り込みが無い
    sarif = tmp_path / "t.sarif"
    sarif.write_text(json.dumps({"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "demo", "version": "1"}}, "results": [
        {"ruleId": "D1", "level": "warning", "message": {"text": "x"}, "locations": [{"physicalLocation": {"artifactLocation": {"uri": "app.c"}, "region": {"startLine": 8}}}]}]}]}), encoding="utf-8")
    funcs = tmp_path / "f.json"
    funcs.write_text(json.dumps([{"name": "dbg.main", "addr": 4096, "size": 16, "file": "app.c"}, {"name": "dbg.pick", "addr": 4200, "size": 16, "file": "app.c"}, {"name": "dbg.ghost", "addr": 4300, "size": 16, "file": "app.c"}]), encoding="utf-8")
    graph = tmp_path / "g.json"
    graph.write_text(json.dumps([{"name": "dbg.main", "size": 10, "imports": ["dbg.pick", "dbg.ghost"]}]), encoding="utf-8")
    assert main(["import-sarif", str(sarif), "--db", db]) == 0
    assert main(["import-r2", str(funcs), "--callgraph", str(graph), "--db", db]) == 0
    data = _api(httpd, "/api/external")[1]
    assert data["sarif"]["total"] == 1 and data["sarif"]["findings"][0]["rule"] == "D1" and data["sarif"]["findings"][0]["stale"] is False
    assert "事実として扱いません" in data["note"]
    comparison = data["binary"]["comparison"]
    assert "ghost" in " ".join(comparison["binary_only"]) and comparison["matched"]  # ソースに無い関数は、バイナリのみ。採用しない
    assert data["scip"]["comparison"] is None


def test_dynamic_runs_and_diff_are_read_only_views_of_saved_observations(gui) -> None:
    httpd, _, db = gui
    from datetime import datetime

    from codeinsight.application import NavigationService
    from codeinsight.domain.observation import DynamicRun, Observation, ObservationKind, RunStatus

    assert _api(httpd, "/api/dynamic")[1]["runs"] == [] and _api(httpd, "/api/dynamic-diff")[0] == 404
    repo = AnalysisRepository(Path(db))
    project = repo.list_projects()[0]
    index = NavigationService(repo).load_index(project)
    ids = {n: s.symbol_id for n, s in _by_name(index).items()}
    hashes = {f.relative_path: f.content_hash for f in index.files.values()}
    run = DynamicRun("r3abcdef", project.project_id, datetime.now(UTC), ("./a.out",), {}, {}, "c-gcov+c-instrument", "1", RunStatus.COMPLETED, source_hashes=hashes)
    repo.save_dynamic_run(run, [Observation("o1", "r3abcdef", ObservationKind.CALL, symbol_id=ids["main"], target_symbol_id=ids["apply"], count=2, path="app.c", name="main"),
                                Observation("o2", "r3abcdef", ObservationKind.CALL, symbol_id=ids["main"], target_symbol_id=ids["add"], count=1, path="app.c", name="main")])
    repo.close()
    runs = _api(httpd, "/api/dynamic")[1]["runs"]
    assert runs[0]["id"] == "r3abcdef" and runs[0]["stale"] is False and "呼ばれないことを意味しません" in _api(httpd, "/api/dynamic")[1]["note"]
    status, diff = _api(httpd, "/api/dynamic-diff?run=r3ab")
    assert status == 200 and diff["counts"]["both"] >= 1 and diff["counts"]["observed_only"] >= 1 and "呼ばれないことを意味しません" in diff["note"]
    assert _api(httpd, "/api/dynamic-diff?run=zzz")[0] == 404


def test_new_endpoints_require_the_token_and_only_accept_get(gui) -> None:
    httpd, _, _ = gui
    for path in ("/api/card?symbol=main", "/api/marks?path=app.c", "/api/unresolved", "/api/status", "/api/config", "/api/boundaries", "/api/inferences", "/api/external", "/api/dynamic"):
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

"""操作モード（serve --allow-actions）: 許可の検査（トークン・Host・Origin・Content-Type）、決められた操作だけ、同時実行1つ、計画の確認、履歴。"""

from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from codeinsight.ai import provider as provider_module
from codeinsight.cli import main
from codeinsight.dynamic import executor
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.web.actions import ActionRunner
from codeinsight.web.api import ViewerApi
from codeinsight.web.server import TOKEN_HEADER, ViewerServer
from test_web_gui_api import SOURCE

SCRIPT = "print('hi')\n"


class FakeProvider:
    name, model = "fake", "fake-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages, *, temperature=0.1, max_tokens=None):
        self.calls += 1
        return provider_module.Completion("## 処理の詳細\n【推論】`op` は `add` か `sub`。[app.c:6-6]", "fake-model")


@pytest.fixture
def actions(tmp_path: Path) -> Iterator[tuple[ViewerServer, ActionRunner, Path, str]]:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "app.c").write_text(SOURCE, encoding="utf-8")
    (root / "main.py").write_text(SCRIPT, encoding="utf-8")
    db = tmp_path / "d.sqlite"
    assert main(["analyze", str(root), "--db", str(db)]) == 0
    repo = AnalysisRepository(db, check_same_thread=False)
    project = repo.list_projects()[0]
    api = ViewerApi(repo, project)
    runner = ActionRunner(db, project.project_id, api.refresh)
    runner.ai_provider = FakeProvider()
    runner.run_executor = lambda run: executor.ExecutionResult(False, 0, 0.1, None, "", "", ["fake"])
    httpd = ViewerServer("127.0.0.1", 0, api, actions=runner)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, runner, root, str(db)
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def post(httpd: ViewerServer, path: str, body: object = None, *, headers: dict[str, str] | None = None, raw: bytes | None = None, origin: bool = True, token: bool = True, host: str | None = None):
    port = httpd.server_address[1]
    data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
    sent = {"Host": host or f"127.0.0.1:{port}", "Content-Type": "application/json", "Content-Length": str(len(data))}
    if origin:
        sent["Origin"] = f"http://127.0.0.1:{port}"
    if token:
        sent[TOKEN_HEADER] = httpd.token
    sent.update(headers or {})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("POST", path, body=data, headers=sent)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, json.loads(payload) if payload else {}


def get(httpd: ViewerServer, path: str):
    port = httpd.server_address[1]
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}", TOKEN_HEADER: httpd.token})
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, json.loads(payload)


def wait_done(httpd: ViewerServer, timeout: float = 30.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        status = get(httpd, "/api/actions/status")[1]
        if not status["busy"]:
            return status
        time.sleep(0.05)
    raise AssertionError("操作が終わりません")


def test_post_is_rejected_unless_actions_are_enabled(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    db = tmp_path / "d.sqlite"
    assert main(["analyze", str(root), "--db", str(db)]) == 0
    repo = AnalysisRepository(db, check_same_thread=False)
    httpd = ViewerServer("127.0.0.1", 0, ViewerApi(repo, repo.list_projects()[0]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        assert post(httpd, "/api/actions/analyze")[0] == 405  # 既定は読み取り専用（これまでと同じ）
        assert get(httpd, "/api/actions/status")[1]["enabled"] is False
        assert get(httpd, "/api/project")[1]["actions_enabled"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_every_check_is_required_for_a_post(actions) -> None:
    httpd, _, _, _ = actions
    assert post(httpd, "/api/actions/analyze", token=False)[0] == 401  # トークン（専用のヘッダー）
    assert post(httpd, "/api/actions/analyze", headers={TOKEN_HEADER: "wrong-token-value-0000"})[0] == 401
    assert post(httpd, "/api/actions/analyze", origin=False)[0] == 403  # Origin なし
    assert post(httpd, "/api/actions/analyze", headers={"Origin": "http://evil.example"})[0] == 403  # 他のオリジン（CSRF）
    assert post(httpd, "/api/actions/analyze", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert post(httpd, "/api/actions/analyze", host="evil.example")[0] == 403  # Host（DNSリバインディング）
    assert post(httpd, "/api/actions/analyze", headers={"Content-Type": "text/plain"})[0] == 415
    assert post(httpd, "/api/actions/analyze", raw=b"[1]")[0] == 400
    assert post(httpd, "/api/actions/analyze", raw=b"x" * 70000)[0] == 413
    assert post(httpd, "/api/nothing")[0] == 404 and post(httpd, "/api/actions/nothing")[0] == 404
    assert post(httpd, "/api/actions/analyze?token=x")[0] == 404  # クエリでの指定は受け付けない
    assert get(httpd, "/api/actions/status")[1]["history"] == []  # 検査に通らなかったものは、何も実行していない


def test_only_listed_parameters_are_accepted_and_unsafe_ones_do_not_exist(actions) -> None:
    httpd, _, _, _ = actions
    for body in ({"command": ["a.py"], "allow_network": True}, {"command": ["a.py"], "allow_unsandboxed": True}, {"command": ["a.py"], "sandbox": "none"}, {"command": ["a.py"], "env": ["A=1"]},
                 {"command": ["a.py"], "env": {"A": "1"}}, {"command": []}, {"command": "make test"}, {"command": ["a.py"], "timeout": 99999}, {"command": ["a.py"], "image": "x; rm -rf /"}):
        assert post(httpd, "/api/actions/dynamic-plan", body)[0] == 400, body  # ネットワーク・隔離なし・値つきの環境変数・シェルの文字列は、指定できない
    assert post(httpd, "/api/actions/analyze", {"path": "/etc"})[0] == 400  # 対象の指定も、受け付けない
    assert post(httpd, "/api/actions/infer", {})[0] == 400 and post(httpd, "/api/actions/infer", {"symbol": "a", "all": True})[0] == 400
    assert post(httpd, "/api/actions/infer", {"all": True, "limit": 0})[0] == 400
    assert get(httpd, "/api/actions/status")[1]["history"] == []


def test_reanalysis_runs_in_the_background_and_refreshes_the_viewer(actions) -> None:
    httpd, _, root, _ = actions
    (root / "extra.c").write_text("int extra(void) { return 1; }\n", encoding="utf-8")
    assert "extra.c" not in " ".join(get(httpd, "/api/project")[1]["stale_files"]) and get(httpd, "/api/symbols?q=extra")[1]["total"] == 0
    status, started = post(httpd, "/api/actions/analyze")
    assert status == 200 and started["operation"]["kind"] == "analyze"
    done = wait_done(httpd)
    last = done["history"][0]
    assert last["state"] == "done" and last["result"]["status"] in ("success", "partial") and "変更していません" in last["result"]["note"]
    assert get(httpd, "/api/symbols?q=extra")[1]["total"] == 1  # 完了後、ビューアーが新しい結果を読む
    assert (root / "extra.c").read_text(encoding="utf-8") == "int extra(void) { return 1; }\n"  # 対象は変更しない


def test_only_one_operation_runs_at_a_time(actions) -> None:
    httpd, runner, _, _ = actions
    gate = threading.Event()
    original = runner._analyze

    def slow(params, entry):
        gate.wait(10)
        return original(params, entry)

    runner._analyze = slow  # type: ignore[method-assign]
    assert post(httpd, "/api/actions/analyze")[0] == 200
    assert post(httpd, "/api/actions/analyze")[0] == 409  # 同時に実行できるのは1つ
    assert post(httpd, "/api/actions/infer", {"all": True})[0] == 409
    assert get(httpd, "/api/actions/status")[1]["busy"] is True
    gate.set()
    wait_done(httpd)
    assert post(httpd, "/api/actions/analyze")[0] == 200  # 終わったら、次を受け付ける
    wait_done(httpd)


def test_infer_runs_without_consent_shows_the_destination_and_stores_a_separate_unverified_by_default_result(actions) -> None:
    httpd, runner, _, db = actions
    assert runner.ai_destination()["base_url"].startswith("http://localhost") and runner.ai_destination()["local"] is True
    assert post(httpd, "/api/actions/infer", {"symbol": "via_two"})[0] == 200  # 送信の許可（--allow-send）は要らない
    done = wait_done(httpd)
    result = done["history"][0]["result"]
    assert done["history"][0]["state"] == "done" and result["destination"]["local"] is True and result["items"][0]["status"] in ("verified", "partial", "unverified")
    assert "事実として扱わないでください" in result["note"]
    repo = AnalysisRepository(Path(db))
    explanations = repo.list_explanations(repo.list_projects()[0].project_id, "inference")
    assert len(explanations) == 1 and explanations[0].target == "via_two"  # 解析結果とは別に保存される
    repo.close()
    assert get(httpd, "/api/inferences")[1]["inferences"][0]["function"] == "via_two"
    assert post(httpd, "/api/actions/infer", {"symbol": "nothing"})[0] == 200
    assert wait_done(httpd)["history"][0]["state"] == "failed"
    assert post(httpd, "/api/actions/infer", {"symbol": "via_two", "dry_run": True})[0] == 200
    dry = wait_done(httpd)["history"][0]["result"]["items"][0]
    assert dry["dry_run"] is True and dry["prompt"] and runner.ai_provider.calls == 1  # dry-run は、送らない


def test_batch_infer_can_be_cancelled_between_items(actions) -> None:
    httpd, runner, _, _ = actions
    gate = threading.Event()
    complete = runner.ai_provider.complete

    def slow(messages, **kw):
        gate.wait(10)
        return complete(messages, **kw)

    runner.ai_provider.complete = slow
    assert post(httpd, "/api/actions/infer", {"all": True, "limit": 5})[0] == 200
    assert post(httpd, "/api/actions/cancel")[0] == 200
    gate.set()
    last = wait_done(httpd)["history"][0]
    assert last["state"] == "cancelled"
    assert post(httpd, "/api/actions/cancel")[0] == 409  # 実行中でなければ、取り消せない


def test_dynamic_run_requires_the_confirmed_plan_and_never_allows_network_or_unsandboxed(actions) -> None:
    httpd, runner, root, db = actions
    body = {"command": ["main.py"], "collector": "python", "timeout": 30}
    assert post(httpd, "/api/actions/dynamic-run", body)[0] == 400  # 計画を確認していない
    status, planned = post(httpd, "/api/actions/dynamic-plan", body)
    plan = planned["operation"]["result"]
    assert status == 200 and plan["executed"] is False and plan["confirmation"]["command"] == ["main.py"] and "遮断" in plan["confirmation"]["network"] and "不可" in plan["confirmation"]["unsandboxed"]
    assert post(httpd, "/api/actions/dynamic-run", {**body, "confirm_digest": "0" * 24})[0] == 400
    assert post(httpd, "/api/actions/dynamic-run", {**body, "confirm_digest": plan["plan_digest"], "timeout": 31})[0] == 400  # 確認した内容と違う実行は、できない
    assert post(httpd, "/api/actions/dynamic-run", {**body, "command": ["other.py"], "confirm_digest": plan["plan_digest"]})[0] == 400
    assert get(httpd, "/api/actions/status")[1]["history"][0]["kind"] == "dynamic-plan"  # 実行はされていない
    assert post(httpd, "/api/actions/dynamic-run", {**body, "confirm_digest": plan["plan_digest"]})[0] == 200
    last = wait_done(httpd)["history"][0]
    assert last["kind"] == "dynamic-run" and last["state"] == "done" and last["result"]["run_id"]
    assert "confirm_digest" not in last["params"]
    repo = AnalysisRepository(Path(db))
    run = repo.list_dynamic_runs(repo.list_projects()[0].project_id)[0]
    assert run.permission["allow_network"] is False and run.permission["allow_unsandboxed"] is False and run.permission["sandbox_backend"] == "container"  # 許可の内容が、実行の記録として残る
    assert run.sandbox["network"] == "none" and run.sandbox["target_mount"] == "read-only"
    repo.close()
    assert (root / "main.py").read_text(encoding="utf-8") == SCRIPT


def test_history_keeps_parameters_without_secrets_and_reports_internal_errors_generically(actions) -> None:
    httpd, runner, _, _ = actions

    def boom(params, entry):
        raise RuntimeError("/Users/secret/path failed")

    runner._analyze = boom  # type: ignore[method-assign]
    assert post(httpd, "/api/actions/analyze")[0] == 200
    last = wait_done(httpd)["history"][0]
    assert last["state"] == "failed" and "secret" not in json.dumps(last) and last["error"] == "内部エラーが発生しました"
    assert post(httpd, "/api/actions/dynamic-plan", {"command": ["a.py"], "env": ["HOME"]})[0] == 200
    plan_entry = get(httpd, "/api/actions/status")[1]["history"][0]
    assert plan_entry["params"]["env"] == ["HOME"] and "TOKEN" not in json.dumps(plan_entry)  # 環境変数は、名前だけ
    assert httpd.token not in json.dumps(get(httpd, "/api/actions/status")[1])  # トークンを、応答に出さない

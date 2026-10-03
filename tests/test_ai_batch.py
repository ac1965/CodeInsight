from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from codeinsight.ai.config import AIConfig, ConsentError
from codeinsight.ai.provider import AIProviderError, Completion
from codeinsight.ai.service import ExplanationService
from codeinsight.cli import main
from codeinsight.cli.ai_commands import _file_base
from test_ai import GOOD_ANSWER, FakeProvider, _setup, _Stub, _symbol  # noqa: F401  （テスト用のAI・HTTPスタブ）

CODE = "".join(f"def func{n}(x):\n    return x + {n}\n\n\n" for n in range(6))


@pytest.fixture
def batch_env(analyzed, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(CODE, encoding="utf-8")
    repo, project, nav, index = _setup(analyzed, root)
    symbols = [_symbol(nav, index, f"mod.func{n}") for n in range(6)]

    def service(provider, **config) -> ExplanationService:
        return ExplanationService(repo, nav, AIConfig(allow_send=True, model="fake-model", **config), provider)

    return repo, project, nav, index, symbols, service, root


class Recording(FakeProvider):
    """呼び出しの回数と、同時に実行された数を記録するAI。"""

    def __init__(self, text: str = GOOD_ANSWER, delay: float = 0.0) -> None:
        super().__init__(text)
        self.delay, self.lock, self.active, self.peak, self.count = delay, threading.Lock(), 0, 0, 0

    def complete(self, messages, *, temperature=0.1, max_tokens=None) -> Completion:
        with self.lock:
            self.active += 1
            self.count += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return Completion(self.text, self.model)
        finally:
            with self.lock:
                self.active -= 1


# --- エラーの分類 ---


def test_provider_errors_are_classified_for_retry_and_abort() -> None:
    assert AIProviderError("x", kind="http", status=429).retryable and AIProviderError("x", kind="http", status=503).retryable
    assert AIProviderError("x", kind="timeout").retryable and AIProviderError("x", kind="format").retryable
    for status in (400, 401, 403, 404):
        error = AIProviderError("x", kind="http", status=status)
        assert error.fatal and not error.retryable  # 認証・権限・モデル/URLの誤りは、再試行しても回復しない
    assert AIProviderError("x", kind="connection").fatal  # 接続できなければ、残りも失敗する
    assert not AIProviderError("plain").retryable and not AIProviderError("plain").fatal  # 種類が不明なものは、再試行も打ち切りもしない


# --- 並列 ---


def test_queries_run_in_parallel_but_results_keep_the_input_order(batch_env) -> None:
    repo, project, nav, index, symbols, service, _ = batch_env
    barrier = threading.Barrier(3, timeout=10)  # 3件が同時に問い合わせ中にならないと、通過できない

    class Gated(Recording):
        def complete(self, messages, *, temperature=0.1, max_tokens=None):
            barrier.wait()
            return super().complete(messages, temperature=temperature, max_tokens=max_tokens)

    provider = Gated()
    items = service(provider).explain_symbols(project, index, symbols[:3], workers=3, reuse=False)
    assert [i.symbol.name for i in items] == ["func0", "func1", "func2"] and all(i.status == "created" for i in items)
    # バリア（3件が同時に到達しないと通過できず、タイムアウトで失敗になる）を全件が通過した = 実際に同時に実行された
    assert len({i.result.explanation.explanation_id for i in items}) == 3  # それぞれ保存された
    assert all(i.result.explanation.target == f"mod.func{n}" for n, i in enumerate(items))


def test_parallel_is_faster_than_serial_and_workers_must_be_positive(batch_env) -> None:
    repo, project, nav, index, symbols, service, _ = batch_env
    started = time.monotonic()
    service(Recording(delay=0.3)).explain_symbols(project, index, symbols, workers=6, reuse=False)
    assert time.monotonic() - started < 1.2  # 直列なら 1.8 秒以上
    with pytest.raises(ValueError):
        service(Recording()).explain_symbols(project, index, symbols, workers=0)


# --- 再利用（再開） ---


def test_saved_explanations_are_reused_without_contacting_the_ai(batch_env) -> None:
    repo, project, nav, index, symbols, service, root = batch_env
    first = Recording()
    service(first).explain_symbols(project, index, symbols[:3], workers=2)
    assert first.count == 3

    second = Recording()
    items = service(second).explain_symbols(project, index, symbols[:3], workers=2)
    assert [i.status for i in items] == ["reused"] * 3 and second.count == 0  # AIへ再送信しない（重複した問い合わせを避ける）
    assert all(i.result.report is not None for i in items)  # 再利用でも、検証は再実行する
    assert len(repo.list_explanations(project.project_id)) == 3  # 重複して保存しない

    third = Recording()
    forced = service(third).explain_symbols(project, index, symbols[:3], workers=2, reuse=False)
    assert [i.status for i in forced] == ["created"] * 3 and third.count == 3  # --no-reuse では、新しく生成する


def test_a_changed_source_or_model_is_not_reused(analyzed, tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "mod.py").write_text(CODE, encoding="utf-8")
    repo, project, nav, index = _setup(analyzed, root)
    first = Recording()
    ExplanationService(repo, nav, AIConfig(allow_send=True, model="m1"), first).explain_symbols(project, index, [_symbol(nav, index, "mod.func0")])
    other_model = Recording()
    items = ExplanationService(repo, nav, AIConfig(allow_send=True, model="m2"), other_model).explain_symbols(project, index, [_symbol(nav, index, "mod.func0")])
    assert items[0].status == "created" and other_model.count == 1  # モデルが違えば、再利用しない

    (root / "mod.py").write_text(CODE.replace("x + 0", "x + 100"), encoding="utf-8")  # ソースを変更して、再解析する
    repo2, project2, nav2, index2 = _setup(analyzed, root)
    changed = Recording()
    items = ExplanationService(repo2, nav2, AIConfig(allow_send=True, model="m1"), changed).explain_symbols(project2, index2, [_symbol(nav2, index2, "mod.func0")])
    assert items[0].status == "created" and changed.count == 1  # 根拠が変わっていれば、古い解説を再利用しない


# --- 再試行と打ち切り ---


class Flaky(Recording):
    def __init__(self, errors: list[AIProviderError]) -> None:
        super().__init__()
        self.errors = errors

    def complete(self, messages, *, temperature=0.1, max_tokens=None):
        with self.lock:
            error = self.errors.pop(0) if self.errors else None
        if error:
            raise error
        return super().complete(messages, temperature=temperature, max_tokens=max_tokens)


def test_transient_errors_are_retried_with_exponential_backoff(batch_env) -> None:
    repo, project, nav, index, symbols, service, _ = batch_env
    delays: list[float] = []
    provider = Flaky([AIProviderError("busy", kind="http", status=429), AIProviderError("down", kind="http", status=503)])
    items = service(provider).explain_symbols(project, index, symbols[:1], workers=1, backoff=2.0, sleep=delays.append)
    assert items[0].status == "created" and items[0].attempts == 3 and delays == [2.0, 4.0]  # 指数バックオフ


def test_retries_are_bounded_and_a_failure_does_not_stop_the_other_items(batch_env) -> None:
    repo, project, nav, index, symbols, service, _ = batch_env
    always = [AIProviderError("busy", kind="http", status=503)] * 3
    provider = Flaky(list(always))
    items = service(provider).explain_symbols(project, index, symbols[:2], workers=1, retries=3, sleep=lambda _s: None)
    assert [i.status for i in items] == ["failed", "created"]  # 1件目は再試行の上限で失敗。2件目は処理される
    assert items[0].attempts == 3 and "busy" in items[0].error


def test_fatal_error_aborts_the_rest_and_reports_them_as_unprocessed(batch_env) -> None:
    repo, project, nav, index, symbols, service, _ = batch_env
    provider = Flaky([AIProviderError("認証に失敗", kind="http", status=401)])
    items = service(provider).explain_symbols(project, index, symbols, workers=1, sleep=lambda _s: None)
    assert [i.status for i in items] == ["failed"] + ["skipped"] * 5  # 回復しない失敗の後は、残りを打ち切る
    assert "401" not in items[0].error or "認証" in items[0].error
    assert provider.count == 0 and not repo.list_explanations(project.project_id)  # 何も保存されない（失敗を成功として残さない）


def test_unexpected_exceptions_become_failures_without_breaking_the_batch(batch_env) -> None:
    repo, project, nav, index, symbols, service, _ = batch_env

    class Boom(Recording):
        def complete(self, messages, *, temperature=0.1, max_tokens=None):
            with self.lock:
                self.count += 1
                first = self.count == 1
            if first:
                raise RuntimeError("想定外")
            return Completion(self.text, self.model)

    items = service(Boom()).explain_symbols(project, index, symbols[:3], workers=1, reuse=False)
    assert [i.status for i in items] == ["failed", "created", "created"] and "想定外" in items[0].error


def test_nothing_is_sent_without_consent(batch_env) -> None:
    repo, project, nav, index, symbols, _, _ = batch_env
    provider = Recording()
    denied = ExplanationService(repo, nav, AIConfig(model="m"), provider)  # allow_send なし
    with pytest.raises(ConsentError):
        denied.explain_symbols(project, index, symbols)
    assert provider.count == 0  # 許可が無ければ、1件も送信しない


# --- CLI ---


def test_file_base_names() -> None:
    assert _file_base("a.b.c@12") == "a.b.c_L12" and _file_base("main") == "main" and _file_base("x y/z") == "x_y_z"


def test_cli_explain_many_writes_files_and_resumes_by_reusing(batch_env, tmp_path: Path, capsys) -> None:
    repo, project, nav, index, symbols, service, root = batch_env
    db = str(tmp_path / "cli.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    capsys.readouterr()
    names = tmp_path / "names.tsv"
    names.write_text("mod.func0\tmod.py\nmod.func1\tmod.py\nmod.missing\tmod.py\n", encoding="utf-8")
    out = tmp_path / "ai"
    stub = _Stub()
    try:
        stub.body = {"model": "stub-model", "choices": [{"message": {"content": GOOD_ANSWER}}]}
        argv = ["explain-many", "--from-file", str(names), "--out-dir", str(out), "--workers", "2", "--allow-send",
                "--ai-base-url", stub.url, "--ai-model", "stub-model", "--db", db]
        code = main(argv)
        captured = capsys.readouterr()
        assert code == 1  # 1件は特定できず失敗
        assert "2 件生成、0 件再利用、1 件失敗" in captured.out and "mod.missing" in captured.err
        assert sorted(p.name for p in out.glob("*.md")) == ["mod.func0.md", "mod.func1.md"]
        text = (out / "mod.func0.md").read_text(encoding="utf-8")
        assert "AI解説（解析結果ではありません）" in text and "検証結果" in text and len(stub.posts()) == 2

        names.write_text("mod.func0\tmod.py\nmod.func1\tmod.py\n", encoding="utf-8")  # 再実行（中断からの再開）
        assert main(argv) == 0
        captured = capsys.readouterr()
        assert "0 件生成、2 件再利用" in captured.out and len(stub.posts()) == 2  # AIへは再送信しない
        assert "再利用しました" in (out / "mod.func0.md").read_text(encoding="utf-8")
    finally:
        stub.close()


def test_cli_explain_many_refuses_without_permission_and_stops_on_fatal_errors(batch_env, tmp_path: Path, capsys) -> None:
    repo, project, nav, index, symbols, service, root = batch_env
    db = str(tmp_path / "cli.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    capsys.readouterr()
    assert main(["explain-many", "mod.func0", "--ai-model", "m", "--db", db]) == 3  # 許可なし
    assert "許可" in capsys.readouterr().err

    stub = _Stub()
    try:
        stub.status = 401
        stub.body = {"error": "unauthorized"}
        code = main(["explain-many", "mod.func0", "mod.func1", "mod.func2", "--workers", "1", "--allow-send", "--ai-base-url", stub.url,
                     "--ai-model", "m", "--no-save", "--db", db])
        captured = capsys.readouterr()
        assert code == 1 and "未処理" in captured.out and "打ち切りました" in captured.err  # 回復しない失敗で打ち切り、再実行を促す
        assert len(stub.posts()) == 1  # 認証エラーの後は、問い合わせを続けない
    finally:
        stub.close()

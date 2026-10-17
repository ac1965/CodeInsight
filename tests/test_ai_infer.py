"""AIの推測（infer）: 未解決・曖昧な呼び出しの呼び出し先を、AIに推測させる。送信の許可なしで実行できる（利用者の指示による原則の変更）。
解析結果は変えず、【推論】つきの解説として別に保存し、引用を検証する。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeinsight.ai.config import AIConfig, ConsentError
from codeinsight.ai.context import ContextError
from codeinsight.ai.service import ExplanationService
from codeinsight.application import NavigationService
from codeinsight.cli import main
from codeinsight.domain import ExplanationStatus
from test_ai import FakeProvider

SOURCE = """\
static int mul(int a, int b) { return a * b; }
static int add(int a, int b) { return a + b; }
int apply(int (*f)(int, int), int x) { return f(x, 2); }
int plain(int x) { return mul(x, 2); }
int main(void) { return apply(mul, 1) + apply(add, 2) + plain(3); }
"""
ANSWER = (
    "## 説明対象\n`apply` の未解決の呼び出し。[lp.c:3-3]\n\n## 処理の概要\n関数ポインタ `f` を呼ぶ。[lp.c:3-3]\n\n"
    "## 処理の詳細\n【推論】`f` は `mul` または `add` と考えられる（呼び出し元が渡している）。[lp.c:5-5]\n\n"
    "## 関連する関数・モジュール\n`mul` [lp.c:1-1]、`add` [lp.c:2-2]\n\n## 根拠\n[lp.c:3-3]\n\n## 解析上の制約\n未確認: 実行時の値。\n\n## 推論を含む点\n【推論】上記の候補。"
)


@pytest.fixture
def setup(analyzed, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lp.c").write_text(SOURCE, encoding="utf-8")
    repo, project, _ = analyzed(root)
    nav = NavigationService(repo)
    index = nav.load_index(project)
    symbols = {s.qualified_name: s for s in index.symbols.values()}
    return repo, project, nav, index, symbols


def test_inference_context_lists_pending_calls_and_candidate_sources(setup) -> None:
    repo, project, nav, index, symbols = setup
    service = ExplanationService(repo, nav, AIConfig(model="m"), FakeProvider(ANSWER))
    result = service.infer_targets(project, index, symbols["apply"], dry_run=True)
    user = result.messages[1].content
    assert result.context.target_kind == "inference" and "未解決・曖昧な呼び出し（推測の対象）" in user
    assert "曖昧（候補が複数）" in user and "候補 mul" in user and "候補 add" in user  # 候補の関数のソースも渡す
    assert "【推論】" in result.messages[0].content and "解決済みの呼び出しは、推測の対象にしません" in user


def test_inference_runs_without_the_send_permission_and_saves_a_separate_inference(setup) -> None:
    repo, project, nav, index, symbols = setup
    before = (len(repo.list_symbols_for_project(project.project_id)), [r.resolution_status for r in repo.list_references_for_project(project.project_id)])
    provider = FakeProvider(ANSWER)
    service = ExplanationService(repo, nav, AIConfig(model="m", allow_send=False), provider)  # 送信の許可なし
    result = service.infer_targets(project, index, symbols["apply"])
    assert len(provider.calls) == 1 and result.explanation is not None
    stored = repo.list_explanations(project.project_id)
    assert [(e.target_kind, e.target, e.ai_generated) for e in stored] == [("inference", "apply", True)]
    assert stored[0].status in (ExplanationStatus.VERIFIED, ExplanationStatus.PARTIAL)  # 引用は検証する
    after = (len(repo.list_symbols_for_project(project.project_id)), [r.resolution_status for r in repo.list_references_for_project(project.project_id)])
    assert before == after  # 推測は、解析結果の解決状態を変えない


def test_other_ai_features_still_require_the_send_permission(setup) -> None:
    repo, project, nav, index, symbols = setup
    provider = FakeProvider(ANSWER)
    service = ExplanationService(repo, nav, AIConfig(model="m", allow_send=False), provider)
    with pytest.raises(ConsentError):
        service.explain_symbol(project, index, symbols["apply"])  # 解説は、これまでどおり許可が要る
    assert provider.calls == []


def test_inference_to_a_remote_endpoint_still_needs_the_remote_permission(setup) -> None:
    repo, project, nav, index, symbols = setup
    provider = FakeProvider(ANSWER)
    remote = AIConfig(base_url="https://api.example.com/v1", model="m", allow_send=False)
    with pytest.raises(ConsentError, match="この計算機の外"):
        ExplanationService(repo, nav, remote, provider).infer_targets(project, index, symbols["apply"])
    assert provider.calls == []  # 外部への送信は、追加の許可なしには行わない


def test_no_pending_calls_means_the_ai_is_not_asked(setup) -> None:
    repo, project, nav, index, symbols = setup
    provider = FakeProvider(ANSWER)
    with pytest.raises(ContextError, match="未解決・曖昧な呼び出しがありません"):
        ExplanationService(repo, nav, AIConfig(model="m"), provider).infer_targets(project, index, symbols["plain"])
    assert provider.calls == []  # 対象が無ければ、問い合わせない


def test_cli_infer_dry_run(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lp.c").write_text(SOURCE, encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    capsys.readouterr()
    assert main(["infer", "apply", "--db", db, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "実行すると" in out and "未解決・曖昧な呼び出し（推測の対象）" in out

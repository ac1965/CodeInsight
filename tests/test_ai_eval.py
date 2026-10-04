from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeinsight.ai.config import AIConfig
from codeinsight.ai.evaluation import EvalError, evaluate, load_cases
from codeinsight.ai.provider import Completion
from codeinsight.cli import main

CASES_FILE = Path(__file__).resolve().parent.parent / "eval" / "ai_cases.toml"

GOOD = (
    "## 説明対象\n`app.service.orders.place_order`\n\n"
    "## 処理の概要\n注文を作り、保存して通知します [app/service/orders.py:7-11]。\n\n"
    "## 処理の詳細\n- `slug` で整形します [app/service/orders.py:8]。\n"
    "- `save` で保存し [app/service/orders.py:9]、`notify` で通知します [app/service/orders.py:10]。\n"
    "- 保存は [app/repository/store.py:5-9]、通知は [app/infra/mailer.py:8-10]。\n"
)


class Fake:
    name = "fake"
    model = "fake-model"

    def __init__(self, text: str) -> None:
        self.text = text

    def complete(self, messages, *, temperature=0.1, max_tokens=None) -> Completion:
        return Completion(self.text, self.model)


def _case_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cases.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_shipped_cases_load_and_every_target_resolves() -> None:
    cases = load_cases(CASES_FILE)
    assert len(cases) >= 8 and len({c.case_id for c in cases}) == len(cases)
    summary = evaluate(cases, AIConfig(allow_send=True, model="m"), provider=Fake("説明なし"))
    # 期待する記号の解決に失敗する（=ケースの定義誤り）と "error" になる。空の回答は "fail" になるだけ
    assert [r.case.case_id for r in summary.results if r.status == "error"] == []
    assert all(r.status == "fail" for r in summary.results)


def test_loader_rejects_bad_definitions(tmp_path: Path) -> None:
    with pytest.raises(EvalError):
        load_cases(tmp_path / "missing.toml")
    with pytest.raises(EvalError):
        load_cases(_case_file(tmp_path, '[[case]]\nid = "a"\nproject = "x"\nkind = "bogus"\n'))
    with pytest.raises(EvalError):
        load_cases(_case_file(tmp_path, '[[case]]\nid = "a"\nproject = "x"\nkind = "symbol"\n'))  # target無し


def test_good_answer_passes_and_metrics_are_aggregated() -> None:
    cases = [c for c in load_cases(CASES_FILE) if c.case_id == "layered-place-order"]
    summary = evaluate(cases, AIConfig(allow_send=True, model="m"), provider=Fake(GOOD))
    result = summary.results[0]
    assert result.evidence_recall is not None and result.evidence_recall > 0
    assert result.forbidden_hits == []
    data = summary.to_dict()
    assert data["cases"] == 1 and "pass_rate" in data


def test_invented_facts_are_caught_as_forbidden_terms() -> None:
    cases = [c for c in load_cases(CASES_FILE) if c.case_id == "layered-place-order"]
    answer = GOOD + "\n- 決済は `PaymentGateway` が行います [app/service/orders.py:9]。\n"
    result = evaluate(cases, AIConfig(allow_send=True, model="m"), provider=Fake(answer)).results[0]
    assert result.status == "fail" and "PaymentGateway" in result.forbidden_hits


def test_cli_list_needs_no_ai_and_refuses_without_consent(capsys) -> None:
    assert main(["ai-eval", "--cases", str(CASES_FILE), "--list"]) == 0
    assert "layered-place-order" in capsys.readouterr().out
    assert main(["ai-eval", "--cases", str(CASES_FILE), "--ai-model", "m"]) != 0
    assert "許可" in capsys.readouterr().err


def test_cli_runs_against_a_local_stub_and_writes_report(tmp_path: Path, capsys) -> None:
    from test_ai import _Stub

    stub = _Stub()
    try:
        stub.body = {"model": "stub", "choices": [{"message": {"content": GOOD}}]}
        report = tmp_path / "report.json"
        code = main([
            "ai-eval", "--cases", str(CASES_FILE), "--only", "layered-place-order", "--allow-send",
            "--ai-base-url", stub.url, "--ai-model", "stub", "--report", str(report), "--format", "json",
        ])
        out = capsys.readouterr().out
        assert code == 0 and len(stub.posts()) == 1
        assert json.loads(out)["cases"] == 1 and json.loads(report.read_text())["cases"] == 1
        code = main([
            "ai-eval", "--cases", str(CASES_FILE), "--only", "layered-place-order", "--allow-send",
            "--ai-base-url", stub.url, "--ai-model", "stub",
        ])
        out = capsys.readouterr().out
        assert code == 0 and "layered-place-order" in out and "合格率" in out and "機械的な指標" in out
    finally:
        stub.close()


def test_term_matching_respects_word_boundaries() -> None:
    from codeinsight.ai.evaluation import _mentions

    assert not _mentions("transform を呼びます", "ORM")  # 語の一部には一致しない
    assert _mentions("SQLAlchemy の ORM を使う", "ORM")
    assert _mentions("`math.pi` を使う", "math.pi") and not _mentions("mathxpi", "math.pi")
    assert _mentions("orders.log に書く", "orders.log") and _mentions("再帰呼び出し", "再帰")


def test_ai_eval_rejects_dry_run_instead_of_silently_sending(capsys) -> None:
    """--dry-run は「送信しない」の意味。評価は送信しないと成り立たないので、無視して送信せず、拒否する。"""

    assert main(["ai-eval", "--dry-run", "--allow-send", "--ai-model", "x", "--ai-base-url", "http://127.0.0.1:1/v1"]) == 2
    err = capsys.readouterr().err
    assert "対応していません" in err and "何も送信していません" in err

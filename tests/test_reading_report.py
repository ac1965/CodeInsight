from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from codeinsight.cli import main
from codeinsight.presentation.pdf_export import PdfExportError, find_browser, html_to_pdf
from codeinsight.presentation.reading_report import build_report

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

FAKE_BROWSER_WRITES_THEN_HANGS = """#!/bin/sh
# Chrome の安定版のように、PDFを書いた後も、終了しない
for a in "$@"; do case "$a" in --print-to-pdf=*) f="${a#--print-to-pdf=}";; esac; done
printf '%%PDF-1.4\\n%s\\n%%%%EOF\\n' "fake pdf body fake pdf body fake pdf body fake pdf body fake pdf body fake pdf body" > "$f"
sleep 120
"""
FAKE_BROWSER_NO_OUTPUT = "#!/bin/sh\necho 'boom' >&2\nexit 3\n"


def _fake(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-browser"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """make reading の成果物のうち、レポートの入力になる最小のもの。"""

    out = tmp_path / "out"
    (out / "functions").mkdir(parents=True)
    (out / "graphs").mkdir()
    (out / "analysis").mkdir()
    (out / "logs").mkdir()
    (out / "README.md").write_text(
        "# コードリーディング資料: demo\n\n## この資料が対応している範囲（言語別）\n\n* **python** — 対応: 構造。未対応: （なし）。\n\n"
        "## この資料の読み方（事実と推論の区別）\n\n* ここにある内容は、構文解析で確認できた事実です。\n",
        encoding="utf-8",
    )
    (out / "overview.json").write_text('{"project": "demo", "revision": "abc123", "languages": {"python": 3}}', encoding="utf-8")
    (out / "overview.txt").write_text("プロジェクト: demo\n" + "\n".join(f"行{n}" for n in range(30)), encoding="utf-8")
    (out / "risks.txt").write_text("<script>alert(1)</script> & 手がかり", encoding="utf-8")  # 対象由来の文字列を想定
    (out / "functions" / "demo.main_L3.txt").write_text("demo.main (function)\n  呼び出し元 0件", encoding="utf-8")
    (out / "logs" / "docs-check.txt.log").write_text("※ 名前の一致だけを見ています。", encoding="utf-8")
    return out


# --- HTML ---


def test_report_has_cover_toc_sections_and_a_content_security_policy(out_dir: Path) -> None:
    report = build_report(out_dir, target="/path/demo")
    html = report.html
    assert "コードリーディング資料" in html and "/path/demo" in html and "abc123" in html  # 表紙
    assert "<h1>目次</h1>" in html and 'href="#c1-' in html
    assert report.sections[:2] == ["この資料の読み方", "全体像"] and "付録" in report.sections
    assert "Content-Security-Policy" in html and "default-src 'none'" in html  # スクリプト・外部通信の遮断
    assert "demo.main_L3" in html and "呼び出し元 0件" in html  # 関数のカード
    assert "言語別の対応範囲" in html and "<strong>python</strong>" in html  # READMEの内容を取り込む


def test_text_from_the_target_is_escaped_and_never_becomes_markup(out_dir: Path) -> None:
    html = build_report(out_dir).html
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_long_outputs_are_truncated_with_an_explicit_note(out_dir: Path) -> None:
    report = build_report(out_dir, max_lines=10)
    assert "全 31 行のうち、先頭 10 行だけを載せています" in report.html
    assert any("overview.txt" in item and "先頭 10 行" in item for item in report.omitted)
    assert "省略・打ち切りしたもの" in report.html  # 付録にも記録する


def test_missing_files_and_oversized_graphs_are_reported_not_hidden(out_dir: Path) -> None:
    nodes = "\n".join(f'  n{i} [label="n{i}"];' for i in range(70))
    (out_dir / "graphs" / "call.dot").write_text("digraph g {\n" + nodes + "\n}\n")
    report = build_report(out_dir, max_graph_nodes=60)
    assert any("graphs/call" in item and "70 個" in item for item in report.omitted)
    assert "ノードが 70 個あり" in report.html
    assert any("boundaries.txt" in item and "生成されていません" in item for item in report.omitted)  # 無いファイルを黙って飛ばさない


@pytest.mark.skipif(shutil.which("dot") is None, reason="graphviz（dot）が必要")
def test_small_graphs_are_embedded_as_svg(out_dir: Path) -> None:
    (out_dir / "graphs" / "arch.dot").write_text('digraph g { a [label="A"]; b [label="B"]; a -> b; }\n')
    report = build_report(out_dir)
    assert "arch" in report.graphs and "<svg" in report.html


def test_ai_explanations_are_marked_as_not_analysis_results(out_dir: Path) -> None:
    (out_dir / "ai").mkdir()
    (out_dir / "ai" / "demo.main_L3.md").write_text("━━ AI解説（解析結果ではありません）\n## 説明対象", encoding="utf-8")
    html = build_report(out_dir).html
    assert "AIの解説" in html and "解析結果（事実）ではありません" in html


# --- PDF変換 ---


def test_pdf_is_returned_as_soon_as_it_is_complete_even_if_the_browser_never_exits(tmp_path: Path) -> None:
    html = tmp_path / "r.html"
    html.write_text("<html></html>")
    started = time.monotonic()
    pdf = html_to_pdf(html, tmp_path / "r.pdf", browser=_fake(tmp_path, FAKE_BROWSER_WRITES_THEN_HANGS), timeout=60)
    assert pdf.read_bytes().startswith(b"%PDF") and time.monotonic() - started < 20  # 終了を待たずに、PDFの完成で返る（Chrome安定版の挙動）


def test_browser_failures_are_reported_clearly(tmp_path: Path, monkeypatch) -> None:
    html = tmp_path / "r.html"
    html.write_text("<html></html>")
    with pytest.raises(PdfExportError) as failed:
        html_to_pdf(html, tmp_path / "x.pdf", browser=_fake(tmp_path, FAKE_BROWSER_NO_OUTPUT), timeout=20)
    assert "PDFが作られませんでした" in str(failed.value) and "終了コード 3" in str(failed.value)

    monkeypatch.setenv("CODEINSIGHT_BROWSER", str(tmp_path / "missing-browser"))
    assert find_browser() is None
    with pytest.raises(PdfExportError) as none:
        html_to_pdf(html, tmp_path / "y.pdf")
    assert "見つかりません" in str(none.value) and "CODEINSIGHT_BROWSER" in str(none.value) and str(html) in str(none.value)  # HTMLは残っていることを伝える


# --- CLI / make ---


def test_cli_reading_report_writes_html_and_pdf(out_dir: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CODEINSIGHT_BROWSER", _fake(tmp_path, FAKE_BROWSER_WRITES_THEN_HANGS))
    assert main(["reading-report", "--out", str(out_dir), "--target", "/path/demo", "--max-lines", "20"]) == 0
    text = capsys.readouterr().out
    assert (out_dir / "out-reading.html").is_file() and (out_dir / "out-reading.pdf").read_bytes().startswith(b"%PDF")
    assert "PDF:" in text and "打ち切り" in text

    assert main(["reading-report", "--out", str(out_dir), "--format", "html", "--name", "plain"]) == 0
    assert (out_dir / "plain.html").is_file() and not (out_dir / "plain.pdf").exists()


def test_cli_reports_failure_when_no_browser_but_keeps_the_html(out_dir: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("CODEINSIGHT_BROWSER", "/nonexistent/browser")
    assert main(["reading-report", "--out", str(out_dir)]) == 1  # PDFを作れなかったことを、終了コードで示す
    assert "PDFを作れませんでした" in capsys.readouterr().out and (out_dir / "out-reading.html").is_file()
    assert main(["reading-report", "--out", str(out_dir / "nothing")]) == 2  # 成果物がない


def test_make_reading_creates_the_single_file_report_and_lists_it_in_the_index(tmp_path: Path) -> None:
    target = tmp_path / "target"
    shutil.copytree(FIXTURES / "layered", target)
    out = tmp_path / "out"
    env = {**os.environ, "CODEINSIGHT_BROWSER": _fake(tmp_path, FAKE_BROWSER_WRITES_THEN_HANGS)}
    result = subprocess.run(["make", "--no-print-directory", "reading", f"TARGET={target}", f"OUT={out}", "TOP=2"],
                            cwd=ROOT, capture_output=True, text=True, timeout=300, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (out / "out-reading.pdf").is_file() and (out / "out-reading.html").is_file()
    assert "1ファイル版" in (out / "README.md").read_text(encoding="utf-8")
    html = (out / "out-reading.html").read_text(encoding="utf-8")
    assert "app.service.orders.place_order" in html and "全体像" in html  # 実際の成果物がまとまっている

    skipped = subprocess.run(["make", "--no-print-directory", "reading", f"TARGET={target}", f"OUT={tmp_path / 'nopdf'}", "TOP=2", "PDF=0"],
                             cwd=ROOT, capture_output=True, text=True, timeout=300, env=env)
    assert skipped.returncode == 0 and not list((tmp_path / "nopdf").glob("*.pdf"))  # PDF=0 では作らない


@pytest.mark.skipif(find_browser() is None, reason="PDF変換のブラウザが必要")
def test_a_real_browser_produces_a_multi_page_pdf(out_dir: Path, tmp_path: Path) -> None:
    html = tmp_path / "real.html"
    html.write_text(build_report(out_dir).html, encoding="utf-8")
    pdf = html_to_pdf(html, tmp_path / "real.pdf", timeout=120)
    data = pdf.read_bytes()
    assert data.startswith(b"%PDF") and data.rstrip().endswith(b"%%EOF") and len(data) > 5000


# --- AI解説の追記（AI_SEND=1） ---

AI_TEXT = (
    "━━ AI解説（解析結果ではありません） モデル: qwen3-coder:latest / 対象: demo.main\n   解説ID: abc（保存済み）\n\n## 説明対象\n`demo.main`\n\n"
    "──── 検証結果: 一部未確認（根拠の示されていない記述を含む）\n  引用 4件のうち検証できたもの 3件 / 根拠のある記述 4行\n"
)


def test_ai_chapter_starts_with_a_verification_summary_and_a_warning(out_dir: Path) -> None:
    (out_dir / "ai").mkdir()
    (out_dir / "ai" / "demo.main_L3.md").write_text(AI_TEXT, encoding="utf-8")
    (out_dir / "ai" / "demo.other_L9.md").write_text(AI_TEXT.replace("一部未確認（根拠の示されていない記述を含む）", "未検証（引用の誤り・存在しない名前・根拠なし）"), encoding="utf-8")
    html = build_report(out_dir).html
    assert "検証状態の一覧" in html and "qwen3-coder:latest" in html and "3 / 4" in html
    assert "一部未確認 1 件 / 未検証 1 件" in html and "検証済み 0 件" in html
    assert "「未検証」の解説は、事実として扱わないでください" in html  # 検証できていない解説を、事実として読ませない


def _stub_env(tmp_path: Path, stub) -> dict[str, str]:
    return {**os.environ, "CODEINSIGHT_BROWSER": _fake(tmp_path, FAKE_BROWSER_WRITES_THEN_HANGS),
            "CODEINSIGHT_AI_BASE_URL": stub.url, "CODEINSIGHT_AI_MODEL": "stub-model"}


def test_make_reading_with_ai_send_appends_ai_explanations_to_the_pdf(tmp_path: Path) -> None:
    from test_ai import _Stub  # AIの代わりに、この計算機の中だけで動くHTTPスタブ

    stub = _Stub()
    try:
        stub.body = {"model": "stub-model", "choices": [{"message": {"content": "## 説明対象\n解説です。"}}]}
        target = tmp_path / "target"
        shutil.copytree(FIXTURES / "layered", target)
        out = tmp_path / "out"
        # MODEL= は指定しない: AI_SEND=1 だけで動く（モデルは環境変数から解決される）
        result = subprocess.run(["make", "--no-print-directory", "reading", f"TARGET={target}", f"OUT={out}", "TOP=2", "AI_SEND=1"],
                                cwd=ROOT, capture_output=True, text=True, timeout=300, env=_stub_env(tmp_path, stub))
        assert result.returncode == 0, result.stdout + result.stderr
        assert list((out / "ai").glob("*.md")) and len(stub.posts()) >= 1  # AIへ送信された（許可あり）
        html = (out / "out-reading.html").read_text(encoding="utf-8")
        assert "AIの解説" in html and "検証状態の一覧" in html and "解析結果（事実）ではありません" in html
        assert "## AIの解説" in (out / "README.md").read_text(encoding="utf-8") and "ai/" in (out / "README.md").read_text(encoding="utf-8")
    finally:
        stub.close()


def test_make_reading_without_ai_send_never_contacts_the_ai(tmp_path: Path) -> None:
    from test_ai import _Stub

    stub = _Stub()
    try:
        target = tmp_path / "target"
        shutil.copytree(FIXTURES / "layered", target)
        out = tmp_path / "out"
        result = subprocess.run(["make", "--no-print-directory", "reading", f"TARGET={target}", f"OUT={out}", "TOP=2", "PDF=0"],
                                cwd=ROOT, capture_output=True, text=True, timeout=300, env=_stub_env(tmp_path, stub))
        assert result.returncode == 0 and "AI解説はスキップ" in result.stdout
        assert stub.posts() == [] and not (out / "ai").exists()  # AI_SEND=1 が無ければ、何も送信しない
    finally:
        stub.close()


def test_make_reading_with_ai_send_but_no_model_explains_why_and_keeps_the_other_outputs(tmp_path: Path) -> None:
    target = tmp_path / "target"
    shutil.copytree(FIXTURES / "layered", target)
    out = tmp_path / "out"
    env = {k: v for k, v in os.environ.items() if k != "CODEINSIGHT_AI_MODEL"} | {"HOME": str(tmp_path)}  # 設定ファイルも無い
    result = subprocess.run(["make", "--no-print-directory", "reading", f"TARGET={target}", f"OUT={out}", "TOP=1", "AI_SEND=1", "PDF=0"],
                            cwd=ROOT, capture_output=True, text=True, timeout=300, env=env)
    assert result.returncode == 0  # 他の成果物は作る
    assert "AI解説を作れませんでした" in result.stdout and "モデルが指定されていません" in result.stdout  # 原因を示す
    assert (out / "overview.txt").is_file() and not list((out / "ai").glob("*.md"))

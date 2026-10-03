from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codeinsight.cli import main

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

pytestmark = pytest.mark.skipif(shutil.which("make") is None or shutil.which("uv") is None, reason="make / uv が必要")


def _make(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(["make", "--no-print-directory", *args], cwd=cwd, capture_output=True, text=True, timeout=300)


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


def test_make_reading_produces_the_reading_materials_without_touching_the_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    shutil.copytree(FIXTURES / "layered", target)
    before = _snapshot(target)
    out = tmp_path / "out"

    result = _make("reading", f"TARGET={target}", f"OUT={out}", "TOP=3")

    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("README.md", "overview.txt", "architecture.txt", "boundaries.txt", "externals.txt", "analysis/unresolved.txt", "graphs/call.html", "graphs/arch.mmd"):
        assert (out / name).is_file(), name
    assert list((out / "functions").glob("*.txt")), "主要な関数の読解カードがある"
    index = (out / "README.md").read_text(encoding="utf-8")
    assert "この資料が対応している範囲（言語別）" in index and "実行順序や実際に通る経路を示すものではありません" in index
    assert "生成していません" in index  # AIは既定で使わない（AI_SEND=1 が無い）
    assert not (out / "ai").exists()
    assert _snapshot(target) == before  # 対象のソースは変更されない（出力は対象の外）


def test_make_reading_refuses_an_output_inside_the_target_and_a_missing_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    shutil.copytree(FIXTURES / "layered", target)

    inside = _make("reading", f"TARGET={target}", f"OUT={target}/out")
    assert inside.returncode != 0 and "TARGET の外" in inside.stdout + inside.stderr
    assert not (target / "out" / "README.md").exists()

    assert _make("reading").returncode != 0  # TARGET 必須
    assert _make("reading", f"TARGET={tmp_path / 'nothing'}", f"OUT={tmp_path / 'o'}").returncode != 0


def test_make_c_build_requires_explicit_permission(tmp_path: Path) -> None:
    result = _make("reading-c-build", f"TARGET={FIXTURES / 'c_callgraph'}", f"OUT={tmp_path / 'o'}")
    assert result.returncode != 0 and "ALLOW_BUILD=1" in result.stdout + result.stderr  # 対象のconfigure/makeは許可なしに実行しない


def test_risks_says_when_the_project_has_files_it_cannot_check(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "c.sqlite")
    assert main(["analyze", str(FIXTURES / "c_callgraph"), "--db", db]) == 0
    capsys.readouterr()
    assert main(["risks", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "0件" in out and "Pythonのみ対応" in out and "「問題なし」を意味しません" in out


@pytest.mark.parametrize("command, fragment", [
    (["risks"], "リスクの検出はPythonのみ対応"),
    (["config"], "設定値の検出はPythonのみ対応"),
    (["environment"], "実行環境の前提の抽出はPythonのみ対応"),
    (["boundaries"], "入口と境界の検出は、c ではmain 関数のみ対応"),
])
def test_language_limited_commands_say_what_they_did_not_check(tmp_path: Path, capsys, command: list[str], fragment: str) -> None:
    db = str(tmp_path / "c.sqlite")
    assert main(["analyze", str(FIXTURES / "c_callgraph"), "--db", db]) == 0
    capsys.readouterr()
    assert main([*command, "--db", db]) == 0
    assert fragment in capsys.readouterr().out  # 「0件」「確認できませんでした」を「問題なし」と読ませない


def test_coverage_note_goes_to_stderr_for_json_and_is_absent_for_python_only_projects(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "c.sqlite")
    assert main(["analyze", str(FIXTURES / "c_callgraph"), "--db", db]) == 0
    capsys.readouterr()
    assert main(["config", "--format", "json", "--db", db]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "[]" and "Pythonのみ対応" in captured.err  # JSONの形式は変えない

    py = str(tmp_path / "p.sqlite")
    assert main(["analyze", str(FIXTURES / "python_flow"), "--db", py]) == 0
    capsys.readouterr()
    assert main(["risks", "--db", py]) == 0
    assert "Pythonのみ対応" not in capsys.readouterr().out


def test_understand_marks_unsupported_sections_instead_of_leaving_them_blank(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "c.sqlite")
    assert main(["analyze", str(FIXTURES / "c_callgraph"), "--db", db]) == 0
    capsys.readouterr()
    assert main(["understand", "apply", "--db", db]) == 0
    out = capsys.readouterr().out
    assert out.count("対象外") >= 3 and "空欄は「なし」を意味しません" in out
    assert "ありません（静的に追える範囲）" not in out  # Cで「変更なし」と断定しない

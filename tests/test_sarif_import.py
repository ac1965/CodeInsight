from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codeinsight.cli import main
from codeinsight.infrastructure.sarif import SarifError, parse_sarif, to_relative_path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    shutil.copytree(FIXTURES / "elisp_sample", root)
    return root


def _analyze(root: Path, db: Path) -> None:
    assert main(["analyze", str(root), "--db", str(db)]) == 0


def test_parse_extracts_location_and_counts_the_rest() -> None:
    document = parse_sarif((FIXTURES / "sarif" / "sample.sarif").read_bytes())
    assert document.tools == ["demo-linter"] and document.without_location == 1
    first = document.results[0]
    assert (first.rule_id, first.level, first.uri, first.start_line) == ("DL001", "error", "core.el", 84)
    assert document.results[1].level == "warning" and document.results[1].end_line == 3  # level 省略は warning、endLine 省略は開始行


@pytest.mark.parametrize("data", [b"not json", b"[]", b'{"runs": 1}', b'{"version": "1.0.0", "runs": []}'])
def test_parse_rejects_non_sarif(data: bytes) -> None:
    with pytest.raises(SarifError):
        parse_sarif(data)


def test_paths_outside_the_project_are_rejected(tmp_path: Path) -> None:
    assert to_relative_path("a/b.c", tmp_path) == "a/b.c"
    assert to_relative_path(f"file://{tmp_path}/a/b.c", tmp_path) == "a/b.c"
    assert to_relative_path("../x.c", tmp_path) is None
    assert to_relative_path("/etc/passwd", tmp_path) is None
    assert to_relative_path("https://example.com/a.c", tmp_path) is None


def test_import_keeps_external_findings_separate_and_reports_skips(project: Path, tmp_path: Path, capsys) -> None:
    db = tmp_path / "s.db"
    _analyze(project, db)
    capsys.readouterr()
    assert main(["import-sarif", str(FIXTURES / "sarif" / "sample.sarif"), "--db", str(db), "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "2件 取り込みました" in out and "位置（ファイル・行）を持たない" in out
    assert "プロジェクトのルートの外" in out and "存在しないファイル" in out
    assert main(["risks", "--db", str(db), "--project", str(project)]) == 0
    risks = capsys.readouterr().out
    assert "外部ツールの指摘" in risks and "CodeInsight自身の解析結果ではなく" in risks
    assert "\x1b" not in risks  # メッセージ中の制御文字を、端末に出さない
    assert main(["risks", "--format", "json", "--db", str(db), "--project", str(project)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {f["origin"] for f in data["external_findings"]} == {"external"}
    assert all(f["origin"] != "external" for f in data["findings"] if "origin" in f)  # 自身の指摘と混ざらない


def test_reimport_replaces_and_clear_removes(project: Path, tmp_path: Path, capsys) -> None:
    db, sarif = tmp_path / "s.db", FIXTURES / "sarif" / "sample.sarif"
    _analyze(project, db)
    args = ["--db", str(db), "--project", str(project)]
    for _ in range(2):
        assert main(["import-sarif", str(sarif), *args]) == 0
    capsys.readouterr()
    assert main(["risks", "--format", "json", *args]) == 0
    assert len(json.loads(capsys.readouterr().out)["external_findings"]) == 2  # 同じ内容の再取り込みで、重複しない
    assert main(["import-sarif", "--clear", *args]) == 0
    capsys.readouterr()
    assert main(["risks", "--format", "json", *args]) == 0
    assert json.loads(capsys.readouterr().out)["external_findings"] == []


def test_findings_become_stale_when_the_file_changes_and_are_shown_in_understand(project: Path, tmp_path: Path, capsys) -> None:
    db, sarif = tmp_path / "s.db", FIXTURES / "sarif" / "sample.sarif"
    _analyze(project, db)
    args = ["--db", str(db), "--project", str(project)]
    assert main(["import-sarif", str(sarif), *args]) == 0
    capsys.readouterr()
    assert main(["understand", "core-pipeline", *args]) == 0
    assert "外部ツールの指摘（demo-linter" in capsys.readouterr().out  # core.el:84 は core-pipeline の範囲
    core = project / "core.el"
    core.write_text(";; 追記\n" + core.read_text(encoding="utf-8"), encoding="utf-8")
    assert main(["risks", "--format", "json", *args]) == 0
    data = json.loads(capsys.readouterr().out)
    assert all(f["stale"] for f in data["external_findings"] if f["path"] == "core.el")  # 古い指摘として示す


def test_import_does_not_modify_the_target(project: Path, tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _analyze(project, db)
    before = {p.name: p.read_bytes() for p in project.iterdir()}
    assert main(["import-sarif", str(FIXTURES / "sarif" / "sample.sarif"), "--db", str(db), "--project", str(project)]) == 0
    assert {p.name: p.read_bytes() for p in project.iterdir()} == before

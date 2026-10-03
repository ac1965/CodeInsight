from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from codeinsight.analysis import CAnalyzer, PythonAnalyzer, SymbolExtractor
from codeinsight.analysis.language_adapter import FileAnalysis, SourceUnit
from codeinsight.analysis.python_analyzer import PythonAnalyzer as PyAnalyzer
from codeinsight.application import AnalysisCoordinator, ProjectManager
from codeinsight.application.analysis_coordinator import AnalysisProgress
from codeinsight.domain import AnalysisFileStatus, AnalysisStatus, Language
from codeinsight.infrastructure import AnalysisRepository


def _extractor() -> SymbolExtractor:
    return SymbolExtractor({Language.C: CAnalyzer(), Language.PYTHON: PythonAnalyzer()})


def _make_project(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def test_symbol_ids_are_deterministic_and_survive_line_shifts(tmp_path: Path) -> None:
    root = _make_project(tmp_path, {"a.py": "def f():\n    return 1\n\n\ndef g():\n    return 2\n"})
    unit = SourceUnit.from_path("file-1", root / "a.py")
    first = {s.qualified_name: s.symbol_id for s in PyAnalyzer().analyze_file(unit).symbols}
    second = {s.qualified_name: s.symbol_id for s in PyAnalyzer().analyze_file(unit).symbols}
    assert first == second

    # 先頭に行を足して行番号がずれても、シンボルIDは変わらない
    (root / "a.py").write_text("# comment\n\n" + (root / "a.py").read_text(), encoding="utf-8")
    shifted = SourceUnit.from_path("file-1", root / "a.py")
    third = {s.qualified_name: s.symbol_id for s in PyAnalyzer().analyze_file(shifted).symbols}
    assert third == first


def test_same_named_modules_in_different_packages_have_distinct_names(tmp_path: Path) -> None:
    root = _make_project(
        tmp_path,
        {"pkg/a/utils.py": "def f():\n    pass\n", "pkg/b/utils.py": "def f():\n    pass\n"},
    )
    with AnalysisRepository(tmp_path / "db.sqlite") as repo:
        project = ProjectManager(repo).register(root)
        AnalysisCoordinator(repo, _extractor()).analyze_project(project)
        names = {s.qualified_name for s in repo.list_symbols_for_project(project.project_id)}
    assert {"pkg.a.utils", "pkg.b.utils", "pkg.a.utils.f", "pkg.b.utils.f"} <= names


def test_failed_reanalysis_removes_stale_symbols(tmp_path: Path) -> None:
    root = _make_project(tmp_path, {"a.py": "def old():\n    pass\n"})
    with AnalysisRepository(tmp_path / "db.sqlite") as repo:
        project = ProjectManager(repo).register(root)
        coordinator = AnalysisCoordinator(repo, _extractor())
        coordinator.analyze_project(project)
        assert any(s.name == "old" for s in repo.list_symbols_for_project(project.project_id))

        (root / "a.py").write_text("def broken(:\n", encoding="utf-8")
        result = coordinator.analyze_project(project)

        assert result.status == AnalysisStatus.FAILED
        (source_file,) = repo.list_source_files(project.project_id)
        assert source_file.analysis_status == AnalysisFileStatus.FAILED
        assert repo.list_symbols_for_project(project.project_id) == []


def test_removed_files_are_deleted_from_results(tmp_path: Path) -> None:
    root = _make_project(tmp_path, {"a.py": "def a():\n    pass\n", "b.py": "def b():\n    pass\n"})
    with AnalysisRepository(tmp_path / "db.sqlite") as repo:
        project = ProjectManager(repo).register(root)
        coordinator = AnalysisCoordinator(repo, _extractor())
        coordinator.analyze_project(project)

        (root / "b.py").unlink()
        coordinator.analyze_project(project)

        assert [f.relative_path for f in repo.list_source_files(project.project_id)] == ["a.py"]
        assert {s.name for s in repo.list_symbols_for_project(project.project_id)} == {"a"}


@pytest.mark.skipif(os.geteuid() == 0, reason="rootではchmodで読み込み不能にできない")
def test_unreadable_file_is_recorded_and_other_files_continue(tmp_path: Path) -> None:
    root = _make_project(tmp_path, {"good.py": "def good():\n    pass\n", "bad.py": "x = 1\n"})
    (root / "bad.py").chmod(0o000)
    try:
        with AnalysisRepository(tmp_path / "db.sqlite") as repo:
            project = ProjectManager(repo).register(root)
            result = AnalysisCoordinator(repo, _extractor()).analyze_project(project)

            assert result.status == AnalysisStatus.PARTIAL
            assert any("bad.py" in e for e in result.errors)
            statuses = {
                f.relative_path: f.analysis_status for f in repo.list_source_files(project.project_id)
            }
            assert statuses == {
                "good.py": AnalysisFileStatus.ANALYZED,
                "bad.py": AnalysisFileStatus.FAILED,
            }
    finally:
        (root / "bad.py").chmod(0o644)


class _ExplodingAdapter:
    language = Language.PYTHON

    def analyze_file(self, unit: SourceUnit) -> FileAnalysis:
        if unit.relative_path == "boom.py":
            raise RuntimeError("想定外の失敗")
        return PyAnalyzer().analyze_file(unit)


def test_unexpected_analyzer_exception_does_not_abort_analysis(tmp_path: Path) -> None:
    root = _make_project(tmp_path, {"boom.py": "x = 1\n", "ok.py": "def ok():\n    pass\n"})
    extractor = SymbolExtractor({Language.PYTHON: _ExplodingAdapter()})
    with AnalysisRepository(tmp_path / "db.sqlite") as repo:
        project = ProjectManager(repo).register(root)
        result = AnalysisCoordinator(repo, extractor).analyze_project(project)

        assert result.status == AnalysisStatus.PARTIAL
        assert any("boom.py" in e and "RuntimeError" in e for e in result.errors)
        assert {s.name for s in repo.list_symbols_for_project(project.project_id)} >= {"ok"}


def test_progress_callback_reports_each_file(tmp_path: Path) -> None:
    root = _make_project(tmp_path, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    events: list[AnalysisProgress] = []
    with AnalysisRepository(tmp_path / "db.sqlite") as repo:
        project = ProjectManager(repo).register(root)
        AnalysisCoordinator(repo, _extractor()).analyze_project(project, progress=events.append)
    assert [(e.done, e.total) for e in events] == [(1, 2), (2, 2)]


def _git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    completed = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, env=env
    )
    return completed.stdout.strip()


def test_repository_revision_is_refreshed_on_each_analysis(tmp_path: Path) -> None:
    root = _make_project(tmp_path, {"a.py": "x = 1\n"})
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "first")
    first_revision = _git(root, "rev-parse", "HEAD")

    with AnalysisRepository(tmp_path / "db.sqlite") as repo:
        project = ProjectManager(repo).register(root)
        coordinator = AnalysisCoordinator(repo, _extractor())
        result = coordinator.analyze_project(project)
        assert result.repository_revision == first_revision

        (root / "a.py").write_text("x = 2\n", encoding="utf-8")
        _git(root, "commit", "-q", "-am", "second")
        second_revision = _git(root, "rev-parse", "HEAD")
        result = coordinator.analyze_project(project)

        assert result.repository_revision == second_revision
        assert repo.get_project(project.project_id).repository_revision == second_revision


def test_analyzer_version_change_triggers_reanalysis(tmp_path: Path, monkeypatch) -> None:
    root = _make_project(tmp_path, {"a.py": "x = 1\n"})
    with AnalysisRepository(tmp_path / "db.sqlite") as repo:
        project = ProjectManager(repo).register(root)
        coordinator = AnalysisCoordinator(repo, _extractor())
        coordinator.analyze_project(project)
        (before,) = repo.list_source_files(project.project_id)

        monkeypatch.setattr(
            "codeinsight.application.analysis_coordinator.ANALYZER_VERSION", "99.0.0"
        )
        coordinator.analyze_project(project)
        (after,) = repo.list_source_files(project.project_id)

    assert before.analyzer_version != after.analyzer_version == "99.0.0"

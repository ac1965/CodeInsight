from __future__ import annotations

from pathlib import Path

from codeinsight.analysis import CAnalyzer, PythonAnalyzer, SymbolExtractor
from codeinsight.application import AnalysisCoordinator, ProjectManager
from codeinsight.domain import AnalysisFileStatus, AnalysisStatus, Language
from codeinsight.infrastructure import AnalysisRepository


def _build_extractor() -> SymbolExtractor:
    return SymbolExtractor({Language.C: CAnalyzer(), Language.PYTHON: PythonAnalyzer()})


def test_analyze_python_project_end_to_end(tmp_path: Path, python_sample_dir: Path) -> None:
    with AnalysisRepository(tmp_path / "analysis.db") as repo:
        project = ProjectManager(repo).register(python_sample_dir, name="python_sample")
        result = AnalysisCoordinator(repo, _build_extractor()).analyze_project(project)

        assert result.status == AnalysisStatus.SUCCESS
        source_files = repo.list_source_files(project.project_id)
        assert {f.relative_path for f in source_files} == {"shapes.py", "main.py"}

        names = {s.name for s in repo.list_symbols_for_project(project.project_id)}
        assert {"Shape", "Circle", "main", "describe"} <= names


def test_analyze_c_project_end_to_end(tmp_path: Path, c_sample_dir: Path) -> None:
    with AnalysisRepository(tmp_path / "analysis.db") as repo:
        project = ProjectManager(repo).register(c_sample_dir, name="c_sample")
        result = AnalysisCoordinator(repo, _build_extractor()).analyze_project(project)

        assert result.status == AnalysisStatus.SUCCESS
        source_files = repo.list_source_files(project.project_id)
        assert {f.relative_path for f in source_files} == {"main.c", "util.c", "util.h"}

        names = {s.name for s in repo.list_symbols_for_project(project.project_id)}
        assert {"add", "factorial", "main"} <= names


def test_analysis_error_is_recorded_without_blocking_other_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "mixed"
    project_dir.mkdir()
    (project_dir / "good.py").write_text("def good():\n    return 1\n", encoding="utf-8")
    (project_dir / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

    with AnalysisRepository(tmp_path / "analysis.db") as repo:
        project = ProjectManager(repo).register(project_dir)
        result = AnalysisCoordinator(repo, _build_extractor()).analyze_project(project)

        assert result.status == AnalysisStatus.PARTIAL
        assert result.errors

        source_files = {f.relative_path: f for f in repo.list_source_files(project.project_id)}
        assert source_files["good.py"].analysis_status == AnalysisFileStatus.ANALYZED
        assert source_files["broken.py"].analysis_status == AnalysisFileStatus.FAILED

        symbols = repo.list_symbols_for_project(project.project_id)
        assert any(s.name == "good" for s in symbols)
        assert not any(s.name == "broken" for s in symbols)


def test_reanalysis_skips_unchanged_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")

    with AnalysisRepository(tmp_path / "analysis.db") as repo:
        project = ProjectManager(repo).register(project_dir)
        coordinator = AnalysisCoordinator(repo, _build_extractor())

        coordinator.analyze_project(project)
        first_ids = {s.symbol_id for s in repo.list_symbols_for_project(project.project_id)}

        coordinator.analyze_project(project)
        second_ids = {s.symbol_id for s in repo.list_symbols_for_project(project.project_id)}

        # 内容が変わっていなければ再解析がスキップされ、シンボルIDは再生成されない
        assert first_ids == second_ids

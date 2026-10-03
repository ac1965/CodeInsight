from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def c_sample_dir() -> Path:
    return FIXTURES_DIR / "c_sample"


@pytest.fixture
def python_sample_dir() -> Path:
    return FIXTURES_DIR / "python_sample"


@pytest.fixture
def c_callgraph_dir() -> Path:
    return FIXTURES_DIR / "c_callgraph"


@pytest.fixture
def python_pkg_dir() -> Path:
    return FIXTURES_DIR / "python_pkg"


@pytest.fixture
def analyzed(tmp_path: Path):
    """プロジェクトを解析し、(repository, project, 結果) を返すファクトリ。"""

    from codeinsight.analysis import CAnalyzer, PythonAnalyzer, SymbolExtractor
    from codeinsight.application import AnalysisCoordinator, ProjectManager
    from codeinsight.domain import Language
    from codeinsight.infrastructure import AnalysisRepository

    repositories: list[AnalysisRepository] = []

    def build(root: Path):
        repo = AnalysisRepository(tmp_path / f"db{len(repositories)}.sqlite")
        repositories.append(repo)
        extractor = SymbolExtractor({Language.C: CAnalyzer(), Language.PYTHON: PythonAnalyzer()})
        project = ProjectManager(repo).register(root)
        result = AnalysisCoordinator(repo, extractor).analyze_project(project)
        return repo, project, result

    yield build
    for repo in repositories:
        repo.close()


@pytest.fixture
def python_reexport_dir() -> Path:
    return FIXTURES_DIR / "python_reexport"


@pytest.fixture
def python_doc_dir() -> Path:
    return FIXTURES_DIR / "python_doc"


@pytest.fixture
def c_doc_dir() -> Path:
    return FIXTURES_DIR / "c_doc"


@pytest.fixture
def python_flow_dir() -> Path:
    return FIXTURES_DIR / "python_flow"


@pytest.fixture
def layered_dir() -> Path:
    return FIXTURES_DIR / "layered"

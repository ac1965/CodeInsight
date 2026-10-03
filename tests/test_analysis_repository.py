from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from codeinsight.domain import (
    AnalysisFileStatus,
    AnalysisResult,
    AnalysisStatus,
    Language,
    Project,
    SourceFile,
    Symbol,
    SymbolKind,
)
from codeinsight.infrastructure.analysis_repository import AnalysisRepository


def test_save_and_reload_project_source_file_and_symbols(tmp_path: Path) -> None:
    db_path = tmp_path / "analysis.db"
    project = Project(project_id="p1", root_path=tmp_path, name="demo")

    with AnalysisRepository(db_path) as repo:
        repo.save_project(project)
        repo.save_source_file(
            SourceFile(
                file_id="f1",
                project_id="p1",
                relative_path="main.py",
                language=Language.PYTHON,
                content_hash="abc123",
                last_analyzed_at=datetime.now(UTC),
                analysis_status=AnalysisFileStatus.ANALYZED,
            )
        )
        repo.replace_symbols_for_file(
            "f1",
            [
                Symbol(
                    symbol_id="s1",
                    file_id="f1",
                    name="main",
                    qualified_name="main",
                    kind=SymbolKind.FUNCTION,
                    start_line=1,
                    end_line=3,
                )
            ],
        )
        repo.save_analysis_result(
            AnalysisResult(
                analysis_id="a1",
                project_id="p1",
                analyzer_version="0.1.0",
                analysis_timestamp=datetime.now(UTC),
                status=AnalysisStatus.SUCCESS,
            )
        )

    # 別接続で開き直しても永続化されていることを確認する（3.10: 保存と再利用）
    with AnalysisRepository(db_path) as repo:
        reloaded = repo.get_project("p1")
        assert reloaded is not None
        assert reloaded.name == "demo"

        files = repo.list_source_files("p1")
        assert [f.relative_path for f in files] == ["main.py"]

        symbols = repo.list_symbols_for_file("f1")
        assert [s.name for s in symbols] == ["main"]

        results = repo.list_analysis_results("p1")
        assert len(results) == 1
        assert results[0].status == AnalysisStatus.SUCCESS


def test_replace_symbols_removes_stale_entries(tmp_path: Path) -> None:
    db_path = tmp_path / "analysis.db"
    with AnalysisRepository(db_path) as repo:
        repo.save_project(Project(project_id="p1", root_path=tmp_path, name="demo"))
        repo.save_source_file(
            SourceFile(
                file_id="f1",
                project_id="p1",
                relative_path="a.py",
                language=Language.PYTHON,
                content_hash="hash1",
            )
        )
        repo.replace_symbols_for_file(
            "f1",
            [
                Symbol(
                    symbol_id="s1",
                    file_id="f1",
                    name="old_func",
                    qualified_name="old_func",
                    kind=SymbolKind.FUNCTION,
                    start_line=1,
                    end_line=2,
                )
            ],
        )
        repo.replace_symbols_for_file(
            "f1",
            [
                Symbol(
                    symbol_id="s2",
                    file_id="f1",
                    name="new_func",
                    qualified_name="new_func",
                    kind=SymbolKind.FUNCTION,
                    start_line=1,
                    end_line=2,
                )
            ],
        )

        symbols = repo.list_symbols_for_file("f1")
        assert [s.name for s in symbols] == ["new_func"]


def test_delete_project_removes_dependent_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "analysis.db"
    with AnalysisRepository(db_path) as repo:
        repo.save_project(Project(project_id="p1", root_path=tmp_path, name="demo"))
        repo.save_source_file(
            SourceFile(
                file_id="f1",
                project_id="p1",
                relative_path="a.py",
                language=Language.PYTHON,
                content_hash="hash1",
            )
        )
        repo.replace_symbols_for_file(
            "f1",
            [
                Symbol(
                    symbol_id="s1",
                    file_id="f1",
                    name="f",
                    qualified_name="f",
                    kind=SymbolKind.FUNCTION,
                    start_line=1,
                    end_line=2,
                )
            ],
        )

        repo.delete_project("p1")

        assert repo.get_project("p1") is None
        assert repo.list_source_files("p1") == []
        assert repo.list_symbols_for_file("f1") == []

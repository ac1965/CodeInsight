from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codeinsight.domain import Confidence, ReferenceKind, ResolutionStatus, SymbolKind

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="Goのツールチェーン（go）が必要")

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "go_sample"
MODULE = "example.com/demo"


@pytest.fixture
def view(analyzed, tmp_path: Path):
    root = tmp_path / "demo"
    shutil.copytree(SAMPLE, root)
    (root / "broken.go").unlink()  # 構文エラーのファイルは、別のテストで扱う
    repo, project, result = analyzed(root)
    symbols = {s.symbol_id: s for s in repo.list_symbols_for_project(project.project_id)}
    refs = repo.list_references_for_project(project.project_id)
    deps = repo.list_dependencies_for_project(project.project_id) if hasattr(repo, "list_dependencies_for_project") else []
    files = {f.file_id: f.relative_path for f in repo.list_source_files(project.project_id)}

    def symbol(qualified: str):
        (found,) = [s for s in symbols.values() if s.qualified_name == qualified]
        return found

    def call(source: str, name: str):
        found = [r for r in refs if r.reference_kind == ReferenceKind.CALL and symbols[r.source_symbol_id].qualified_name == source and r.target_name == name]
        assert found, (source, name)
        return sorted(found, key=lambda r: r.source_location.start_line)[0]  # 同じ呼び出しが複数あっても、解決の結果は同じ

    def target(reference) -> str | None:
        return symbols[reference.target_symbol_id].qualified_name if reference.target_symbol_id else None

    return symbols, refs, deps, files, symbol, call, target, result


def test_extracts_packages_types_methods_and_docs(view) -> None:
    _, _, _, _, symbol, _, _, result = view
    assert result.status.value in ("success", "partial") and not result.errors
    assert symbol(f"{MODULE}.main").kind == SymbolKind.FUNCTION
    assert symbol(f"{MODULE}/store.Repository").kind == SymbolKind.INTERFACE
    assert symbol(f"{MODULE}/store.Memory").kind == SymbolKind.STRUCT
    assert symbol(f"{MODULE}/store.Memory.Save").kind == SymbolKind.METHOD  # レシーバーの型で修飾する
    assert symbol(f"{MODULE}/store.Limit").kind == SymbolKind.GLOBAL_VARIABLE
    assert symbol(f"{MODULE}/store.NewMemory").summary.startswith("NewMemory は")  # ドキュメントコメントの先頭行
    assert symbol(f"{MODULE}/svc.Service").base_classes == ("Base",)  # 埋め込み


def test_calls_resolve_by_package_and_by_declared_receiver_type(view) -> None:
    _, _, _, _, _, call, target, _ = view
    new_memory = call(f"{MODULE}.main", "store.NewMemory")
    assert new_memory.resolution_status == ResolutionStatus.RESOLVED and new_memory.confidence == Confidence.CONFIRMED
    assert target(new_memory) == f"{MODULE}/store.NewMemory"
    copy_items = call(f"{MODULE}/store.Memory.List", "m.copyItems")  # レシーバー m の型 Memory から、同じ型のメソッドへ
    assert target(copy_items) == f"{MODULE}/store.Memory.copyItems" and copy_items.confidence == Confidence.CONFIRMED
    place = call(f"{MODULE}.main", "service.Place")  # service := &svc.Service{} の型から
    assert target(place) == f"{MODULE}/svc.Service.Place"
    count = call(f"{MODULE}.main", "count")
    assert target(count) == f"{MODULE}.count"


def test_interface_calls_resolve_to_the_interface_method_as_inferred(view) -> None:
    symbols, _, _, _, symbol, call, target, _ = view
    assert symbol(f"{MODULE}/store.Repository.Save").kind == SymbolKind.FUNCTION_DECLARATION  # インターフェースのメソッドは、宣言として抽出する
    save = call(f"{MODULE}/svc.Service.Place", "Save")  # s.Repo.Save(): s は Service、フィールド Repo は Repository（インターフェース）
    assert save.resolution_status == ResolutionStatus.RESOLVED and save.confidence == Confidence.INFERRED
    assert target(save) == f"{MODULE}/store.Repository.Save" and "インターフェース" in save.note  # 実装（Memory.Save）は、実行時に決まる
    listing = call(f"{MODULE}.count", "r.List")  # r は Repository
    assert listing.confidence == Confidence.INFERRED and target(listing) == f"{MODULE}/store.Repository.List"


def test_struct_fields_are_symbols_with_their_types_and_embedded_methods_are_inferred(view) -> None:
    _, _, _, _, symbol, call, target, _ = view
    field = symbol(f"{MODULE}/svc.Service.Repo")
    assert field.kind == SymbolKind.CLASS_VARIABLE and field.summary == f"型: {MODULE}/store.Repository"
    log = call(f"{MODULE}/svc.Service.Place", "s.Log")  # Service 自身には Log が無く、埋め込みの Base から引き継ぐ
    assert log.confidence == Confidence.INFERRED and target(log) == f"{MODULE}/svc.Base.Log"


def test_builtin_and_external_calls_are_external(view) -> None:
    _, _, _, _, _, call, _, _ = view
    assert call(f"{MODULE}.main", "fmt.Println").resolution_status == ResolutionStatus.EXTERNAL
    assert call(f"{MODULE}.count", "len").resolution_status == ResolutionStatus.EXTERNAL and "組み込み" in call(f"{MODULE}.count", "len").note


def test_embedding_is_resolved_as_inheritance(view) -> None:
    symbols, refs, _, _, _, _, target, _ = view
    inheritance = [r for r in refs if r.reference_kind == ReferenceKind.INHERITANCE]
    assert [(symbols[r.source_symbol_id].qualified_name, target(r)) for r in inheritance] == [(f"{MODULE}/svc.Service", f"{MODULE}/svc.Base")]


def test_syntax_error_is_recorded_as_a_failed_file(analyzed, tmp_path: Path) -> None:
    root = tmp_path / "demo"
    shutil.copytree(SAMPLE, root)
    repo, project, result = analyzed(root)
    failed = [f for f in repo.list_source_files(project.project_id) if f.analysis_status.value == "failed"]
    assert [f.relative_path for f in failed] == ["broken.go"]  # 解析に失敗したファイルを、正常として扱わない
    assert result.status.value == "partial"

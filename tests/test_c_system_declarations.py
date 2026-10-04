from __future__ import annotations

from pathlib import Path

import pytest

from codeinsight.domain import ReferenceKind, ResolutionStatus, SymbolKind

FILES = {
    "a.c": '#include <stdlib.h>\n#include <string.h>\nextern void free(void *);\n\nint use(char *p) {\n    free(p);\n    return (int)strlen(p);\n}\n',
    "replacement.c": '#include <string.h>\n\nint strncmp(const char *a, const char *b, size_t n) {\n    (void)a; (void)b; (void)n;\n    return 0;\n}\n\nint compare(const char *a) {\n    return strncmp(a, "x", 1);\n}\n',
    "decl_only.c": "int mylib_call(int);\n\nint wrapper(void) {\n    return mylib_call(1);\n}\n",
}


@pytest.fixture
def view(analyzed, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    for name, text in FILES.items():
        (root / name).write_text(text, encoding="utf-8")
    repo, project, _ = analyzed(root)
    symbols = {s.symbol_id: s for s in repo.list_symbols_for_project(project.project_id)}
    refs = repo.list_references_for_project(project.project_id)

    def call(source: str, name: str):
        (found,) = [r for r in refs if r.reference_kind == ReferenceKind.CALL and symbols[r.source_symbol_id].name == source and r.target_name == name]
        return found

    return symbols, call


def test_functions_declared_in_system_headers_are_external_even_with_a_local_prototype(view) -> None:
    _, call = view
    for name in ("free", "strlen"):
        reference = call("use", name)
        assert reference.resolution_status == ResolutionStatus.EXTERNAL, name  # `.c` 内の再宣言（free）を、解決先にしない
        assert reference.target_symbol_id is None and "システムヘッダー" in reference.note


def test_project_definition_with_the_name_of_a_system_function_is_ambiguous(view) -> None:
    _, call = view
    reference = call("compare", "strncmp")
    assert reference.resolution_status == ResolutionStatus.AMBIGUOUS  # 代替実装。どちらが使われるかは、ビルドの構成による
    assert reference.target_symbol_id is None and "代替実装" in reference.note


def test_declaration_only_functions_without_system_evidence_still_resolve_to_the_declaration(view) -> None:
    symbols, call = view
    reference = call("wrapper", "mylib_call")
    assert reference.resolution_status == ResolutionStatus.RESOLVED
    assert symbols[reference.target_symbol_id].kind == SymbolKind.FUNCTION_DECLARATION and "宣言のみ" in reference.note


def test_standard_library_prototypes_without_system_headers_are_external_by_name(analyzed, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "noinc.c").write_text("extern void *malloc (unsigned long);\nextern int mylib_call (int);\n\nint use(void) {\n    malloc(8);\n    return mylib_call(1);\n}\n", encoding="utf-8")
    repo, project, _ = analyzed(root)
    symbols = {s.symbol_id: s for s in repo.list_symbols_for_project(project.project_id)}
    refs = {r.target_name: r for r in repo.list_references_for_project(project.project_id) if r.reference_kind == ReferenceKind.CALL}
    assert refs["malloc"].resolution_status == ResolutionStatus.EXTERNAL and "名前による判定" in refs["malloc"].note  # システムヘッダーが無くても、標準関数名なら外部
    assert refs["mylib_call"].resolution_status == ResolutionStatus.RESOLVED  # 標準関数名でない宣言のみの関数は、従来どおり宣言に解決
    assert symbols[refs["mylib_call"].target_symbol_id].kind == SymbolKind.FUNCTION_DECLARATION

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codeinsight.analysis.call_graph import Direction
from codeinsight.application import (
    AmbiguousSymbolError,
    MatchMode,
    NavigationService,
    SearchService,
    SymbolNotFoundError,
)
from codeinsight.domain import FileFreshness, ReferenceKind, ResolutionStatus, SymbolKind


def _c(analyzed, c_callgraph_dir):
    repo, project, _ = analyzed(c_callgraph_dir)
    nav = NavigationService(repo)
    return repo, project, nav, nav.load_index(project)


def test_search_symbols_modes_kinds_and_file(analyzed, c_callgraph_dir: Path) -> None:
    repo, project, _ = analyzed(c_callgraph_dir)
    search = SearchService(repo)

    exact = search.search_symbols(project, "fib", match=MatchMode.EXACT)
    assert {(h.path, h.symbol.kind) for h in exact} == {
        ("ops.h", SymbolKind.FUNCTION_DECLARATION),
        ("ops.c", SymbolKind.FUNCTION),
    }
    prefix = search.search_symbols(project, "ap", match=MatchMode.PREFIX)
    assert {h.symbol.name for h in prefix} == {"apply"}
    only_defs = search.search_symbols(project, "fib", kinds=[SymbolKind.FUNCTION])
    assert [h.path for h in only_defs] == ["ops.c"]
    in_file = search.search_symbols(project, "a", file="ops.h")
    assert in_file and all(h.path == "ops.h" for h in in_file)


def test_search_files_and_text_report_staleness(
    analyzed, c_callgraph_dir: Path, tmp_path: Path
) -> None:
    root = tmp_path / "copy"
    shutil.copytree(c_callgraph_dir, root)
    repo, project, _ = analyzed(root)
    search = SearchService(repo)

    assert [f.relative_path for f in search.search_files(project, "OPS")] == ["ops.c", "ops.h"]

    hits = search.search_text(project, "fib(")
    assert hits and all(h.freshness == FileFreshness.FRESH for h in hits)

    (root / "ops.c").write_text((root / "ops.c").read_text() + "\n// fib( changed\n")
    stale = [h for h in search.search_text(project, "fib(") if h.path == "ops.c"]
    assert stale and all(h.freshness == FileFreshness.STALE for h in stale)

    with pytest.raises(Exception):
        search.search_text(project, "(", regex=True)


def test_resolve_symbol_prefers_definition_and_reports_ambiguity(
    analyzed, c_callgraph_dir: Path, python_sample_dir: Path
) -> None:
    _, _, nav, index = _c(analyzed, c_callgraph_dir)
    assert nav.resolve_symbol(index, "add").symbol.kind == SymbolKind.FUNCTION
    with pytest.raises(SymbolNotFoundError):
        nav.resolve_symbol(index, "no_such_function")

    repo, project, _ = analyzed(python_sample_dir)
    py_nav = NavigationService(repo)
    py_index = py_nav.load_index(project)
    assert py_nav.resolve_symbol(py_index, "shapes.Circle.area").symbol.name == "area"
    with pytest.raises(AmbiguousSymbolError) as caught:
        py_nav.resolve_symbol(py_index, "area")
    assert len(caught.value.candidates) == 2


def test_callers_and_callees(analyzed, c_callgraph_dir: Path) -> None:
    _, _, nav, index = _c(analyzed, c_callgraph_dir)
    fib = nav.resolve_symbol(index, "fib").symbol
    main = nav.resolve_symbol(index, "main").symbol

    callers = nav.callers(index, fib)
    assert {h.source.name for h in callers} == {"main", "fib"}

    callees = nav.callees(index, main)
    by_name = {h.reference.target_name: h for h in callees}
    assert by_name["apply"].reference.resolution_status == ResolutionStatus.RESOLVED
    assert by_name["op"].reference.resolution_status == ResolutionStatus.UNRESOLVED
    assert by_name["printf"].reference.resolution_status == ResolutionStatus.EXTERNAL


def test_references_to_symbol(analyzed, c_callgraph_dir: Path) -> None:
    _, _, nav, index = _c(analyzed, c_callgraph_dir)
    add = nav.resolve_symbol(index, "add").symbol
    hits = nav.references_to(index, add)
    assert [(h.path, h.reference.reference_kind, h.source.name) for h in hits] == [
        ("main.c", ReferenceKind.FUNCTION_REF, "main")
    ]


def test_call_hierarchy_marks_recursion_depth_limit_and_unresolved(
    analyzed, c_callgraph_dir: Path
) -> None:
    _, _, nav, index = _c(analyzed, c_callgraph_dir)
    fib = nav.resolve_symbol(index, "fib").symbol
    main = nav.resolve_symbol(index, "main").symbol

    tree = nav.call_hierarchy(index, fib, Direction.CALLEES, max_depth=3)
    assert [c.label for c in tree.children] == ["fib", "fib"]
    assert all(child.recursive and not child.children for child in tree.children)

    tree = nav.call_hierarchy(index, main, Direction.CALLEES, max_depth=1)
    children = {c.label: c for c in tree.children}
    assert children["op"].symbol is None  # 関数ポインタ呼び出しは未解決の葉
    assert children["printf"].symbol is None
    assert children["apply"].truncated  # 深さ1で打ち切られ、さらに呼び出しがある

    callers_tree = nav.call_hierarchy(index, fib, Direction.CALLERS, max_depth=2)
    assert {c.label for c in callers_tree.children} == {"main", "fib"}


def test_call_paths(analyzed, c_callgraph_dir: Path) -> None:
    _, _, nav, index = _c(analyzed, c_callgraph_dir)
    main = nav.resolve_symbol(index, "main").symbol
    fib = nav.resolve_symbol(index, "fib").symbol
    helper = nav.resolve_symbol(index, "helper").symbol

    paths = nav.call_paths(index, main, fib)
    assert len(paths) == 1
    assert [h.reference.target_name for h in paths[0]] == ["fib"]
    assert nav.call_paths(index, helper, fib) == []  # 関数ポインタ経由は確定した辺に含めない


def test_file_dependencies_dependents_and_cycles(
    analyzed, c_callgraph_dir: Path, python_pkg_dir: Path
) -> None:
    _, _, nav, index = _c(analyzed, c_callgraph_dir)
    deps = nav.dependencies(index, "main.c")
    assert {(h.dependency.target_name, h.target_path) for h in deps} == {
        ("stdio.h", None),
        ("ops.h", "ops.h"),
    }
    dependents = nav.dependencies(index, "ops.h", dependents=True)
    assert {h.source_path for h in dependents} == {"main.c", "ops.c"}
    assert nav.dependency_cycles(index) == []

    repo, project, _ = analyzed(python_pkg_dir)
    py_nav = NavigationService(repo)
    cycles = py_nav.dependency_cycles(py_nav.load_index(project))
    assert any({"app/core.py", "app/plugin.py"} <= set(cycle) for cycle in cycles)


def test_show_source_with_context_and_staleness(
    analyzed, c_callgraph_dir: Path, tmp_path: Path
) -> None:
    root = tmp_path / "copy"
    shutil.copytree(c_callgraph_dir, root)
    repo, project, _ = analyzed(root)
    nav = NavigationService(repo)
    index = nav.load_index(project)

    view = nav.show_source(project, index, "ops.c", start=3, end=5, context=1)
    assert view.freshness == FileFreshness.FRESH
    assert [n for n, _ in view.lines] == [2, 3, 4, 5, 6]
    assert (view.highlight_start, view.highlight_end) == (3, 5)

    (root / "ops.c").write_text("// 変更\n" + (root / "ops.c").read_text())
    assert nav.show_source(project, index, "ops.c", 1, 1).freshness == FileFreshness.STALE

    with pytest.raises(SymbolNotFoundError):
        nav.show_source(project, index, "../secret.txt")


def test_unresolved_references_are_listed_separately_from_external(
    analyzed, c_callgraph_dir: Path
) -> None:
    _, _, nav, index = _c(analyzed, c_callgraph_dir)
    unresolved = nav.unresolved_references(index)
    assert {h.reference.target_name for h in unresolved} == {"op"}

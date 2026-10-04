from __future__ import annotations

from pathlib import Path

import pytest

from codeinsight.domain import Confidence, ReferenceKind, ResolutionStatus, SymbolKind

SOURCE = '''\
def helper():
    return 1


def outer(items):
    def add(x):
        return helper() + x

    def walk(node, depth):
        if depth:
            walk(node, depth - 1)
        return add(node)

    def callback(value):
        return helper()

    if items:
        def variant():
            return 1
    else:
        def variant():
            return 2

    total = walk(items, 3)
    items.sort(key=callback)
    return variant() + total


class Worker:
    def run(self):
        return 1


class Factory:
    def make(self) -> Worker:
        return Worker()

    def maybe(self) -> Worker | None:
        return None

    def use(self):
        worker = self.make()
        worker.run()
        self.make().run()
        self.maybe().run()
'''


@pytest.fixture
def view(analyzed, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(SOURCE, encoding="utf-8")
    repo, project, _ = analyzed(root)
    symbols = {s.symbol_id: s for s in repo.list_symbols_for_project(project.project_id)}
    refs = repo.list_references_for_project(project.project_id)

    def find(source: str, target_name: str, kind: ReferenceKind = ReferenceKind.CALL):
        return [r for r in refs if r.reference_kind == kind and symbols[r.source_symbol_id].qualified_name == source and r.target_name == target_name]

    def target(reference) -> str | None:
        return symbols[reference.target_symbol_id].qualified_name if reference.target_symbol_id else None

    return symbols, find, target


def test_nested_functions_are_extracted_as_symbols(view) -> None:
    symbols, _, _ = view
    names = {s.qualified_name: s for s in symbols.values()}
    assert names["mod.outer.add"].kind == SymbolKind.FUNCTION
    assert names["mod.outer.walk"].parent_symbol_id == names["mod.outer"].symbol_id


def test_direct_calls_to_nested_functions_are_resolved_and_attributed_to_the_caller(view) -> None:
    _, find, target = view
    (call_add,) = find("mod.outer.walk", "add")
    assert call_add.resolution_status == ResolutionStatus.RESOLVED and call_add.confidence == Confidence.CONFIRMED
    assert target(call_add) == "mod.outer.add"
    (recursion,) = find("mod.outer.walk", "walk")  # 自分自身の呼び出し（再帰）
    assert target(recursion) == "mod.outer.walk"
    (helper,) = find("mod.outer.add", "helper")  # ネストした関数の本体の呼び出しは、その関数のもの
    assert target(helper) == "mod.helper"
    (walk,) = find("mod.outer", "walk")
    assert target(walk) == "mod.outer.walk"


def test_nested_function_used_as_callback_keeps_an_inferred_edge(view) -> None:
    _, find, target = view
    (edge,) = find("mod.outer", "callback")  # コールバックとして渡され、直接は呼ばれない
    assert edge.resolution_status == ResolutionStatus.RESOLVED and edge.confidence == Confidence.INFERRED
    assert target(edge) == "mod.outer.callback"
    assert not find("mod.outer", "add")  # 直接呼ばれている add には、定義位置からの辺を足さない（add は walk から呼ばれる）


def test_conditional_nested_definitions_are_ambiguous(view) -> None:
    _, find, _ = view
    (call,) = find("mod.outer", "variant")
    assert call.resolution_status == ResolutionStatus.AMBIGUOUS  # どちらの定義が使われるかは静的に決まらない


def test_return_annotations_type_the_result_of_self_method_calls(view) -> None:
    _, find, target = view
    runs = find("mod.Factory.use", "worker.run") + find("mod.Factory.use", "self.make().run") + find("mod.Factory.use", "self.maybe().run")
    assert len(runs) == 3
    for run in runs:
        assert run.resolution_status == ResolutionStatus.RESOLVED and run.confidence == Confidence.INFERRED
        assert target(run) == "mod.Worker.run"  # 戻り値の型注釈（`X | None` を含む）から、型を推定

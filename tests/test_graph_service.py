from __future__ import annotations

from pathlib import Path

import pytest

from codeinsight.application import NavigationService
from codeinsight.application.graph_builder import Traversal
from codeinsight.application.graph_service import GraphRequest, GraphRequestError, GraphService


@pytest.fixture
def env(analyzed, c_callgraph_dir: Path):
    repo, project, _ = analyzed(c_callgraph_dir)
    navigation = NavigationService(repo)
    index = navigation.load_index(project)
    return GraphService(navigation), project, index


def test_builds_each_kind_from_a_request(env) -> None:
    service, project, index = env
    apply_symbol = next(s for s in index.symbols.values() if s.name == "apply")
    call = service.build(project, index, GraphRequest("call", apply_symbol, depth=1, direction=Traversal.IN))
    assert {n.label for n in call.nodes} == {"apply", "main"} and call.focus == apply_symbol.symbol_id
    assert service.build(project, index, GraphRequest("deps")).graph_kind == "file_dependency"
    flow = service.build(project, index, GraphRequest("flow", apply_symbol))
    assert flow.graph_kind == "flow" and flow.meta["function"] == "apply"  # Cの関数は、Clang ASTから制御フロー図にする


def test_invalid_requests_raise_a_request_error(env) -> None:
    service, project, index = env
    with pytest.raises(GraphRequestError):
        service.build(project, index, GraphRequest("flow"))  # 起点が必要
    with pytest.raises(GraphRequestError):
        service.build(project, index, GraphRequest("deps", root_path="no/such.c"))
    with pytest.raises(GraphRequestError):
        service.build(project, index, GraphRequest("unknown"))


def test_annotate_marks_stale_files(env) -> None:
    service, project, index = env
    model = service.annotate(service.build(project, index, GraphRequest("deps")), project, ["a.c"])
    assert model.meta["stale_files"] == ["a.c"] and any("古い可能性" in n for n in model.notes)

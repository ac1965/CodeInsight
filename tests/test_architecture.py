from __future__ import annotations

from pathlib import Path

from codeinsight.application import NavigationService
from codeinsight.application.architecture_service import ArchitectureService
from codeinsight.application.external_service import (
    ExternalService,
    categorize,
    categorize_c_function,
)
from codeinsight.application.graph_builder import EdgeStyle, GraphBuilder
from codeinsight.cli import main
from codeinsight.domain import Language
from codeinsight.presentation import to_mermaid


def _setup(analyzed, root: Path):
    repo, project, _ = analyzed(root)
    nav = NavigationService(repo)
    return nav, nav.load_index(project).materialize()


def test_components_roles_and_layer_levels(analyzed, layered_dir: Path) -> None:
    nav, index = _setup(analyzed, layered_dir)
    architecture = ArchitectureService().build(index, depth=2)
    components = architecture.components

    assert {"app/web", "app/service", "app/domain", "app/repository", "app/infra", "app/util", "tests"} <= set(components)
    assert components["app/web"].role == "presentation"
    assert components["app/service"].role == "application"
    assert components["app/domain"].role == "domain"
    assert components["app/repository"].role == "persistence"
    assert components["app/infra"].role == "infrastructure"
    assert components["app/util"].role == "shared" and components["tests"].role == "test"

    # 依存の向きから求めた段: 他に依存されるだけのutilが最下位、依存だけするtestsが最上位
    assert components["app/util"].level == 0
    assert components["tests"].level > components["app/service"].level > components["app/util"].level
    service = components["app/service"]
    assert {"app/domain", "app/repository", "app/infra", "app/util"} <= service.depends_on
    assert service.imports["app/repository"] == 1 and service.calls["app/repository"] >= 1


def test_cycles_and_layer_violations_are_reported_as_candidates(analyzed, layered_dir: Path) -> None:
    nav, index = _setup(analyzed, layered_dir)
    architecture = ArchitectureService().build(index, depth=2)

    # service -> infra -> domain -> web -> service の循環（infra は型注釈のためdomainをimportしている）
    assert ["app/domain", "app/infra", "app/service", "app/web"] in architecture.cycles
    # ドメイン層がプレゼンテーション層に依存するのは候補として指摘する
    pairs = {(v.source, v.target) for v in architecture.violations}
    assert ("app/domain", "app/web") in pairs
    # infra -> domain（型注釈のためのimport）は、アーキテクチャの流儀によるため指摘しない
    assert ("app/infra", "app/domain") not in pairs


def test_architecture_graph_marks_suspect_edges_as_inferred(analyzed, layered_dir: Path) -> None:
    nav, index = _setup(analyzed, layered_dir)
    architecture = ArchitectureService().build(index, depth=2)
    model = GraphBuilder(index).architecture_graph(architecture)
    by_id = {n.node_id: n for n in model.nodes}

    suspect = next(e for e in model.edges if (e.source, e.target) == ("app/domain", "app/web"))
    assert suspect.style == EdgeStyle.INFERRED and "逆向き" in suspect.label
    normal = next(e for e in model.edges if (e.source, e.target) == ("app/service", "app/repository"))
    assert normal.style == EdgeStyle.CONFIRMED and "import 1" in normal.label
    assert "推定" in by_id["app/web"].label  # 役割は推定であることを示す
    mermaid = to_mermaid(model)
    assert "-.->" in mermaid and "⚠逆向き?" in mermaid


def test_external_connections_are_classified_by_category(analyzed, layered_dir: Path) -> None:
    nav, index = _setup(analyzed, layered_dir)
    report = ExternalService().report(index)
    grouped = report.by_category()

    assert {u.library for u in grouped["network"]} >= {"requests", "smtplib", "requests.post"}
    assert {"sqlite3", "sqlite3.connect"} <= {u.library for u in grouped["database"]}
    assert "json.dump" in {u.library for u in grouped["persistence"]}
    assert "open" in {u.library for u in grouped["filesystem"]}  # 組み込みのopen
    use = next(u for u in grouped["database"] if u.library == "sqlite3.connect")
    assert use.owner == "app.repository.store.save" and use.path == "app/repository/store.py"


def test_effects_follow_resolved_calls_and_show_the_route(analyzed, layered_dir: Path) -> None:
    nav, index = _setup(analyzed, layered_dir)
    service = ExternalService()
    place_order = nav.resolve_symbol(index, "app.service.orders.place_order").symbol
    summary = service.effects(index, service.report(index), place_order, depth=3)

    assert summary.direct == []  # place_order自身は外部へ直接アクセスしない
    reached = {(u.category, u.library, tuple(route)) for u, route in summary.reachable}
    assert ("database", "sqlite3.connect", ("app.service.orders.place_order", "app.repository.store.save")) in reached
    assert any(c == "network" and lib == "requests.post" for c, lib, _ in reached)
    assert any(c == "filesystem" and lib == "open" for c, lib, _ in reached)

    shallow = service.effects(index, service.report(index), place_order, depth=0)
    assert shallow.reachable == []  # 深さ0では呼び出し先へ入らない


def test_categorize_uses_longest_prefix_and_c_functions() -> None:
    assert categorize("os.environ.get", Language.PYTHON) == "config"
    assert categorize("os.path.join", Language.PYTHON) == "filesystem"
    assert categorize("os.system", Language.PYTHON) == "process"
    assert categorize("requests.get", Language.PYTHON) == "network"
    assert categorize("somelib.thing", Language.PYTHON) is None
    assert categorize("sys/socket.h", Language.C) == "network"
    assert categorize("openssl/ssl.h", Language.C) == "crypto"
    assert categorize_c_function("pthread_create") == "concurrency"
    assert categorize_c_function("fopen") == "filesystem" and categorize_c_function("my_func") is None


def test_c_external_calls_are_classified(analyzed, c_callgraph_dir: Path) -> None:
    nav, index = _setup(analyzed, c_callgraph_dir)
    uses = ExternalService().report(index).by_category()
    assert any(u.library == "printf" for u in uses["logging"])
    assert {u.library for u in uses["filesystem"]} == {"stdio.h"}  # stdio.h のinclude


def test_cli_architecture_externals_effects_and_graph(tmp_path: Path, layered_dir: Path, capsys) -> None:
    db = str(tmp_path / "a.sqlite")
    main(["analyze", str(layered_dir), "--db", db])
    capsys.readouterr()

    def run(*argv: str):
        code = main([*argv, "--db", db])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    code, out, err = run("architecture", "--depth", "2", "--exclude", "tests/*")
    assert code == 0
    assert "app/web" in out and "プレゼンテーション/API層・推定" in out
    assert "app/domain → app/web" not in out and "app/domain[ドメイン/ビジネスロジック層] → app/web[プレゼンテーション/API層]" in out
    assert "循環する依存" in out and "名前による推定" in err

    code, out, _ = run("externals", "--category", "database")
    assert "データベース" in out and "sqlite3.connect" in out and "ネットワーク" not in out

    code, out, err = run("effects", "app.service.orders.place_order")
    assert code == 0 and "sqlite3.connect" in out and "経路:" in out and "app.repository.store.save" in out

    code, out, _ = run("graph", "arch", "--depth", "2", "--format", "mermaid")
    assert code == 0 and "⚠逆向き?" in out

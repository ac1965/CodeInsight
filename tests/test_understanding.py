from __future__ import annotations

import os
import subprocess
from pathlib import Path

from codeinsight.application import NavigationService
from codeinsight.application.external_service import ExternalService, classify_operation
from codeinsight.application.history_service import HistoryService
from codeinsight.application.impact_service import ImpactService
from codeinsight.application.test_map_service import TestMapService
from codeinsight.application.understand_service import UnderstandService
from codeinsight.application.unused_service import UnusedService
from codeinsight.cli import main
from codeinsight.domain import Language


def _git(root: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Alice", "GIT_AUTHOR_EMAIL": "a@example.com",
           "GIT_COMMITTER_NAME": "Alice", "GIT_COMMITTER_EMAIL": "a@example.com"}
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def _setup(analyzed, root: Path):
    repo, project, _ = analyzed(root)
    nav = NavigationService(repo)
    return project, nav, nav.load_index(project).materialize()


def _symbol(nav, index, name):
    return nav.resolve_symbol(index, name).symbol


# --- 操作の分類（外部への入出力か、純粋な計算か） ---


def test_operations_are_classified_into_read_write_effect_output_and_pure() -> None:
    assert classify_operation("pathlib.Path") == "pure" and classify_operation("pathlib.Path.with_suffix") == "pure"
    assert classify_operation("urllib.parse.urljoin") == "pure" and classify_operation("json.loads") == "pure"
    assert classify_operation("requests.RequestException") == "pure"  # 例外クラスは外部への操作ではない
    assert classify_operation("pathlib.Path.read_text") == "read" and classify_operation("os.getenv") == "read"
    assert classify_operation("pathlib.Path.write_text") == "write" and classify_operation("shutil.rmtree") == "write"
    assert classify_operation("requests.get") == "effect" and classify_operation("subprocess.run") == "effect"
    assert classify_operation("requests.Session.post") == "effect"
    assert classify_operation("print") == "output" and classify_operation("logging.info") == "output"
    assert classify_operation("open") == "io" and classify_operation("somelib.thing") == "call"


def test_effects_exclude_pure_operations_and_exception_classes(analyzed, tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "m.py").write_text(
        "import json\nimport requests\nfrom pathlib import Path\nfrom urllib.parse import urljoin\n\n\n"
        "def work(url):\n    target = Path('x').with_suffix('.txt')\n    full = urljoin(url, 'a')\n    data = json.loads('{}')\n"
        "    try:\n        requests.get(full)\n    except requests.RequestException:\n        print('failed')\n"
        "    with open(target, 'w') as handle:\n        handle.write(str(data))\n"
    )
    project, nav, index = _setup(analyzed, root)
    service = ExternalService()
    report = service.report(index)
    libraries = {u.library for u in report.uses if u.kind == "call"}
    assert not libraries & {"pathlib.Path", "urllib.parse.urljoin", "json.loads", "requests.RequestException"}

    summary = service.effects(index, report, _symbol(nav, index, "m.work"))
    assert {u.library for u in summary.direct} == {"requests.get", "print", "open"}


# --- 未使用コードの候補 ---


def test_unused_candidates_carry_a_confidence_and_a_reason(analyzed, tmp_path: Path) -> None:
    root = tmp_path / "p"
    (root / "tests").mkdir(parents=True)
    (root / "lib.py").write_text(
        "import functools\n\n\ndef used():\n    return 1\n\n\ndef _private_unused():\n    return 2\n\n\n"
        "def public_unused():\n    return 3\n\n\n@functools.lru_cache\ndef decorated_unused():\n    return 4\n\n\n"
        "def only_tests():\n    return 5\n\n\nclass Base:\n    def hook(self):\n        return 0\n\n\n"
        "class Child(Base):\n    def hook(self):\n        return 1\n\n\ndef main():\n    return used()\n"
    )
    (root / "tests" / "test_lib.py").write_text("from lib import only_tests\n\n\ndef test_x():\n    assert only_tests()\n")
    project, nav, index = _setup(analyzed, root)
    found = {c.symbol.qualified_name: c for c in UnusedService().candidates(index)}

    assert "lib.used" not in found and "lib.main" not in found
    assert found["lib._private_unused"].confidence == "high"
    assert found["lib.public_unused"].confidence == "medium" and "公開API" in found["lib.public_unused"].reason
    assert found["lib.decorated_unused"].confidence == "low" and "デコレータ" in found["lib.decorated_unused"].reason
    assert found["lib.only_tests"].confidence == "low" and "テストからのみ" in found["lib.only_tests"].reason
    assert found["lib.Child.hook"].confidence == "low" and "多態" in found["lib.Child.hook"].reason


# --- テストとの対応・影響範囲 ---


def test_test_mapping_distinguishes_direct_and_indirect_reach(analyzed, layered_dir: Path) -> None:
    project, nav, index = _setup(analyzed, layered_dir)
    service = TestMapService()

    direct = service.tests_for(index, _symbol(nav, index, "app.service.orders.place_order"))
    assert [(r.test.qualified_name, r.direct) for r in direct.reaches] == [("tests.test_orders.test_place_order", True)]
    assert direct.test_files_importing == ["tests/test_orders.py"]

    indirect = service.tests_for(index, _symbol(nav, index, "app.util.helpers.slug"))
    (reach,) = indirect.reaches
    assert not reach.direct and reach.route == ["app.service.orders.place_order", "app.util.helpers.slug"]
    assert not service.tests_for(index, _symbol(nav, index, "app.util.helpers.slug"), depth=1).reaches  # 深さ1では届かない

    untested = {s.qualified_name for s in service.untested(index)}
    assert "app.web.routes.handle" in untested and "app.service.orders.place_order" not in untested
    assert "app.util.helpers.slug" not in untested


def test_impact_follows_callers_to_entry_points_and_tests(analyzed, layered_dir: Path) -> None:
    project, nav, index = _setup(analyzed, layered_dir)
    slug = _symbol(nav, index, "app.util.helpers.slug")
    handle = _symbol(nav, index, "app.web.routes.handle")
    report = ImpactService().impact(index, slug, depth=4, entry_symbol_ids={handle.symbol_id})

    distances = {a.symbol.qualified_name: a.distance for a in report.affected}
    assert distances["app.service.orders.place_order"] == 1
    assert distances["app.web.routes.handle"] == 2 and distances["tests.test_orders.test_place_order"] == 2
    assert [a.symbol.qualified_name for a in report.entry_points] == ["app.web.routes.handle"]
    assert report.entry_points[0].route == ["app.web.routes.handle", "app.service.orders.place_order", "app.util.helpers.slug"]
    assert {"app/service", "app/web"} <= {c.rsplit("/", 0)[0] for c in report.components}

    shallow = ImpactService().impact(index, slug, depth=1)
    assert {a.symbol.qualified_name for a in shallow.affected} == {"app.service.orders.place_order"}


# --- Git履歴 ---


def _git_project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    (root / "util.py").write_text("X = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "feat: 計算関数を追加")
    (root / "calc.py").write_text("def add(a, b):\n    # 型を厳密に扱う\n    return int(a) + int(b)\n\n\ndef sub(a, b):\n    return a - b\n")
    (root / "util.py").write_text("X = 2\n")
    _git(root, "commit", "-q", "-am", "fix: addで文字列を受け取れるようにする")
    (root / "util.py").write_text("X = 3\n")
    (root / "calc.py").write_text((root / "calc.py").read_text() + "\n\n# memo\n")
    _git(root, "commit", "-q", "-am", "chore: 調整")
    (root / "util.py").write_text("X = 4\n")
    (root / "calc.py").write_text((root / "calc.py").read_text() + "# memo2\n")
    _git(root, "commit", "-q", "-am", "chore: さらに調整")
    return root


def test_symbol_history_lists_commits_that_touched_the_function(analyzed, tmp_path: Path) -> None:
    root = _git_project(tmp_path)
    project, nav, index = _setup(analyzed, root)
    service = HistoryService()

    add = service.symbol_history(project, index, _symbol(nav, index, "calc.add"))
    assert [c.subject for c in add.commits] == ["fix: addで文字列を受け取れるようにする", "feat: 計算関数を追加"]
    assert add.introduced.subject == "feat: 計算関数を追加" and add.available and not add.modified_in_working_tree
    sub = service.symbol_history(project, index, _symbol(nav, index, "calc.sub"))
    assert [c.subject for c in sub.commits] == ["feat: 計算関数を追加"]  # subは最初のコミット以降変更されていない

    (root / "calc.py").write_text((root / "calc.py").read_text() + "# 未コミット\n")
    assert service.symbol_history(project, index, _symbol(nav, index, "calc.add")).modified_in_working_tree


def test_history_report_finds_churn_and_co_changed_files(analyzed, tmp_path: Path) -> None:
    root = _git_project(tmp_path)
    project, nav, index = _setup(analyzed, root)
    report = HistoryService().report(project, index, min_coupling=3)

    assert report.available and report.commits_examined == 4
    assert {c.path: c.commits for c in report.churn} == {"calc.py": 4, "util.py": 4}
    assert ("calc.py", "util.py", 4) in report.coupling


def test_history_is_unavailable_outside_git(analyzed, python_doc_dir: Path, tmp_path: Path) -> None:
    import shutil

    root = tmp_path / "nogit"
    shutil.copytree(python_doc_dir, root)
    project, nav, index = _setup(analyzed, root)
    history = HistoryService().symbol_history(project, index, _symbol(nav, index, "pkg.client.fetch"))
    assert not history.available and history.commits == []
    assert not HistoryService().report(project, index).available


# --- 読解カード（8つの問い） ---


def _understanding_project(tmp_path: Path) -> Path:
    root = _git_project(tmp_path)
    (root / "app.py").write_text(
        '"""アプリ本体。"""\nimport os\n\n\nclass Missing(Exception):\n    pass\n\n\n'
        "def lookup(table, key, default=None):\n    \"\"\"キーを検索する。\"\"\"\n    timeout = int(os.getenv('LOOKUP_TIMEOUT', '5'))\n"
        "    table.setdefault('hits', 0)\n    table['hits'] += 1\n    if key not in table:\n        raise Missing(key)\n"
        "    return table[key], timeout\n\n\n"
        "def safe(table):\n    try:\n        return lookup(table, 'a')\n    except Missing:\n        return None\n\n\n"
        "def risky(table):\n    return lookup(table, 'b')\n"
    )
    (root / "test_app.py").write_text("from app import safe\n\n\ndef test_safe_returns_none_for_missing_key():\n    assert safe({}) is None\n")
    (root / "README.md").write_text("# demo\n\n`lookup` はキーを検索する関数です。\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "feat: lookupを追加")
    return root


def test_understand_answers_the_eight_questions_with_evidence(analyzed, tmp_path: Path) -> None:
    root = _understanding_project(tmp_path)
    project, nav, index = _setup(analyzed, root)
    u = UnderstandService(nav).understand(project, index, _symbol(nav, index, "app.lookup"))

    # 1. なぜ存在するのか
    assert u.summary == "キーを検索する。" and u.module_summary == "アプリ本体。"
    assert ("README.md", 3) == u.doc_mentions[0][:2]
    assert u.history.introduced.subject == "feat: lookupを追加"
    # 2. 誰が呼ぶのか
    assert {h.source.qualified_name for h in u.callers} == {"app.safe", "app.risky"} and u.caller_total == 2
    # 3. 何を入力するのか
    assert [(p.name, p.default) for p in u.parameters] == [("table", ""), ("key", ""), ("default", "None")]
    assert set(u.caller_arguments["key"]) == {"'a'", "'b'"}
    assert [c.name for c in u.config_reads] == ["LOOKUP_TIMEOUT"]
    # 4. 何を変更するのか（引数のオブジェクトを書き換える）
    assert any("引数 table" in text and "setdefault" in text for text in u.parameter_mutations)
    assert any("引数 table" in text and "table['hits']" in text for text in u.parameter_mutations)
    # 5. 何を返すのか
    assert [text for _, text in u.returns] == ["(table[key], timeout)"]
    # 6. 誰に影響するのか
    assert {a.symbol.qualified_name for a in u.impact.affected} >= {"app.safe", "app.risky", "test_app.test_safe_returns_none_for_missing_key"}
    # 7. 失敗するとどうなるのか: Missing は safe が捕捉するが、risky は捕捉しない（呼び出し箇所2件中1件）
    assert {e.exception for e in u.exceptions.propagated} == {"Missing"}
    assert u.caught_by_callers["Missing"] == (1, 2)
    # 8. 履歴
    assert u.history.available and u.history.commits[-1].subject == "feat: lookupを追加"
    assert u.limitations == []


def test_understand_reports_what_it_cannot_cover(analyzed, c_callgraph_dir: Path) -> None:
    project, nav, index = _setup(analyzed, c_callgraph_dir)
    u = UnderstandService(nav).understand(project, index, _symbol(nav, index, "fib"))
    assert u.language == Language.C and u.parameters == [] and u.exceptions is None
    assert any("Pythonのみ対応" in text for text in u.limitations)
    assert {h.source.name for h in u.callers} == {"main", "fib"}  # 呼び出し元・影響は、Cでも得られる


# --- CLI ---


def test_cli_understand_history_tests_impact_unused(tmp_path: Path, capsys) -> None:
    root = _understanding_project(tmp_path)
    db = str(tmp_path / "u.sqlite")
    main(["analyze", str(root), "--db", db])
    capsys.readouterr()

    def run(*argv: str):
        code = main([*argv, "--db", db])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    code, out, _ = run("understand", "app.lookup")
    assert code == 0
    for heading in ("1. なぜ存在するのか", "2. 誰が呼ぶのか", "3. 何を入力するのか", "4. 何を変更するのか",
                    "5. 何を返すのか", "6. 誰に影響するのか", "7. 失敗するとどうなるのか", "8. なぜ現在の実装になっているのか"):
        assert heading in out
    assert "呼び出し箇所 2 件中 1 件が捕捉" in out and "引数 table を setdefault() で破壊的に変更する" in out
    assert "README.md:3" in out and "feat: lookupを追加" in out and "設計判断の理由は" in out

    code, out, _ = run("history", "app.lookup")
    assert "feat: lookupを追加" in out
    code, out, _ = run("history")
    assert "calc.py" in out and "同時に変更される" in out

    code, out, _ = run("tests", "app.safe")
    assert "test_safe_returns_none_for_missing_key" in out
    code, out, _ = run("tests", "--untested")
    assert "app.risky" in out and "app.safe\t" not in out

    code, out, _ = run("impact", "app.lookup")
    assert "app.safe" in out and "app.risky" in out and "関連するテスト" in out

    code, out, _ = run("unused", "--min-confidence", "high")
    assert code == 0 and "確度 高" not in out.replace("0件", "") or "calc.sub" in out


def test_reads_through_callees_are_reported_as_inputs_with_the_route(analyzed, tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "store.py").write_text(
        "class Store:\n    def __init__(self, path):\n        self.path = path\n\n"
        "    def load(self):\n        if not self.path.exists():\n            return {}\n        return self.path.read_text()\n\n"
        "    def add(self, key):\n        data = self.load()\n        self.path.write_text(str(data) + key)\n"
    )
    project, nav, index = _setup(analyzed, root)
    service = ExternalService()
    summary = service.effects(index, service.report(index), _symbol(nav, index, "store.Store.add"))

    assert {u.library for u in summary.direct} == {"self.path.write_text"}
    assert {(u.library, route[-1]) for u, route in summary.reads_reachable} == {
        ("self.path.exists", "store.Store.load"), ("self.path.read_text", "store.Store.load")
    }
    assert all(u.confidence == "inferred" for u, _ in summary.reads_reachable)  # メソッド名による推定


def test_environment_report_collects_declared_used_and_platform_facts(analyzed, tmp_path: Path) -> None:
    from codeinsight.application.environment_service import EnvironmentService

    root = tmp_path / "p"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.10"\ndependencies = ["requests>=2", "unusedlib"]\n'
        '[project.optional-dependencies]\ngui = ["PySide6"]\n'
    )
    (root / "m.py").write_text(
        "import subprocess\nimport sys\nimport shutil\n\nimport requests\nimport notdeclared\nimport PySide6.QtCore\n\n\n"
        "def run():\n    if sys.platform == 'darwin':\n        subprocess.run(['open', '-a', 'x'])\n"
        "    java = shutil.which('java')\n    return requests.get('http://x'), java, notdeclared\n"
    )
    project, nav, index = _setup(analyzed, root)
    report = EnvironmentService().report(project, index)

    assert report.python_requirement == ">=3.10"
    assert set(report.declared) == {"requests", "unusedlib", "PySide6"}
    assert set(report.used_external) == {"requests", "notdeclared", "PySide6"}  # 標準ライブラリは含まない
    assert report.undeclared == ["notdeclared"] and report.unused_declared == ["unusedlib"]
    assert "ネットワーク/HTTP" in report.frameworks and "GUI/イベント" in report.frameworks
    assert [(s.line, "darwin" in s.detail) for s in report.platform_checks] == [(11, True)]
    assert {s.detail for s in report.executables} == {"open", "shutil.which('java')"}


def test_docs_check_finds_mentions_missing_from_the_implementation(analyzed, tmp_path: Path) -> None:
    from codeinsight.application.spec_check_service import SpecCheckService

    root = tmp_path / "p"
    root.mkdir()
    (root / "app.py").write_text(
        "import argparse\nimport os\n\n\ndef compute():\n    return os.getenv('APP_LEVEL')\n\n\n"
        "def build():\n    parser = argparse.ArgumentParser()\n    parser.add_argument('--fast')\n    parser.add_argument('--hidden')\n"
        "    return parser\n"
    )
    (root / "README.md").write_text(
        "# 使い方\n\n`compute()` を呼び、`--fast` を付ける。`--removed-flag` と `APP_GONE_VAR` と `legacy_helper` は旧仕様。\n"
        "環境変数 `APP_LEVEL` も参照する。標準ライブラリの `os.getenv` は対象外。\n"
    )
    project, nav, index = _setup(analyzed, root)
    report = SpecCheckService().check(project, index)

    missing = {(m.kind, m.token) for m in report.missing_in_code}
    assert missing == {("option", "--removed-flag"), ("env", "APP_GONE_VAR"), ("identifier", "legacy_helper")}
    assert [name for name, _, _ in report.undocumented_options] == ["--hidden"]
    assert report.undocumented_env == []  # APP_LEVEL は文書にある
    assert report.documents == 1


def test_cli_environment_and_docs_check(tmp_path: Path, capsys) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "x"\nrequires-python = ">=3.11"\ndependencies = ["requests"]\n')
    (root / "m.py").write_text("import requests\n\n\ndef f():\n    return requests.get('u')\n")
    (root / "README.md").write_text("`f()` と `gone_function` を使う。\n")
    db = str(tmp_path / "e.sqlite")
    main(["analyze", str(root), "--db", db])
    capsys.readouterr()

    code = main(["environment", "--db", db])
    out = capsys.readouterr().out
    assert code == 0 and "Python: >=3.11" in out and "requests×1" in out and "ネットワーク/HTTP: requests" in out
    code = main(["docs-check", "--db", db])
    out = capsys.readouterr().out
    assert code == 0 and "gone_function" in out and "[識別子] f()" not in out


def test_docs_check_understands_boolean_option_pairs_and_ignores_file_names(analyzed, tmp_path: Path) -> None:
    from codeinsight.application.spec_check_service import SpecCheckService

    root = tmp_path / "p"
    root.mkdir()
    (root / "app.py").write_text(
        "import argparse\n\n\ndef build():\n    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--yoko', action=argparse.BooleanOptionalAction)\n    return parser\n"
    )
    (root / "README.md").write_text("`--yoko` / `--no-yoko` で切り替える。設定は `config.json` と `settings.yaml`。\n")
    project, nav, index = _setup(analyzed, root)
    report = SpecCheckService().check(project, index)
    assert report.missing_in_code == []

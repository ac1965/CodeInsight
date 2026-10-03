from __future__ import annotations

from pathlib import Path

from codeinsight.application import NavigationService
from codeinsight.application.boundary_service import BoundaryService
from codeinsight.application.config_service import ConfigService
from codeinsight.cli import main


def _setup(analyzed, root: Path):
    repo, project, _ = analyzed(root)
    nav = NavigationService(repo)
    return project, nav.load_index(project).materialize()


def _by(items, kind):
    return {i.name: i for i in items if i.kind == kind}


def test_environment_variables_with_defaults_and_uses(analyzed, python_boundary_dir: Path) -> None:
    project, index = _setup(analyzed, python_boundary_dir)
    items, skipped = ConfigService().scan(project, index)
    env = _by(items, "env")

    assert skipped == []
    assert env["APP_TIMEOUT"].default == "'10'" and env["APP_TIMEOUT"].owner == "app.cmd_run"
    lines = (python_boundary_dir / "app.py").read_text().splitlines()
    print_line = next(n for n, text in enumerate(lines, 1) if "print(args.verbose" in text)
    assert [u.line for u in env["APP_TIMEOUT"].uses] == [print_line]  # print(..., timeout, ...) で使われる
    assert env["APP_TOKEN"].detail.startswith("必須")  # os.environ[...] は未設定だとKeyError
    assert "APP_MODE" in _by(items, "env_write")


def test_cli_options_are_linked_to_the_attributes_that_read_them(analyzed, python_boundary_dir: Path) -> None:
    project, index = _setup(analyzed, python_boundary_dir)
    items, _ = ConfigService().scan(project, index)
    options = _by(items, "cli_option")

    verbose = options["--verbose, -v"]
    assert verbose.help == "詳細表示" and "action=" in verbose.detail
    assert [(u.owner, u.how) for u in verbose.uses] == [("app.cmd_run", "args.verbose を読む")]
    retries = options["--retries"]
    assert retries.default == "RETRIES" and "type=int" in retries.detail
    assert [u.owner for u in retries.uses] == ["app.cmd_run"]
    assert options["name"].uses  # 位置引数も args.name の読み取りに結び付く
    assert {"run", "serve"} <= set(_by(items, "cli_subcommand"))


def test_constants_and_config_files(analyzed, python_boundary_dir: Path) -> None:
    project, index = _setup(analyzed, python_boundary_dir)
    items, _ = ConfigService().scan(project, index)
    constants = _by(items, "constant")

    assert {"TIMEOUT", "RETRIES", "NAME"} <= set(constants) and "lowercase_value" not in constants
    assert {(u.owner, u.how) for u in constants["TIMEOUT"].uses} == {("app.cmd_serve", "参照")}
    assert constants["RETRIES"].default == "3"
    assert {u.owner for u in constants["RETRIES"].uses} >= {"app.cmd_serve", "app.build_parser"}
    assert constants["NAME"].uses == []  # どこからも参照されない設定値も、そのまま示す
    loader = _by(items, "config_file")["json.load"]
    assert loader.confidence == "inferred" and loader.owner == "app.cmd_serve"


def test_entry_points_and_cli_commands(analyzed, python_boundary_dir: Path) -> None:
    project, index = _setup(analyzed, python_boundary_dir)
    items, _ = BoundaryService().scan(project, index)
    entries = [i for i in items if i.kind == "entry"]

    assert any(i.label.startswith("コマンド `demo`") and i.target and i.target.qualified_name == "app.main" for i in entries)
    assert any("__main__ ブロック" in i.label and "main" in i.detail for i in entries)
    commands = {i.label: i for i in items if i.kind == "cli"}
    assert commands["サブコマンド `run`"].target.qualified_name == "app.cmd_run"
    assert commands["サブコマンド `serve`"].target.qualified_name == "app.cmd_serve"


def test_http_events_threads_async_and_cache(analyzed, python_boundary_dir: Path) -> None:
    project, index = _setup(analyzed, python_boundary_dir)
    items, _ = BoundaryService().scan(project, index)

    http = {i.label: i for i in items if i.kind == "http"}
    assert http["ROUTE /items methods=['GET', 'POST']"].target.qualified_name == "web.items"
    assert http["GET /health"].target.qualified_name == "web.health"
    assert any(i.label.startswith("GET ハンドラ") and i.owner == "web.Handler.do_GET" for i in items if i.kind == "http")

    events = [i for i in items if i.kind == "event"]
    assert any(i.target and i.target.qualified_name == "app.Worker._on_done" for i in events)  # signal.connect(self._on_done)
    assert any(i.detail == "ラムダ式" for i in events)
    assert any(i.target and i.target.qualified_name == "app.cmd_serve" and "atexit" in i.label for i in events)

    threads = [i for i in items if i.kind == "thread"]
    assert {i.target.qualified_name for i in threads if i.target} == {"app.Worker._loop"}
    assert len(threads) == 2  # Thread(target=...) と executor.submit(...)

    async_items = [i for i in items if i.kind == "async"]
    assert {"app.fetch", "app.runner"} <= {i.owner for i in async_items if i.label == "async 関数"} | {i.target.qualified_name for i in async_items if i.target}
    assert any("asyncio.gather" in i.label for i in async_items)

    cache = {i.owner: i for i in items if i.kind == "cache"}
    assert cache["app.cached_lookup"].confidence == "confirmed" and "lru_cache" in cache["app.cached_lookup"].label


def test_db_connect_is_not_mistaken_for_an_event_registration(analyzed, tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "m.py").write_text("import sqlite3\n\n\ndef f(path):\n    return sqlite3.connect(path)\n")
    project, index = _setup(analyzed, root)
    items, _ = BoundaryService().scan(project, index)
    assert [i for i in items if i.kind == "event"] == []


def test_cli_config_and_boundaries(tmp_path: Path, python_boundary_dir: Path, capsys) -> None:
    db = str(tmp_path / "c.sqlite")
    main(["analyze", str(python_boundary_dir), "--db", db])
    capsys.readouterr()

    def run(*argv: str):
        code = main([*argv, "--db", db])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    code, out, _ = run("config")
    assert code == 0
    assert "APP_TIMEOUT" in out and "既定値: '10'" in out and "args.verbose を読む" in out
    assert "NAME" in out and "使用箇所を確認できません" in out
    code, out, _ = run("config", "--kind", "env", "--name", "TOKEN")
    assert "APP_TOKEN" in out and "APP_TIMEOUT" not in out

    code, out, _ = run("boundaries", "--kind", "http")
    assert "GET /health" in out and "web.health" in out and "CLIコマンド" not in out
    code, out, _ = run("boundaries")
    assert "エントリポイント" in out and "サブコマンド `run`" in out and "→ app.cmd_run" in out


def test_environment_variable_used_as_an_option_default_is_linked_to_the_option(
    analyzed, tmp_path: Path
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "m.py").write_text(
        "import argparse\nimport os\n\n\ndef build():\n    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--jar', default=os.environ.get('APP_JAR'))\n    return parser\n"
    )
    project, index = _setup(analyzed, root)
    items, _ = ConfigService().scan(project, index)
    env = _by(items, "env")["APP_JAR"]
    assert [(u.line, u.how) for u in env.uses] == [(7, "オプション --jar の既定値として使用")]

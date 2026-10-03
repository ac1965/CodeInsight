from __future__ import annotations

import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codeinsight.ai.evaluation import _Workspace
from codeinsight.cli import main
from codeinsight.presentation.tui_model import TuiController, TuiModel, clip, display_width
from codeinsight.presentation.tui_view import compose

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def make_model():
    workspace = _Workspace()

    def build(name: str) -> TuiModel:
        _, project, index, navigation = workspace.get(str(FIXTURES / name))
        return TuiModel(navigation, project, index)

    yield build
    workspace.close()


def _text(controller: TuiController, height: int = 30, width: int = 110) -> str:
    return "\n".join(op.text for op in compose(controller, height, width))


def _open(controller: TuiController, label: str) -> None:
    """ツリー上で label のノードまで開いて移動する。"""

    model = controller.model
    for _ in range(200):
        labels = [r.node.label for r in model.rows]
        if label in labels:
            model.cursor = labels.index(label)
            return
        # まだ見えていないなら、閉じている最初のノードを開く
        for number, row in enumerate(model.rows):
            if row.expandable and not row.expanded and row.node.kind in ("directory", "file", "class"):
                model.cursor = number
                model.expand()
                break
        else:
            break
    raise AssertionError(f"{label} が見つからない")


def test_clip_and_display_width_handle_wide_characters() -> None:
    assert display_width("abc") == 3 and display_width("日本語") == 6
    assert clip("日本語テキスト", 7) == "日本語…" and display_width(clip("日本語テキスト", 7)) <= 7
    assert clip("short", 10) == "short" and clip("anything", 0) == ""


def test_tree_navigation_expand_collapse_and_parent(make_model) -> None:
    model = make_model("python_flow")
    controller = TuiController(model)
    assert model.rows[0].node.kind == "project" and model.rows[0].expanded
    controller.handle("down")
    assert model.current.kind in ("file", "directory")
    controller.handle("enter")  # 開く
    before = len(model.rows)
    controller.handle("left")  # 閉じる
    assert len(model.rows) < before or model.current.kind != "file"
    controller.handle("left")  # 親（プロジェクト）へ
    assert model.cursor == 0
    controller.handle("k")
    assert model.cursor == 0  # 範囲外に出ない


def test_relations_show_status_and_follow_resolved_targets_with_history(make_model) -> None:
    model = make_model("c_callgraph")
    controller = TuiController(model)
    _open(controller, "main")
    assert model.current_symbol is not None and model.current_symbol.name == "main"
    relations = model.relations
    callees = {r.label: r for r in relations if r.direction == "callee"}
    assert "fib" in callees and callees["fib"].target is not None
    assert any(r.status.startswith("未解決") for r in relations) or any(r.status.startswith("外部") for r in relations)  # 関数ポインタ・printf

    controller.handle("tab")
    assert controller.focus == "relations"
    model.relation_cursor = relations.index(callees["fib"])
    controller.handle("enter")
    assert controller.focus == "tree" and model.current_symbol.name == "fib"
    assert controller.handle("b") and model.current_symbol.name == "main"  # 戻る


def test_unresolved_or_external_relations_cannot_be_followed_and_say_why(make_model) -> None:
    model = make_model("c_callgraph")
    controller = TuiController(model)
    _open(controller, "main")
    blocked = [i for i, r in enumerate(model.relations) if r.target is None]
    assert blocked, "main には外部（printf）または未解決（関数ポインタ）の呼び出しがある"
    controller.handle("tab")
    model.relation_cursor = blocked[0]
    controller.handle("enter")
    assert "移動できません" in controller.message and controller.focus == "relations"


def test_search_jumps_to_the_symbol_and_reports_no_match(make_model) -> None:
    model = make_model("python_flow")
    controller = TuiController(model)
    controller.handle("/")
    assert controller.focus == "search"
    for char in "transform":
        controller.handle(char)
    controller.handle("backspace")
    controller.handle("m")
    controller.handle("enter")
    assert controller.focus == "results" and any(h.symbol.name == "transform" for h in controller.results)
    controller.result_cursor = [h.symbol.name for h in controller.results].index("transform")
    controller.handle("enter")
    assert controller.focus == "tree" and model.current_symbol.name == "transform"

    controller.handle("/")
    for char in "zzzz_no_such":
        controller.handle(char)
    controller.handle("enter")
    assert controller.focus == "tree" and "一致するシンボルはありません" in controller.message
    controller.handle("/")
    controller.handle("esc")
    assert controller.focus == "tree"


def test_quit_help_and_unknown_keys(make_model) -> None:
    controller = TuiController(make_model("python_flow"))
    assert controller.handle("zz") is True and controller.handle("?") is True and controller.show_help
    assert controller.handle("x") is True and not controller.show_help  # 何かのキーで閉じる
    assert controller.handle("q") is False


def test_compose_shows_structure_source_relations_and_the_static_note(make_model) -> None:
    model = make_model("c_callgraph")
    controller = TuiController(model)
    _open(controller, "fib")
    text = _text(controller)
    assert "構造" in text and "ソース" in text and "呼び出し関係" in text
    assert "fn fib" in text and "return fib(n - 1)" in text  # ソースの行
    assert "実行順序や実際に通る経路を示すものではありません" in text  # 静的な関係であることの注記
    assert "再帰" not in text  # 再帰は表示の対象外だが、自己呼び出しは関係として現れる
    assert any(op.style == "highlight" for op in compose(controller, 30, 110))  # 選択したシンボルの範囲を強調


def test_compose_marks_stale_files_and_small_terminals(make_model) -> None:
    model = make_model("c_callgraph")
    model.stale_paths = {"ops.c"}
    controller = TuiController(model)
    _open(controller, "fib")
    assert "解析後に変更されています" in _text(controller)
    small = compose(controller, 5, 30)
    assert len(small) == 1 and "小さすぎます" in small[0].text


def test_compose_never_draws_outside_the_screen(make_model) -> None:
    controller = TuiController(make_model("python_flow"))
    for height, width in ((12, 60), (24, 80), (40, 160)):
        for op in compose(controller, height, width):
            assert 0 <= op.y < height and op.x >= 0 and op.x + display_width(op.text) <= width


def test_cli_refuses_without_a_terminal(tmp_path: Path, capsys) -> None:
    db = str(tmp_path / "t.sqlite")
    assert main(["analyze", str(FIXTURES / "python_flow"), "--db", db]) == 0
    capsys.readouterr()
    assert main(["tui", "--db", db]) == 2
    assert "端末" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="pty が使えない環境")
def test_tui_runs_in_a_real_pseudo_terminal(tmp_path: Path) -> None:
    db = str(tmp_path / "t.sqlite")
    assert main(["analyze", str(FIXTURES / "c_callgraph"), "--db", db]) == 0
    master, slave = pty.openpty()  # fork せず、疑似端末を子プロセスの入出力に渡す
    env = {**os.environ, "TERM": "xterm", "LINES": "30", "COLUMNS": "110"}
    process = subprocess.Popen(
        [sys.executable, "-m", "codeinsight.cli", "tui", "--db", db], stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True
    )
    os.close(slave)
    fd = master
    collected = b""

    def drain(seconds: float) -> None:
        nonlocal collected
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    collected += os.read(fd, 65536)
                except OSError:
                    return

    try:
        drain(2.0)
        os.write(fd, b"?")
        drain(0.5)
        os.write(fd, b"x")
        os.write(fd, b"q")
        drain(1.0)
        returncode = process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
        os.close(fd)
    assert returncode == 0
    assert "構造".encode() in collected and b"CodeInsight" in collected

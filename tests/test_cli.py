from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codeinsight.cli import main, safe


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "cli.sqlite")


@pytest.fixture
def c_project(tmp_path: Path, db: str, c_callgraph_dir: Path, capsys) -> Path:
    root = tmp_path / "cproj"
    shutil.copytree(c_callgraph_dir, root)
    assert main(["analyze", str(root), "--db", db]) == 0
    capsys.readouterr()
    return root


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_analyze_json_summary(tmp_path: Path, db: str, python_pkg_dir: Path, capsys) -> None:
    code, out, _ = run(capsys, "analyze", str(python_pkg_dir), "--db", db, "--format", "json")
    data = json.loads(out)
    assert code == 0
    assert data["status"] == "success"
    assert data["files"] == 5 and data["references"] > 0
    assert any("動的インポート" in w for w in data["warnings"])


def test_read_commands_require_existing_database(tmp_path: Path, capsys) -> None:
    code, _, err = run(capsys, "symbols", "--db", str(tmp_path / "missing.sqlite"))
    assert code == 1 and "データベースが見つかりません" in err


def test_callers_callees_and_unresolved_are_shown(c_project: Path, db: str, capsys) -> None:
    code, out, err = run(capsys, "callees", "main", "--db", db)
    assert code == 0
    assert "[未解決※1]" in out and "[外部※2]" in out and "[解決]" in out
    # 理由は各行に繰り返さず、脚注にまとめて示す
    assert out.count("関数ポインタ経由の呼び出し") == 1
    assert "※1 関数ポインタ経由" in out
    assert "実行順序" in err  # 静的な関係である旨を併記

    code, out, _ = run(capsys, "callers", "fib", "--db", db, "--format", "json")
    results = json.loads(out)["results"]
    assert {r["source"] for r in results} == {"main", "fib"}

    code, out, _ = run(capsys, "unresolved", "--db", db)
    assert "■ 関数ポインタ経由の呼び出し" in out  # 理由別にまとめて表示
    assert "main.c:11" in out


def test_unresolved_is_grouped_by_reason_and_limited(c_project: Path, db: str, capsys) -> None:
    code, out, _ = run(capsys, "unresolved", "--limit", "1", "--db", db)
    assert code == 0
    assert out.count("■") == 2  # 理由は2種類（変数経由・構造体メンバ経由）
    code, out, _ = run(capsys, "unresolved", "--all", "--db", db)
    assert "ほか" not in out


def test_trace_marks_recursion_and_unresolved(c_project: Path, db: str, capsys) -> None:
    code, out, _ = run(capsys, "trace", "main", "--depth", "2", "--db", db)
    assert code == 0
    assert "再帰" in out and "[未解決]" in out
    # 外部の呼び出しは既定で省略し、件数だけ示す。同じ呼び出しは1行に集約する
    assert "[外部]" not in out and "外部の呼び出し 1件を省略" in out
    assert "×2" in out

    code, out, _ = run(capsys, "trace", "main", "--depth", "1", "--external", "--db", db)
    assert "printf  [外部]" in out and "省略" not in out.replace("深さ制限で省略", "")


def test_ambiguous_symbol_exits_with_candidates(
    tmp_path: Path, db: str, python_sample_dir: Path, capsys
) -> None:
    main(["analyze", str(python_sample_dir), "--db", db])
    capsys.readouterr()
    code, _, err = run(capsys, "callers", "area", "--db", db)
    assert code == 2
    assert "shapes.Circle.area" in err and "shapes.Shape.area" in err

    code, _, err = run(capsys, "callers", "no_such", "--db", db)
    assert code == 2 and "見つかりません" in err and "'shapes'" not in err
    assert "プロジェクト 'python_sample'" in err  # どのプロジェクトで探したかを示す


def test_path_and_deps_and_show(c_project: Path, db: str, capsys) -> None:
    code, out, _ = run(capsys, "path", "main", "fib", "--db", db)
    assert code == 0 and "経路1: main -> fib" in out

    code, out, _ = run(capsys, "deps", "ops.h", "--dependents", "--db", db)
    assert "main.c -> ops.h" in out and "ops.c -> ops.h" in out

    code, out, _ = run(capsys, "show", "ops.c:3-4", "--context", "0", "--db", db)
    assert code == 0
    assert out.splitlines() == ["> 3 | int add(int a, int b) {", "> 4 |     return a + b;"]


def test_stale_warning_after_source_change(c_project: Path, db: str, capsys) -> None:
    (c_project / "ops.c").write_text((c_project / "ops.c").read_text() + "\n/* 変更 */\n")

    code, out, err = run(capsys, "callees", "main", "--db", db)
    assert code == 0 and "解析後に変更されています" in err and "ops.c" in err

    code, out, _ = run(capsys, "status", "--db", db)
    assert "古い(ソース変更あり)" in out

    code, _, err = run(capsys, "show", "ops.c:1", "--db", db)
    assert "解析後に変更されています" in err


def test_graph_command_writes_file(c_project: Path, db: str, tmp_path: Path, capsys) -> None:
    target = tmp_path / "out.html"
    code, _, _ = run(capsys, "graph", "call", "--root", "apply", "--depth", "1",
                     "--format", "html", "-o", str(target), "--db", db)
    assert code == 0 and "<svg" in target.read_text(encoding="utf-8")

    code, out, _ = run(capsys, "graph", "deps", "--format", "mermaid", "--db", db)
    assert code == 0 and out.startswith("graph LR")

    code, _, err = run(capsys, "graph", "deps", "--root", "nope.c", "--db", db)
    assert code == 2 and "ファイルが見つかりません" in err


def test_search_modes(c_project: Path, db: str, capsys) -> None:
    code, out, _ = run(capsys, "search", "fib", "--match", "exact", "--kind", "function", "--db", db)
    assert out.strip().startswith("ops.c:16-21")

    code, out, _ = run(capsys, "search", "OPS", "--files", "--db", db)
    assert out.splitlines() == ["ops.c\tc", "ops.h\tc"]

    code, out, _ = run(capsys, "search", r"fib\(", "--text", "--regex", "--db", db)
    assert "ops.c:20" in out

    code, _, err = run(capsys, "search", "(", "--text", "--regex", "--db", db)
    assert code == 1 and "正規表現" in err


def test_multiple_projects_require_selection(
    tmp_path: Path, db: str, c_callgraph_dir: Path, python_pkg_dir: Path, capsys
) -> None:
    main(["analyze", str(c_callgraph_dir), "--db", db])
    main(["analyze", str(python_pkg_dir), "--db", db])
    capsys.readouterr()

    code, _, err = run(capsys, "symbols", "--db", db)
    assert code == 1 and "--project" in err
    code, out, _ = run(capsys, "symbols", "--db", db, "--project", "python_pkg", "--kind", "class")
    assert code == 0 and "app.models.Base" in out


def test_terminal_control_characters_in_source_are_neutralised() -> None:
    assert safe("a\x1b[31mred\x07") == "a\\x1b[31mred\\x07"
    assert safe("通常の日本語\tタブ") == "通常の日本語\tタブ"


def test_not_found_suggests_similar_names(c_project: Path, db: str, capsys) -> None:
    code, _, err = run(capsys, "callers", "fibb", "--db", db)
    assert code == 2
    assert "もしかして" in err and "fib" in err


def test_exclude_filters_results_without_touching_stored_analysis(
    c_project: Path, db: str, capsys
) -> None:
    code, out, _ = run(capsys, "callers", "fib", "--exclude", "main.c", "--db", db)
    assert code == 0 and "ops.c:20" in out and "main.c:13" not in out

    code, out, _ = run(capsys, "symbols", "--exclude", "ops.*", "--db", db)
    assert "ops." not in out and "main.c" in out

    code, out, _ = run(capsys, "graph", "deps", "--exclude", "main.c", "--format", "json", "--db", db)
    labels = {n["label"] for n in json.loads(out)["nodes"]}
    assert "main.c" not in labels and "ops.c" in labels

    # 絞り込みは表示だけの操作で、保存済みの結果は変わらない
    code, out, _ = run(capsys, "callers", "fib", "--db", db)
    assert "main.c:13" in out and "ops.c:20" in out


def test_tree_merges_module_into_file_and_supports_depth(
    tmp_path: Path, db: str, capsys
) -> None:
    root = tmp_path / "t"
    root.mkdir()
    (root / "a.py").write_text("X = 1\n\n\nclass A:\n    y = 2\n\n    def m(self):\n        pass\n")
    main(["analyze", str(root), "--db", db])
    capsys.readouterr()

    code, out, _ = run(capsys, "tree", "--db", db)
    assert "(module" not in out  # モジュールシンボルはファイルノードに統合される
    assert "X  (global_variable" in out and "m  (method" in out

    code, out, _ = run(capsys, "tree", "--no-variables", "--depth", "1", "--db", db)
    assert "A  (class" in out and "X  (" not in out and "m  (method" not in out


def test_closed_pipe_exits_quietly(c_project: Path, db: str, monkeypatch, capsys) -> None:
    import codeinsight.cli.parser as cli

    def broken(*_args, **_kwargs):
        raise BrokenPipeError

    monkeypatch.setattr(cli, "cmd_symbols", broken)
    parser_main = cli.main
    # build_parserで束縛済みの関数を差し替えるため、parserを作り直させる
    assert parser_main(["symbols", "--db", db]) == 0

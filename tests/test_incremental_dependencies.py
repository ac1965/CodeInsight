"""依存を考慮した差分解析: include しているヘッダーの変更・削除、compile_commands.json の変更で、C/C++ を解析し直す。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeinsight.application.analysis_coordinator import include_dependents
from codeinsight.cli import main
from codeinsight.domain import Dependency, DependencyKind, SourceLocation
from codeinsight.infrastructure.analysis_repository import AnalysisRepository

HEADER_ON = "#define ENABLE_EXTRA 1\n"
HEADER_OFF = "/* extra disabled */\n"
LEVEL2 = '#include "config.h"\n'
MAIN_C = '#include "level2.h"\n#ifdef ENABLE_EXTRA\nint extra(void) { return 1; }\n#endif\nint core(void) { return 0; }\n'
OTHER_C = "int standalone(void) { return 2; }\n"
PY = "def py_func():\n    return 1\n"


@pytest.fixture
def project(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    for name, text in {"config.h": HEADER_ON, "level2.h": LEVEL2, "main.c": MAIN_C, "other.c": OTHER_C, "mod.py": PY}.items():
        (root / name).write_text(text, encoding="utf-8")
    return root, str(tmp_path / "d.sqlite")


def analyze(root: Path, db: str, capsys) -> dict:
    capsys.readouterr()
    assert main(["analyze", str(root), "--db", db, "--format", "json"]) == 0
    return json.loads(capsys.readouterr().out)


def symbols(db: str) -> set[str]:
    repository = AnalysisRepository(Path(db))
    try:
        project = repository.list_projects()[0]
        return {s.qualified_name for s in repository.list_symbols_for_project(project.project_id)}
    finally:
        repository.close()


def test_changing_a_header_reanalyzes_transitive_includers_only(project, capsys) -> None:
    root, db = project
    first = analyze(root, db, capsys)
    assert first["reanalysis"]["changed"] == 5 and "extra" in symbols(db)
    unchanged = analyze(root, db, capsys)
    assert unchanged["reanalysis"] == {**unchanged["reanalysis"], "changed": 0, "dependents": 0, "skipped": 5}  # 何も変えなければ、全部スキップ
    (root / "config.h").write_text(HEADER_OFF, encoding="utf-8")  # main.c は変えていない。level2.h を経由して include している
    after = analyze(root, db, capsys)
    r = after["reanalysis"]
    assert r["changed"] == 1 and r["dependents"] == 2 and sorted(r["dependent_files"]) == ["level2.h", "main.c"]  # 推移的な include 元だけを解析し直す
    assert r["skipped"] == 2  # other.c と mod.py（依存なし）は、スキップのまま
    assert "extra" not in symbols(db)  # 条件付きコンパイルで無効になった関数が、古い結果として残らない


def test_incremental_result_equals_a_fresh_analysis(project, tmp_path: Path, capsys) -> None:
    root, db = project
    analyze(root, db, capsys)
    (root / "config.h").write_text(HEADER_OFF, encoding="utf-8")
    analyze(root, db, capsys)
    fresh_db = str(tmp_path / "fresh.sqlite")
    analyze(root, fresh_db, capsys)
    assert symbols(db) == symbols(fresh_db)


def test_deleting_a_header_reanalyzes_its_includers(project, capsys) -> None:
    root, db = project
    analyze(root, db, capsys)
    (root / "config.h").unlink()
    after = analyze(root, db, capsys)
    assert after["reanalysis"]["removed"] == 1 and "level2.h" in after["reanalysis"]["dependent_files"]
    assert "extra" not in symbols(db)  # ヘッダーが無くなれば、そのマクロで有効だった関数も無くなる（include 元は、前回の結果を使い回さない）


def test_changing_compile_commands_reanalyzes_c_files_only(project, tmp_path: Path, capsys) -> None:
    root, db = project
    build = tmp_path / "build"
    build.mkdir()
    db_file = build / "compile_commands.json"

    def write(flags: list[str]) -> None:
        db_file.write_text(json.dumps([{"directory": str(root), "file": str(root / "main.c"), "arguments": ["cc", "-c", *flags, str(root / "main.c")]}]), encoding="utf-8")

    write([])
    capsys.readouterr()
    args = ["analyze", str(root), "--db", db, "--compile-commands", str(build), "--format", "json"]
    assert main(args) == 0
    capsys.readouterr()
    assert main(args) == 0
    same = json.loads(capsys.readouterr().out)["reanalysis"]
    assert same["changed"] == 0 and same["rebuilt_for_build_config"] == 0  # 設定が同じなら、スキップ
    write(["-DENABLE_EXTRA2=1"])
    assert main(args) == 0
    changed = json.loads(capsys.readouterr().out)["reanalysis"]
    assert changed["rebuilt_for_build_config"] == 4 and changed["skipped"] == 1  # C のファイル（ヘッダー2・ソース2）だけ。Python は対象外


def test_include_dependents_is_transitive_and_ignores_other_kinds() -> None:
    def dep(source: str, target: str | None, kind=DependencyKind.INCLUDE) -> Dependency:
        return Dependency("d" + source + str(target), source, target or "x", target, kind, SourceLocation(source, 1, 1), target_file_id=target)

    deps = [dep("a", "b"), dep("b", "c"), dep("x", "c", DependencyKind.IMPORT), dep("y", None)]
    assert include_dependents(deps, {"c"}) == {"a", "b"}  # import は対象外。解決できない include も対象外
    assert include_dependents(deps, {"a"}) == set()

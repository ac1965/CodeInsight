"""Goの補助プログラム（cifast）と、Python の実装の結果が同じであることの確認（走査・ハッシュ・経路探索）。"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
from pathlib import Path

import pytest

from codeinsight.domain import Project
from codeinsight.domain.project import ProjectConfiguration
from codeinsight.infrastructure.file_scanner import FileScanner
from codeinsight.native import helper as helper_module
from codeinsight.native import native_helper

pytestmark = pytest.mark.skipif(shutil.which("go") is None, reason="Goのツールチェーンが無い")


def _project(root: Path, **configuration) -> Project:
    return Project(project_id="p", root_path=root, name="p", configuration=ProjectConfiguration(**configuration))


def _python_scan(project: Project, monkeypatch) -> list[str]:
    with monkeypatch.context() as m:
        m.setenv("CODEINSIGHT_NO_NATIVE", "1")
        return [f.relative_path for f in FileScanner().scan(project)]


def _native_scan(project: Project) -> list[str]:
    return [f.relative_path for f in FileScanner().scan(project)]


def _tree(root: Path, paths: list[str]) -> None:
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")


PATTERNS = [
    "*.log", "build/", "/top.txt", "docs/*.md", "**/gen/**", "src/**/tmp", "!keep.log", "!docs/keep.md", "a?c.txt", "*", "**",
    "/lib/", "node_modules", "foo/bar", "*.py[cod]", "# comment", "", "  ", "x/**", "**/y", "dir/*", "!dir/ok.txt", "sub/", "name with space.txt",
]
FILES = [
    "a.py", "b.log", "keep.log", "top.txt", "sub/top.txt", "build/o.c", "src/build/o.c", "docs/a.md", "docs/keep.md", "docs/deep/c.md",
    "src/gen/g.c", "src/x/gen/h.c", "src/a/b/tmp", "src/tmp", "abc.txt", "a/c.txt", "lib/l.c", "src/lib/l.c", "node_modules/m.js", "foo/barf",
    "foo/bar/baz.c", "x/1/2.c", "p/y", "p/q/y/z.c", "dir/ok.txt", "dir/no.txt", "dir/sub/n.txt", "sub/in.c", "name with space.txt", "m.pyc",
]


@pytest.mark.parametrize("pattern", [p for p in PATTERNS if p.strip()])
def test_gitignore_single_pattern_matches_pathspec(tmp_path: Path, monkeypatch, pattern: str) -> None:
    _tree(tmp_path, FILES)
    (tmp_path / ".gitignore").write_text(pattern + "\n", encoding="utf-8")
    project = _project(tmp_path)
    assert _native_scan(project) == _python_scan(project, monkeypatch)


def test_gitignore_pattern_combinations_match_pathspec(tmp_path: Path, monkeypatch) -> None:
    _tree(tmp_path, FILES)
    rng = random.Random(7)
    project = _project(tmp_path)
    for _ in range(40):
        lines = rng.sample([p for p in PATTERNS if p.strip()], rng.randint(2, 6))
        (tmp_path / ".gitignore").write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert _native_scan(project) == _python_scan(project, monkeypatch), lines


def test_unsupported_gitignore_syntax_falls_back_to_python(tmp_path: Path, monkeypatch) -> None:
    _tree(tmp_path, FILES)
    (tmp_path / ".gitignore").write_text("*.py[cod]\n\\#literal\n", encoding="utf-8")
    assert native_helper().scan(tmp_path, ["*.py[cod]"], set(), set()) is None
    project = _project(tmp_path)
    assert _native_scan(project) == _python_scan(project, monkeypatch)  # 結果は同じ（Python 側で処理する）


def test_scan_exclusions_and_symlinks_match_python(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    _tree(root, ["a.c", "pkg/b.py", "pkg/skip.py", "build/x.c", "deep/er/c.c"])
    _tree(outside, ["secret.c"])
    (root / "link_out").symlink_to(outside)  # ルート外へのディレクトリのリンク（辿らない）
    (root / "link_in").symlink_to(root / "pkg")  # ルート内の実体を指すリンク（訪問済みなら辿らない）
    (root / "loop").symlink_to(root)  # 循環
    (root / "file_out.c").symlink_to(outside / "secret.c")
    (root / "file_in.c").symlink_to(root / "a.c")
    (root / "broken.c").symlink_to(root / "nope.c")
    project = _project(root, exclude_files=("skip.py",))
    assert _native_scan(project) == _python_scan(project, monkeypatch)
    assert "secret.c" not in "".join(_native_scan(project))


def test_scan_without_gitignore_setting(tmp_path: Path, monkeypatch) -> None:
    _tree(tmp_path, ["a.c", "ignored.c"])
    (tmp_path / ".gitignore").write_text("ignored.c\n", encoding="utf-8")
    project = _project(tmp_path, respect_gitignore=False)
    assert _native_scan(project) == _python_scan(project, monkeypatch) == [".gitignore", "a.c", "ignored.c"]


def test_scan_real_repository_matches_python(monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    project = _project(root)
    assert _native_scan(project) == _python_scan(project, monkeypatch)


def test_hash_matches_hashlib_and_reports_unreadable(tmp_path: Path) -> None:
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(3_000_000))
    small = tmp_path / "s.txt"
    small.write_text("日本語\n", encoding="utf-8")
    empty = tmp_path / "e"
    empty.write_bytes(b"")
    paths = [str(big), str(small), str(empty), str(tmp_path / "missing"), str(tmp_path)]
    hashes = native_helper().hash_files(paths)
    assert hashes is not None
    assert hashes[str(big)] == hashlib.sha256(big.read_bytes()).hexdigest()
    assert hashes[str(small)] == hashlib.sha256(small.read_bytes()).hexdigest()
    assert hashes[str(empty)] == hashlib.sha256(b"").hexdigest()
    assert hashes[str(tmp_path / "missing")] is None
    assert hashes[str(tmp_path)] is None  # ディレクトリは読めない（Python の read_bytes と同じく None 扱い）


def _python_paths(edges: dict[str, list[tuple[int, str]]], source: str, target: str, max_depth: int, limit: int) -> list[list[int]]:
    paths: list[list[int]] = []

    def walk(current: str, visited: set[str], trail: list[int]) -> None:
        if len(paths) >= limit or len(trail) >= max_depth:
            return
        for edge_id, next_id in edges.get(current, []):
            if next_id in visited:
                continue
            if next_id == target:
                paths.append([*trail, edge_id])
                if len(paths) >= limit:
                    return
                continue
            walk(next_id, visited | {next_id}, [*trail, edge_id])

    walk(source, {source}, [])
    return paths


@pytest.mark.parametrize("seed", range(8))
def test_paths_match_python_algorithm(seed: int) -> None:
    rng = random.Random(100 + seed)
    names = [f"n{i}" for i in range(rng.randint(3, 14))]
    edges: dict[str, list[tuple[int, str]]] = {}
    for edge_id in range(rng.randint(3, 40)):
        edges.setdefault(rng.choice(names), []).append((edge_id, rng.choice(names)))
    source, target = rng.sample(names, 2)
    for max_depth, limit in ((8, 20), (3, 5), (1, 1), (12, 100)):
        assert native_helper().paths(edges, source, target, max_depth, limit) == _python_paths(edges, source, target, max_depth, limit)


def test_unavailable_helper_returns_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEINSIGHT_NO_NATIVE", "1")
    assert helper_module.NativeHelper().paths({}, "a", "b", 3, 3) is None
    assert helper_module.NativeHelper().hash_files([str(tmp_path)]) is None
    monkeypatch.delenv("CODEINSIGHT_NO_NATIVE")
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("CODEINSIGHT_DATA_DIR", str(tmp_path / "data"))
    helper = helper_module.NativeHelper()
    assert helper.hash_files([str(tmp_path)]) is None  # go が無い → Python へ戻る（エラーにしない）
    assert helper.unavailable_reason is not None



def test_call_graph_find_paths_same_result_with_and_without_native(monkeypatch) -> None:
    """CallGraph.find_paths は、Go を使う大きい上限でも、使わない場合と同じ結果を返す。"""

    from codeinsight.analysis import call_graph as module
    from codeinsight.domain import Reference, ReferenceKind, ResolutionStatus, SourceLocation, Symbol, SymbolKind

    rng = random.Random(5)
    count = 60
    symbols = [Symbol(symbol_id=f"s{i}", file_id="f", name=f"s{i}", qualified_name=f"s{i}", kind=SymbolKind.FUNCTION, start_line=i + 1, end_line=i + 1, parent_symbol_id=None) for i in range(count)]
    references = []
    for n in range(300):
        a, b = rng.randrange(count), rng.randrange(count)
        references.append(Reference(
            reference_id=f"r{n}", source_symbol_id=f"s{a}", target_symbol_id=f"s{b}", reference_kind=ReferenceKind.CALL,
            source_location=SourceLocation(file_id="f", start_line=n + 1, end_line=n + 1), resolution_status=ResolutionStatus.RESOLVED, target_name=f"s{b}", target_key=f"s{b}",
        ))
    graph = module.CallGraph(symbols, references)
    monkeypatch.setattr(module, "NATIVE_MIN_EDGES", 0)
    monkeypatch.setattr(module, "NATIVE_MIN_LIMIT", 1)
    with_native = graph.find_paths("s0", "s1", 5, 50)
    monkeypatch.setattr(module, "NATIVE_MIN_LIMIT", 10**9)
    assert with_native == graph.find_paths("s0", "s1", 5, 50)
    assert with_native

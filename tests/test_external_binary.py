"""外部ツール radare2 の結果の取り込み（import-r2）と比較（compare-r2）。radare2 は、利用者が実行した出力を読むだけ。"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from codeinsight.application import ProjectIndex
from codeinsight.application.external_binary_service import ExternalBinaryService
from codeinsight.cli import main
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.infrastructure import r2 as r2lib
from codeinsight.infrastructure.r2 import R2Error, candidate_names, read_callgraph, read_functions, read_info

SOURCE = """\
typedef int (*op)(int);
static int helper(int x) { return x + 1; }
static int twice(int x) { return x * 2; }
int unused(void) { return 0; }
int shared(void) { return 1; }
static op table[2] = {helper, twice};
int apply(op f, int v) { return f(v); }
int main(void) { int t = helper(1); t += twice(t); t += apply(helper, t); return t; }
"""
OTHER = "int shared(void) { return 2; }\n"


def fn(name: str, file: str = "main.c", addr: int = 4096) -> dict:
    return {"name": name, "addr": addr, "size": 16, "file": file, "signature": f"int {name} ();", "nbbs": 1, "indegree": 0, "outdegree": 0}


FUNCTIONS = [fn("dbg.main"), fn("dbg.helper"), fn("dbg.twice"), fn("dbg.apply"), fn("sym.imp.printf", file=""), fn("dbg.generated_stub", file="gen.c"), fn("dbg.shared", file="other.c")]
CALLGRAPH = [
    {"name": "dbg.main", "size": 100, "imports": ["sym._helper", "sym._twice", "sym._apply", "sym.imp.printf"]},
    {"name": "dbg.apply", "size": 40, "imports": ["sym._twice"]},  # 関数ポインタ経由の呼び出しを、radare2 が解決した辺（CodeInsightは未解決）
]


@pytest.fixture
def project(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.c").write_text(SOURCE, encoding="utf-8")
    (root / "other.c").write_text(OTHER, encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    return root, db, tmp_path


def write(path: Path, data) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_reader_validates_untrusted_input() -> None:
    assert len(read_functions(json.dumps(FUNCTIONS).encode())) == 7
    for bad in (b"not json", b'{"name": "x"}', b'[{"addr": 1}]', b'["x"]'):
        with pytest.raises(R2Error):
            read_functions(bad)
    with pytest.raises(R2Error):
        read_callgraph(b'{"a": 1}')
    with pytest.raises(R2Error):
        read_info(b"[1]")
    long = read_functions(json.dumps([fn("x" * 5000)]).encode())[0]
    assert len(long.name) == r2lib.MAX_TEXT  # 長さを制限する
    assert read_callgraph(json.dumps(CALLGRAPH + CALLGRAPH).encode()).count(("dbg.main", "sym._helper")) == 1  # 重複は除く


def test_candidate_names_strip_prefixes_and_macho_underscore() -> None:
    assert candidate_names("dbg.main") == ["main"]
    assert candidate_names("sym._helper") == ["_helper", "helper"]
    assert candidate_names("sym.imp.printf") == ["printf"]
    assert candidate_names("sym.geo::Shape::scale(int)") == ["geo::Shape::scale"]


def test_import_and_compare_separate_binary_only_source_only_and_ambiguous(project, capsys) -> None:
    root, db, tmp = project
    funcs = write(tmp / "f.json", FUNCTIONS)
    graph = write(tmp / "g.json", CALLGRAPH)
    assert main(["import-r2", str(funcs), "--callgraph", str(graph), "--db", db, "--tool-version", "6.2.4"]) == 0
    err = capsys.readouterr()
    assert "関数 7件" in err.out and "外部ライブラリ 1件" in err.out and "実行していません" in err.err
    capsys.readouterr()
    assert main(["compare-r2", "--db", db, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"].startswith("radare2（外部ツール")
    matched = {p["symbol"] for p in data["matched"]}
    assert {"main", "helper", "twice", "apply"} <= matched
    assert "shared" in matched and "dbg.generated_stub" in data["binary_only"]  # 別ファイルの同名関数は、デバッグ情報のファイルで絞り込む。対応するソースが無いものは、バイナリのみ
    both = {(e["caller"], e["callee"]) for e in data["edges_both"]}
    assert ("main", "helper") in both and ("main", "twice") in both
    only = {(e["caller"], e["callee"]): e for e in data["edges_binary_only"]}
    assert ("apply", "twice") in only and any("f" in c for c in only[("apply", "twice")]["candidates"])  # 関数ポインタ経由の呼び出しは、解決候補として示す（採用はしない）
    assert "unused" in data["source_only"]  # バイナリに無い関数（main.c の中）
    assert any("最適化" in n for n in data["notes"])


def test_ambiguous_names_are_not_resolved_by_picking_one(project, capsys) -> None:
    root, db, tmp = project
    no_file = [dict(fn("dbg.shared", file=""))]
    assert main(["import-r2", str(write(tmp / "f.json", no_file)), "--db", db]) == 0
    capsys.readouterr()
    assert main(["compare-r2", "--db", db, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [a["binary_function"] for a in data["ambiguous"]] == ["dbg.shared"] and data["matched"] == []  # shared は main.c と other.c の2つ。1つを選ばない


def test_changed_source_is_excluded_as_stale_and_reimport_replaces(project, capsys) -> None:
    root, db, tmp = project
    funcs = write(tmp / "f.json", FUNCTIONS)
    graph = write(tmp / "g.json", CALLGRAPH)
    args = ["import-r2", str(funcs), "--callgraph", str(graph), "--db", db]
    assert main(args) == 0 and main(args) == 0  # 同じ入力の再取り込みは、置き換える（重複しない）
    repository = AnalysisRepository(Path(db))
    project_row = repository.list_projects()[0]
    assert len(ExternalBinaryService(repository).summaries(project_row)) == 1
    repository.close()
    (root / "main.c").write_text(SOURCE + "/* edited */\n", encoding="utf-8")
    capsys.readouterr()
    assert main(["compare-r2", "--db", db, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stale_files"] == ["main.c"] and not any(p["path"] == "main.c" for p in data["matched"])  # 取り込み後に変わったソースは、比較から外す


def test_no_debug_info_stripped_and_unmatched_files_are_reported(project, capsys) -> None:
    root, db, tmp = project
    stripped = [fn("sym.main", file=""), fn("fcn.00001000", file="")]
    info = write(tmp / "i.json", {"core": {"file": "/nowhere/app"}, "bin": {"arch": "arm", "os": "macos", "stripped": True}})
    assert main(["import-r2", str(write(tmp / "f.json", stripped)), "--info", str(info), "--db", db]) == 0
    out = capsys.readouterr().out
    assert "デバッグ情報（DWARF）が無い" in out and "ストリップ済み" in out and "ハッシュを記録していません" in out  # 精度の限界を示す
    unmatched = [fn("dbg.main", file="not_in_project.c")]
    assert main(["import-r2", str(write(tmp / "f2.json", unmatched)), "--db", db]) == 0
    assert "対応づけられなかった: not_in_project.c" in capsys.readouterr().out


def test_errors_and_clear_do_not_touch_the_target(project, capsys) -> None:
    root, db, tmp = project
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert main(["compare-r2", "--db", db]) != 0  # 取り込み前は、案内つきのエラー
    bad = tmp / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert main(["import-r2", str(bad), "--db", db]) != 0  # 壊れた入力は、エラーにする（取り込まない）
    assert main(["import-r2", str(write(tmp / "f.json", FUNCTIONS)), "--db", db]) == 0
    assert main(["import-r2", "--clear", "--db", db]) == 0
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before  # 対象は変更しない


needs_r2 = pytest.mark.skipif(shutil.which("r2") is None or shutil.which("cc") is None, reason="radare2 と cc が必要")


@needs_r2
def test_real_radare2_output_on_a_binary_built_with_debug_info(tmp_path: Path, capsys) -> None:
    """自分でビルドした小さなバイナリを、radare2 で静的に解析した出力を取り込む（バイナリは実行しない）。"""

    root = tmp_path / "real"
    root.mkdir()
    (root / "main.c").write_text("#include <stdio.h>\nstatic int helper(int x) { return x + 1; }\nint unused(void) { return 0; }\nint main(void) { printf(\"%d\\n\", helper(1)); return 0; }\n", encoding="utf-8")
    binary = tmp_path / "app"
    subprocess.run(["cc", "-g", "-O0", str(root / "main.c"), "-o", str(binary)], check=True, capture_output=True)
    for name, command in (("f.json", "aaa; aflj"), ("g.json", "aaa; agCj"), ("i.json", "ij")):
        done = subprocess.run(["r2", "-q", "-c", command, str(binary)], capture_output=True, timeout=120, check=True)
        (tmp_path / name).write_bytes(done.stdout)
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    capsys.readouterr()
    assert main(["import-r2", str(tmp_path / "f.json"), "--callgraph", str(tmp_path / "g.json"), "--info", str(tmp_path / "i.json"), "--binary", str(binary), "--db", db]) == 0
    capsys.readouterr()
    assert main(["compare-r2", "--db", db, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {"main", "helper", "unused"} <= {p["symbol"] for p in data["matched"]}
    assert ("main", "helper") in {(e["caller"], e["callee"]) for e in data["edges_both"]}


def test_project_index_unaffected_by_imports(project) -> None:
    """取り込みは解析結果（シンボル・参照）を変えない。"""

    root, db, tmp = project
    repository = AnalysisRepository(Path(db))
    p = repository.list_projects()[0]
    before = len(ProjectIndex.load(repository, p.project_id).references)
    repository.close()
    assert main(["import-r2", str(write(tmp / "f.json", FUNCTIONS)), "--callgraph", str(write(tmp / "g.json", CALLGRAPH)), "--db", db]) == 0
    repository = AnalysisRepository(Path(db))
    assert len(ProjectIndex.load(repository, p.project_id).references) == before
    repository.close()

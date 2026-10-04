from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeinsight.cli import main
from codeinsight.infrastructure.scip import ScipError, read_scip, symbol_name

# --- 最小のprotobufエンコーダー（テスト用。実際の索引は外部ツールが生成する） ---


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, payload: bytes | int | str) -> bytes:
    if isinstance(payload, int):
        return _varint(number << 3) + _varint(payload)
    data = payload.encode() if isinstance(payload, str) else payload
    return _varint(number << 3 | 2) + _varint(len(data)) + data


def _occurrence(line: int, start: int, end: int, symbol: str, roles: int = 0) -> bytes:
    packed = b"".join(_varint(n) for n in (line, start, end))
    return _field(1, packed) + _field(2, symbol) + (_field(3, roles) if roles else b"")


def _document(path: str, occurrences: list[bytes], symbols: list[tuple[str, str]] = ()) -> bytes:  # type: ignore[assignment]
    body = _field(1, path) + _field(4, "Python")
    body += b"".join(_field(2, o) for o in occurrences)
    body += b"".join(_field(3, _field(1, s) + _field(6, n) + _field(3, f"{n} の説明")) for s, n in symbols)
    return body


def _index(root: Path, documents: list[bytes], tool: str = "scip-python", version: str = "0.6.0") -> bytes:
    metadata = _field(2, _field(1, tool) + _field(2, version)) + _field(3, root.as_uri())
    return _field(1, metadata) + b"".join(_field(2, d) for d in documents)


HELPER = "scip-python python demo 1.0 a/helper()."
HELPER_D = "scip-python python demo 1.0 d/helper()."
MAIN = "scip-python python demo 1.0 a/main()."


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def helper():\n    return 1\n\n\ndef main():\n    return helper()\n", encoding="utf-8")
    (root / "d.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
    (root / "b.py").write_text("from a import main\n\n\ndef run(obj):\n    return obj.helper() + main()\n", encoding="utf-8")
    (root / "c.py").write_text("from a import helper\n\n\ndef conflict():\n    return helper()\n", encoding="utf-8")
    return root


def _write_index(root: Path, tmp_path: Path) -> Path:
    documents = [
        _document("a.py", [_occurrence(0, 4, 10, HELPER, 1), _occurrence(4, 4, 8, MAIN, 1), _occurrence(5, 11, 17, HELPER)], [(HELPER, "helper"), (MAIN, "main")]),
        _document("d.py", [_occurrence(0, 4, 10, HELPER_D, 1)], [(HELPER_D, "helper")]),
        _document("b.py", [_occurrence(3, 4, 7, "scip-python python demo 1.0 b/run().", 1), _occurrence(4, 11, 17, HELPER), _occurrence(4, 25, 29, MAIN)]),
        _document("c.py", [_occurrence(4, 11, 17, HELPER_D)]),  # 索引は d.helper とみなす（自身は a.helper に解決）
    ]
    path = tmp_path / "index.scip"
    path.write_bytes(_index(root, documents))
    return path


def test_symbol_name_parses_the_last_descriptor() -> None:
    assert symbol_name("scip-python python demo 1.0 pkg/mod.py/Class#method().") == "method"
    assert symbol_name("scip-python python demo 1.0 a/helper().") == "helper"
    assert symbol_name("scip-clang cxx . . `ns`/Widget#draw(+1).") == "draw"
    assert symbol_name("scip-python python my  pkg 1.0 a/f().") == "f"  # 名前の空白（二重の空白）のエスケープ
    assert symbol_name("local 7") == "" and symbol_name("broken") == ""


def test_decoder_reads_metadata_documents_and_ranges(tmp_path: Path) -> None:
    path = _write_index(tmp_path, tmp_path)
    metadata, documents = read_scip(path.read_bytes())
    assert (metadata.tool, metadata.tool_version) == ("scip-python", "0.6.0")
    docs = list(documents)
    assert [d.relative_path for d in docs] == ["a.py", "d.py", "b.py", "c.py"]
    first = docs[0].occurrences[0]
    assert (first.start_line, first.start_char, first.end_char, first.is_definition) == (0, 4, 10, True)
    assert docs[0].symbols[0].display_name == "helper" and "説明" in docs[0].symbols[0].documentation


@pytest.mark.parametrize("data", [b"\xff\xff\xff", b"", b"\x0a\x05ab", b"\x08\x01"])
def test_decoder_rejects_broken_input(data: bytes) -> None:
    with pytest.raises(ScipError):
        read_scip(data)


def test_import_and_compare_shows_agreement_conflict_and_index_only(project: Path, tmp_path: Path, capsys) -> None:
    db = tmp_path / "s.db"
    assert main(["analyze", str(project), "--db", str(db)]) == 0
    args = ["--db", str(db), "--project", str(project)]
    index = _write_index(project, tmp_path)
    capsys.readouterr()
    assert main(["import-scip", str(index), *args]) == 0
    assert "文書 4件" in capsys.readouterr().out
    assert main(["compare-scip", "--format", "json", *args]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["confirmed"] >= 2  # a.py の helper() と、b.py の main()
    conflicts = {(c["path"], c["name"]): c for c in data["conflicts"]}
    assert ("c.py", "helper") in conflicts and conflicts[("c.py", "helper")]["index_definitions"][0]["path"] == "d.py"  # 食い違いは、並べて示す
    only_index = {(c["path"], c["name"]) for c in data["index_resolves"]}
    assert ("b.py", "helper") in only_index  # obj.helper() は自身では解決できないが、索引は定義を示す
    # 自身の解析結果は、索引で書き換えない
    assert main(["callees", "run", *args]) == 0
    assert "helper" in capsys.readouterr().out


def test_stale_file_is_excluded_and_reimport_replaces(project: Path, tmp_path: Path, capsys) -> None:
    db = tmp_path / "s.db"
    assert main(["analyze", str(project), "--db", str(db)]) == 0
    args = ["--db", str(db), "--project", str(project)]
    index = _write_index(project, tmp_path)
    for _ in range(2):
        assert main(["import-scip", str(index), *args]) == 0
    (project / "c.py").write_text("# 追記\n" + (project / "c.py").read_text(encoding="utf-8"), encoding="utf-8")
    capsys.readouterr()
    assert main(["compare-scip", "--format", "json", *args]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stale_files"] == ["c.py"] and len(data["indexes"]) == 1  # 変更後のファイルは比較しない・再取り込みで重複しない
    assert not any(c["path"] == "c.py" for c in data["conflicts"])
    assert main(["import-scip", "--clear", *args]) == 0
    capsys.readouterr()
    assert main(["compare-scip", *args]) != 0  # 取り込み済みの索引がない


def test_documents_outside_the_project_or_missing_are_not_imported(project: Path, tmp_path: Path, capsys) -> None:
    db = tmp_path / "s.db"
    assert main(["analyze", str(project), "--db", str(db)]) == 0
    documents = [_document("../outside.py", [_occurrence(0, 0, 1, HELPER)]), _document("nothing.py", [_occurrence(0, 0, 1, HELPER)]), _document("a.py", [_occurrence(0, 4, 10, HELPER, 1)])]
    path = tmp_path / "x.scip"
    path.write_bytes(_index(project, documents))
    capsys.readouterr()
    assert main(["import-scip", str(path), "--db", str(db), "--project", str(project)]) == 0
    out = capsys.readouterr().out
    assert "文書 1件" in out and "ルートの外" in out and "存在しないファイル" in out


def test_import_does_not_modify_the_target(project: Path, tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    assert main(["analyze", str(project), "--db", str(db)]) == 0
    before = {p.name: p.read_bytes() for p in project.iterdir()}
    assert main(["import-scip", str(_write_index(project, tmp_path)), "--db", str(db), "--project", str(project)]) == 0
    assert {p.name: p.read_bytes() for p in project.iterdir()} == before

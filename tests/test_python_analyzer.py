from __future__ import annotations

import uuid
from pathlib import Path

from codeinsight.analysis.language_adapter import SourceUnit
from codeinsight.analysis.python_analyzer import PythonAnalyzer
from codeinsight.domain import SymbolKind


def test_extracts_module_class_function_symbols(python_sample_dir: Path) -> None:
    analyzer = PythonAnalyzer()
    result = analyzer.analyze_file(SourceUnit.from_path(str(uuid.uuid4()), python_sample_dir / "shapes.py"))

    assert result.succeeded, result.errors
    by_name = {s.name: s for s in result.symbols}

    assert by_name["shapes"].kind == SymbolKind.MODULE
    assert by_name["Shape"].kind == SymbolKind.CLASS
    assert by_name["Circle"].kind == SymbolKind.CLASS
    assert "Shape" in by_name["Circle"].base_classes
    assert by_name["fetch_area"].kind == SymbolKind.FUNCTION
    assert by_name["fetch_area"].is_async is True
    assert by_name["DEFAULT_COLOR"].kind == SymbolKind.GLOBAL_VARIABLE
    assert by_name["kind"].kind == SymbolKind.CLASS_VARIABLE

    area_methods = [s for s in result.symbols if s.name == "area"]
    assert len(area_methods) == 2
    assert all(s.kind == SymbolKind.METHOD for s in area_methods)


def test_records_decorators(python_sample_dir: Path) -> None:
    analyzer = PythonAnalyzer()
    result = analyzer.analyze_file(SourceUnit.from_path(str(uuid.uuid4()), python_sample_dir / "shapes.py"))

    circle_area = next(
        s
        for s in result.symbols
        if s.name == "area" and s.qualified_name == "shapes.Circle.area"
    )
    assert any("lru_cache" in decorator for decorator in circle_area.decorators)


def test_syntax_error_is_recorded_as_failure(tmp_path: Path) -> None:
    broken = tmp_path / "broken.py"
    broken.write_text("def broken(:\n    pass\n", encoding="utf-8")

    analyzer = PythonAnalyzer()
    result = analyzer.analyze_file(SourceUnit.from_path(str(uuid.uuid4()), broken))

    assert not result.succeeded
    assert result.errors
    assert not result.symbols


def test_definitions_inside_if_try_and_with_blocks_are_extracted(tmp_path: Path) -> None:
    source = tmp_path / "m.py"
    source.write_text(
        "import sys\n\n"
        "if sys.platform == 'win32':\n    def top():\n        return 1\nelse:\n    def top():\n        return 2\n\n"
        "try:\n    import fast\n    def impl():\n        return fast.go()\nexcept ImportError:\n    def impl():\n        return 0\n\n\n"
        "class P:\n    if sys.platform == 'win32':\n        def run(self):\n            return 1\n"
        "    else:\n        def run(self):\n            return 2\n        LIMIT = 3\n",
        encoding="utf-8",
    )
    result = PythonAnalyzer().analyze_file(SourceUnit.from_path(str(uuid.uuid4()), source))

    found = sorted((s.qualified_name, s.kind.value, s.start_line) for s in result.symbols if s.kind != SymbolKind.MODULE)
    assert ("m.top", "function", 4) in found and ("m.top", "function", 7) in found  # すべての分岐の定義を抽出する
    assert [n for n, _, _ in found].count("m.impl") == 2 and [n for n, _, _ in found].count("m.P.run") == 2
    assert ("m.P.LIMIT", "class_variable", 26) in found

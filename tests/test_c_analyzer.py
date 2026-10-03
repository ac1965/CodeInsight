from __future__ import annotations

import uuid
from pathlib import Path

from codeinsight.analysis.language_adapter import SourceUnit
from codeinsight.analysis.c_analyzer import CAnalyzer
from codeinsight.domain import SymbolKind


def test_extracts_function_and_related_symbols(c_sample_dir: Path) -> None:
    analyzer = CAnalyzer()
    result = analyzer.analyze_file(SourceUnit.from_path(str(uuid.uuid4()), c_sample_dir / "util.c"))

    assert result.succeeded, result.errors
    kinds = {(s.name, s.kind) for s in result.symbols}

    assert ("add", SymbolKind.FUNCTION) in kinds
    assert ("factorial", SymbolKind.FUNCTION) in kinds
    assert ("call_count", SymbolKind.STATIC_VARIABLE) in kinds
    assert ("MAX_RETRY", SymbolKind.MACRO) in kinds


def test_extracts_struct_typedef_and_declaration_from_header(c_sample_dir: Path) -> None:
    analyzer = CAnalyzer()
    result = analyzer.analyze_file(SourceUnit.from_path(str(uuid.uuid4()), c_sample_dir / "util.h"))

    assert result.succeeded, result.errors
    kinds = {(s.name, s.kind) for s in result.symbols}

    assert ("Point", SymbolKind.STRUCT) in kinds
    assert ("Point", SymbolKind.TYPEDEF) in kinds
    assert ("add", SymbolKind.FUNCTION_DECLARATION) in kinds


def test_extracts_global_and_local_variables(c_sample_dir: Path) -> None:
    analyzer = CAnalyzer()
    result = analyzer.analyze_file(SourceUnit.from_path(str(uuid.uuid4()), c_sample_dir / "main.c"))

    kinds = {(s.name, s.kind) for s in result.symbols}
    assert ("global_counter", SymbolKind.GLOBAL_VARIABLE) in kinds
    assert ("origin", SymbolKind.LOCAL_VARIABLE) in kinds

    main_symbol = next(s for s in result.symbols if s.name == "main")
    origin_symbol = next(s for s in result.symbols if s.name == "origin")
    assert origin_symbol.parent_symbol_id == main_symbol.symbol_id


def test_warns_when_compile_commands_missing(c_sample_dir: Path) -> None:
    analyzer = CAnalyzer(compile_commands_dir=None)
    result = analyzer.analyze_file(SourceUnit.from_path(str(uuid.uuid4()), c_sample_dir / "util.c"))

    assert result.succeeded
    assert any("compile_commands.json" in w for w in result.warnings)


def test_syntax_error_is_recorded_as_failure(tmp_path: Path) -> None:
    broken = tmp_path / "broken.c"
    broken.write_text("int main( {\n", encoding="utf-8")

    analyzer = CAnalyzer()
    result = analyzer.analyze_file(SourceUnit.from_path(str(uuid.uuid4()), broken))

    assert not result.succeeded
    assert result.errors

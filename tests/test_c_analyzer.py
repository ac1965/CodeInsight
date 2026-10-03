from __future__ import annotations

import uuid
from pathlib import Path

from codeinsight.analysis.c_analyzer import CAnalyzer
from codeinsight.analysis.language_adapter import SourceUnit
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


def test_relative_paths_in_compile_commands_are_resolved_against_the_command_directory() -> None:
    from codeinsight.analysis.c_analyzer import _absolutize_paths

    args = ["-DHAVE_CONFIG_H", "-I.", "-I../include", "-I", "inc", "-isystem", "/abs/sys", "-include", "pre.h", "-Wall", "-std=c11"]
    assert _absolutize_paths(args, "/build/dir") == [
        "-DHAVE_CONFIG_H", "-I/build/dir", "-I/build/dir/../include", "-I", "/build/dir/inc",
        "-isystem", "/abs/sys", "-include", "/build/dir/pre.h", "-Wall", "-std=c11",
    ]
    assert _absolutize_paths(args, "") == args  # directory が無ければ変更しない


def test_out_of_tree_build_uses_generated_headers_and_interpolates_settings_for_unlisted_files(tmp_path: Path) -> None:
    import json

    src = tmp_path / "src"
    build = tmp_path / "build"
    src.mkdir()
    build.mkdir()
    (build / "config.h").write_text("#define LIMIT 3\n")  # configure が生成するヘッダー（ソースの外にある）
    (src / "a.c").write_text('#include <config.h>\n#include <stdarg.h>\nint a(void) { return LIMIT; }\n')
    (src / "b.c").write_text('#include <config.h>\nint b(void) { return LIMIT + 1; }\n')  # コンパイルDBに無い
    (build / "compile_commands.json").write_text(json.dumps([
        {"directory": str(build), "file": str(src / "a.c"), "arguments": ["/usr/bin/cc", "-DHAVE_CONFIG_H", "-I.", "-c", str(src / "a.c")]},
    ]))
    analyzer = CAnalyzer(compile_commands_dir=build)

    exact = analyzer.analyze_file(SourceUnit.from_path(str(uuid.uuid4()), src / "a.c"))
    assert exact.succeeded, exact.errors  # -I. が build/ を指すので config.h が見つかる。<stdarg.h> も見つかる
    assert [s.name for s in exact.symbols if s.kind.value == "function"] == ["a"] and not exact.warnings

    borrowed = analyzer.analyze_file(SourceUnit.from_path(str(uuid.uuid4()), src / "b.c"))
    assert borrowed.succeeded, borrowed.errors
    assert any("補間したものを使いました（推定）" in w and "b.c" in w for w in borrowed.warnings)  # 推定であることを注記する


def test_output_producing_options_are_removed_so_analysis_never_writes_files() -> None:
    from codeinsight.analysis.c_analyzer import _strip_output_options

    args = ["-MMD", "-MF", "deps/x.d", "-MT", "x.o", "-o", "x.o", "-MFdeps/y.d", "-DA=1", "-I.", "-c", "-Wall", "-pipe", "-Werror", "-Werror=format", "-pedantic-errors"]
    assert _strip_output_options(args) == ["-DA=1", "-I.", "-c", "-Wall"]

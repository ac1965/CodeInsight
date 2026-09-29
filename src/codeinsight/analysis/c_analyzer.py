from __future__ import annotations

import platform
import subprocess
import uuid
from pathlib import Path

import clang.cindex as cindex

from codeinsight.analysis.language_adapter import FileAnalysis
from codeinsight.domain import Language, Symbol, SymbolKind

_DEFAULT_ARGS = ["-std=c11"]


def _detect_default_args() -> list[str]:
    """compile_commands.jsonが無い場合の最小限のデフォルト引数を組み立てる。

    macOSでは標準ヘッダー（<stdio.h>等）の解決にSDKのsysrootが必要なため、
    `xcrun --show-sdk-path`（読み取り専用のメタデータ取得コマンド）で検出
    できればそれを付与する。検出できない場合はデフォルト引数のみを用い、
    その分の解析精度の制約はwarningsで別途通知する。
    """

    args = list(_DEFAULT_ARGS)
    if platform.system() != "Darwin":
        return args
    try:
        completed = subprocess.run(
            ["xcrun", "--show-sdk-path"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return args
    sdk_path = completed.stdout.strip()
    if completed.returncode == 0 and sdk_path:
        return ["-isysroot", sdk_path, *args]
    return args


_TAG_KINDS = {
    cindex.CursorKind.STRUCT_DECL: SymbolKind.STRUCT,
    cindex.CursorKind.UNION_DECL: SymbolKind.UNION,
    cindex.CursorKind.ENUM_DECL: SymbolKind.ENUM,
}


class CAnalyzer:
    """libclang (clang.cindex) を用いたC言語シンボル抽出器。

    compile_commands.json が渡されればそのコンパイルオプションを用いる。
    無い場合は最小限のデフォルト引数で解析し、その旨をwarningsに記録する
    （AGENTS.md 3.2.1: 解析精度に関する制約の明示）。

    既知の制約：

    * プリプロセッサの条件分岐（#ifdef等）は、実際にアクティブになった
      ブランチのみが解析対象になる。他ブランチの内容は抽出されない。
    * 関数ポインタ経由の間接呼び出しは、呼び出し先を推測で確定しない
      （Phase1では呼び出し関係自体を抽出しないため、そもそも対象外）。
    * 構造体のフィールドは、Phase1のシンボル抽出対象に含めない。
    """

    language = Language.C

    def __init__(self, compile_commands_dir: Path | None = None) -> None:
        self._compilation_database: cindex.CompilationDatabase | None = None
        if compile_commands_dir is not None:
            try:
                self._compilation_database = cindex.CompilationDatabase.fromDirectory(
                    str(compile_commands_dir)
                )
            except cindex.CompilationDatabaseError:
                self._compilation_database = None
        self._index = cindex.Index.create()
        self._default_args = _detect_default_args()

    def analyze_file(self, file_id: str, absolute_path: Path) -> FileAnalysis:
        result = FileAnalysis()
        args, precise = self._resolve_args(absolute_path)
        if not precise:
            result.warnings.append(
                "compile_commands.jsonが見つからないため、デフォルト引数で解析しました。"
                "解析精度に制約がある可能性があります。"
            )

        try:
            translation_unit = self._index.parse(
                str(absolute_path),
                args=args,
                options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
            )
        except cindex.TranslationUnitLoadError as exc:
            result.errors.append(f"構文解析に失敗しました: {exc}")
            return result

        fatal_diagnostics = [
            diag
            for diag in translation_unit.diagnostics
            if diag.severity >= cindex.Diagnostic.Error
        ]
        if fatal_diagnostics:
            for diag in fatal_diagnostics:
                result.errors.append(f"{diag.location}: {diag.spelling}")
            return result

        for diag in translation_unit.diagnostics:
            if diag.severity == cindex.Diagnostic.Warning:
                result.warnings.append(f"{diag.location}: {diag.spelling}")

        target_file = Path(str(absolute_path)).resolve()
        seen_locations: set[tuple[SymbolKind, str, int, int]] = set()

        def in_target_file(cursor: cindex.Cursor) -> bool:
            location_file = cursor.location.file
            return location_file is not None and Path(location_file.name).resolve() == target_file

        def visit(cursor: cindex.Cursor, parent_symbol: Symbol | None) -> None:
            for child in cursor.get_children():
                if not in_target_file(child):
                    continue
                symbol = self._build_symbol(file_id, child, parent_symbol)
                if symbol is None:
                    visit(child, parent_symbol)
                    continue
                # libclangは `typedef struct Tag {...} Name;` のようなパターンで
                # 同一箇所のタグ宣言を子カーソルとして重複して報告することがある。
                # 同一の種類・名前・行範囲が既出であれば重複として無視する。
                location_key = (symbol.kind, symbol.name, symbol.start_line, symbol.end_line)
                if location_key in seen_locations:
                    continue
                seen_locations.add(location_key)
                result.symbols.append(symbol)
                visit(child, symbol)

        visit(translation_unit.cursor, None)
        return result

    def _resolve_args(self, absolute_path: Path) -> tuple[list[str], bool]:
        if self._compilation_database is not None:
            commands = self._compilation_database.getCompileCommands(str(absolute_path))
            if commands:
                command = commands[0]
                args = list(command.arguments)[1:]  # コンパイラ実行ファイル名を除く
                args = [arg for arg in args if arg != command.filename]
                return args, True
        return list(self._default_args), False

    def _build_symbol(
        self, file_id: str, cursor: cindex.Cursor, parent_symbol: Symbol | None
    ) -> Symbol | None:
        kind = self._map_kind(cursor)
        if kind is None:
            return None
        name = cursor.spelling or "<anonymous>"
        parent_qualified = parent_symbol.qualified_name if parent_symbol else None
        qualified_name = f"{parent_qualified}::{name}" if parent_qualified else name
        extent = cursor.extent
        return Symbol(
            symbol_id=str(uuid.uuid4()),
            file_id=file_id,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            start_line=extent.start.line,
            end_line=extent.end.line,
            parent_symbol_id=parent_symbol.symbol_id if parent_symbol else None,
        )

    def _map_kind(self, cursor: cindex.Cursor) -> SymbolKind | None:
        ck = cursor.kind
        if ck == cindex.CursorKind.FUNCTION_DECL:
            return (
                SymbolKind.FUNCTION
                if cursor.is_definition()
                else SymbolKind.FUNCTION_DECLARATION
            )
        if ck in _TAG_KINDS:
            return _TAG_KINDS[ck] if cursor.is_definition() else None
        if ck == cindex.CursorKind.TYPEDEF_DECL:
            return SymbolKind.TYPEDEF
        if ck == cindex.CursorKind.MACRO_DEFINITION:
            return SymbolKind.MACRO
        if ck == cindex.CursorKind.VAR_DECL:
            semantic_parent = cursor.semantic_parent
            if (
                semantic_parent is not None
                and semantic_parent.kind == cindex.CursorKind.TRANSLATION_UNIT
            ):
                if cursor.storage_class == cindex.StorageClass.STATIC:
                    return SymbolKind.STATIC_VARIABLE
                return SymbolKind.GLOBAL_VARIABLE
            return SymbolKind.LOCAL_VARIABLE
        return None

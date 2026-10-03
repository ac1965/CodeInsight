from __future__ import annotations

import platform
import subprocess
from pathlib import Path

import clang.cindex as cindex

from codeinsight.analysis.ids import IdAllocator, build_symbol
from codeinsight.analysis.language_adapter import FileAnalysis, SourceUnit
from codeinsight.domain import (
    Dependency,
    DependencyKind,
    Language,
    Reference,
    ReferenceKind,
    ResolutionStatus,
    SourceLocation,
    Symbol,
    SymbolKind,
)

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


_TYPE_DECL_KINDS = frozenset(
    {
        cindex.CursorKind.STRUCT_DECL,
        cindex.CursorKind.UNION_DECL,
        cindex.CursorKind.ENUM_DECL,
        cindex.CursorKind.TYPEDEF_DECL,
    }
)

_TAG_KINDS = {
    cindex.CursorKind.STRUCT_DECL: SymbolKind.STRUCT,
    cindex.CursorKind.UNION_DECL: SymbolKind.UNION,
    cindex.CursorKind.ENUM_DECL: SymbolKind.ENUM,
}


_RESOLVE_CACHE: dict[str, str] = {}


def _resolved(name: str) -> str:
    """Path.resolve() はシステムコールを伴うため、カーソルごとの呼び出しをキャッシュする。"""

    cached = _RESOLVE_CACHE.get(name)
    if cached is None:
        cached = str(Path(name).resolve())
        _RESOLVE_CACHE[name] = cached
    return cached


class CAnalyzer:
    """libclang (clang.cindex) を用いたC言語シンボル抽出器。

    compile_commands.json が渡されればそのコンパイルオプションを用いる。
    無い場合は最小限のデフォルト引数で解析し、その旨をwarningsに記録する
    （AGENTS.md 3.2.1: 解析精度に関する制約の明示）。

    既知の制約：

    * プリプロセッサの条件分岐（#ifdef等）は、実際にアクティブになった
      ブランチのみが解析対象になる。他ブランチの内容は抽出されない。
    * 関数ポインタ経由の間接呼び出しは、呼び出し先を推測で確定せず、
      UNRESOLVED の呼び出し参照として記録する。
    * マクロ展開の内部で生じる呼び出し・参照は、展開後のASTに基づく位置で
      記録されるため、マクロ定義側の位置とは一致しない場合がある。
    * 構造体のフィールドは、シンボル抽出対象に含めない。
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

    def analyze_file(self, unit: SourceUnit) -> FileAnalysis:
        result = FileAnalysis()
        absolute_path = unit.absolute_path
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
                unsaved_files=[(str(absolute_path), unit.content)],
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

        target_name = str(Path(str(absolute_path)).resolve())
        project_root = self._project_root(unit)
        ids = IdAllocator(unit.file_id)
        seen_locations: set[tuple[SymbolKind, str, int, int]] = set()
        callee_locations: set[tuple[int, int]] = set()
        symbols_by_id: dict[str, Symbol] = {}

        def in_target_file(cursor: cindex.Cursor) -> bool:
            location_file = cursor.location.file
            return location_file is not None and _resolved(location_file.name) == target_name

        def location_of(cursor: cindex.Cursor) -> SourceLocation:
            extent = cursor.extent
            return SourceLocation(unit.file_id, extent.start.line, extent.end.line)

        def add_reference(
            kind: ReferenceKind,
            cursor: cindex.Cursor,
            source: Symbol,
            name: str,
            key: str | None,
            status: ResolutionStatus,
            note: str = "",
        ) -> None:
            # ローカル変数の初期化式などで生じた参照は、変数ではなく
            # それを含む関数からの参照として記録する（呼び出し元を関数にするため）。
            while source.kind == SymbolKind.LOCAL_VARIABLE and source.parent_symbol_id:
                parent = symbols_by_id.get(source.parent_symbol_id)
                if parent is None:
                    break
                source = parent
            start_line = cursor.extent.start.line
            result.references.append(
                Reference(
                    reference_id=ids.reference_id(
                        source.symbol_id, kind.value, key or name, start_line
                    ),
                    source_symbol_id=source.symbol_id,
                    target_name=name,
                    target_key=key,
                    reference_kind=kind,
                    source_location=location_of(cursor),
                    resolution_status=status,
                    note=note,
                )
            )

        def collect_reference(cursor: cindex.Cursor, source: Symbol | None) -> None:
            if source is None:
                return
            kind = cursor.kind
            if kind == cindex.CursorKind.CALL_EXPR:
                self._collect_call(cursor, source, callee_locations, add_reference)
            elif kind == cindex.CursorKind.DECL_REF_EXPR:
                self._collect_decl_ref(cursor, source, callee_locations, add_reference)
            elif kind == cindex.CursorKind.TYPE_REF:
                referenced = cursor.referenced
                if referenced is not None and referenced.kind in _TYPE_DECL_KINDS:
                    usr = referenced.get_usr()
                    if usr:
                        add_reference(
                            ReferenceKind.TYPE_USE,
                            cursor,
                            source,
                            referenced.spelling,
                            usr,
                            ResolutionStatus.UNRESOLVED,
                        )

        def visit(cursor: cindex.Cursor, parent_symbol: Symbol | None) -> None:
            for child in cursor.get_children():
                if not in_target_file(child):
                    continue
                symbol = self._build_symbol(ids, unit.file_id, child, parent_symbol)
                if symbol is None:
                    collect_reference(child, parent_symbol)
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
                symbols_by_id[symbol.symbol_id] = symbol
                visit(child, symbol)

        self._collect_includes(translation_unit, unit, project_root, in_target_file, ids, result)
        visit(translation_unit.cursor, None)
        return result

    @staticmethod
    def _project_root(unit: SourceUnit) -> Path:
        root = Path(unit.absolute_path)
        for _ in Path(unit.relative_path).parts:
            root = root.parent
        return root.resolve()

    def _collect_includes(
        self,
        translation_unit: cindex.TranslationUnit,
        unit: SourceUnit,
        project_root: Path,
        in_target_file,
        ids: IdAllocator,
        result: FileAnalysis,
    ) -> None:
        for cursor in translation_unit.cursor.get_children():
            if cursor.kind != cindex.CursorKind.INCLUSION_DIRECTIVE or not in_target_file(cursor):
                continue
            line = cursor.extent.start.line
            written = cursor.spelling
            included = cursor.get_included_file()
            key: str | None = None
            status = ResolutionStatus.UNRESOLVED
            note = ""
            if included is None:
                note = "インクルードされるファイルを特定できない"
            else:
                included_path = Path(included.name).resolve()
                try:
                    key = included_path.relative_to(project_root).as_posix()
                except ValueError:
                    status = ResolutionStatus.EXTERNAL
                    note = "プロジェクト外のヘッダー"
            result.dependencies.append(
                Dependency(
                    dependency_id=ids.dependency_id("include", written, line),
                    source_file_id=unit.file_id,
                    target_name=written,
                    target_key=key,
                    dependency_kind=DependencyKind.INCLUDE,
                    evidence_location=SourceLocation(unit.file_id, line, cursor.extent.end.line),
                    resolution_status=status,
                    note=note,
                )
            )

    @staticmethod
    def _callee_chain(call: cindex.Cursor) -> cindex.Cursor | None:
        """CALL_EXPRの呼び出し対象を表す式（DECL_REF_EXPR/MEMBER_REF_EXPR等）を返す。"""

        children = list(call.get_children())
        if not children:
            return None
        current = children[0]
        while current.kind in (
            cindex.CursorKind.UNEXPOSED_EXPR,
            cindex.CursorKind.PAREN_EXPR,
        ):
            inner = list(current.get_children())
            if not inner:
                break
            current = inner[0]
        return current

    def _collect_call(self, cursor, source, callee_locations, add_reference) -> None:
        callee = self._callee_chain(cursor)
        if callee is None:
            return
        if callee.kind == cindex.CursorKind.DECL_REF_EXPR:
            callee_locations.add((callee.extent.start.line, callee.extent.start.column))
            referenced = callee.referenced
            if referenced is not None and referenced.kind == cindex.CursorKind.FUNCTION_DECL:
                usr = referenced.get_usr()
                if usr:
                    add_reference(
                        ReferenceKind.CALL,
                        cursor,
                        source,
                        referenced.spelling,
                        usr,
                        ResolutionStatus.UNRESOLVED,
                    )
                    return
            note = "関数ポインタ経由の呼び出しであり、呼び出し先を静的に確定できない"
            name = callee.spelling or "<関数ポインタ>"
        elif callee.kind == cindex.CursorKind.MEMBER_REF_EXPR:
            note = "構造体メンバの関数ポインタ経由の呼び出しであり、呼び出し先を確定できない"
            name = callee.spelling or "<関数ポインタ>"
        else:
            note = "呼び出し対象が式の評価結果であり、静的に確定できない"
            name = callee.spelling or "<式>"
        add_reference(
            ReferenceKind.CALL, cursor, source, name, None, ResolutionStatus.UNRESOLVED, note
        )

    def _collect_decl_ref(self, cursor, source, callee_locations, add_reference) -> None:
        if (cursor.extent.start.line, cursor.extent.start.column) in callee_locations:
            return  # 呼び出しの対象として記録済み
        referenced = cursor.referenced
        if referenced is None:
            return
        usr = referenced.get_usr()
        if not usr:
            return
        if referenced.kind == cindex.CursorKind.FUNCTION_DECL:
            add_reference(
                ReferenceKind.FUNCTION_REF,
                cursor,
                source,
                referenced.spelling,
                usr,
                ResolutionStatus.UNRESOLVED,
            )
        elif referenced.kind == cindex.CursorKind.VAR_DECL:
            parent = referenced.semantic_parent
            if parent is not None and parent.kind == cindex.CursorKind.TRANSLATION_UNIT:
                add_reference(
                    ReferenceKind.VARIABLE_REF,
                    cursor,
                    source,
                    referenced.spelling,
                    usr,
                    ResolutionStatus.UNRESOLVED,
                )

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
        self,
        ids: IdAllocator,
        file_id: str,
        cursor: cindex.Cursor,
        parent_symbol: Symbol | None,
    ) -> Symbol | None:
        kind = self._map_kind(cursor)
        if kind is None:
            return None
        name = cursor.spelling or "<anonymous>"
        parent_qualified = parent_symbol.qualified_name if parent_symbol else None
        qualified_name = f"{parent_qualified}::{name}" if parent_qualified else name
        extent = cursor.extent
        return build_symbol(
            ids,
            file_id,
            name=name,
            qualified_name=qualified_name,
            kind=kind,
            start_line=extent.start.line,
            end_line=extent.end.line,
            parent=parent_symbol,
            usr=cursor.get_usr() or None,
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

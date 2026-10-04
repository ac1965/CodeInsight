from __future__ import annotations

import platform
import re
import subprocess
from pathlib import Path

import clang.cindex as cindex

from codeinsight.analysis.ids import KEY_SYSTEM_HEADER, IdAllocator, build_symbol
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


def _query_compiler(command: list[str]) -> str:
    """コンパイラの設定値だけを問い合わせる（読み取り専用。対象のコードはコンパイルも実行もしない）。"""

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _compiler_builtin_include_args() -> list[str]:
    """Linux等で、コンパイラ組み込みのヘッダー（<stddef.h>等）の場所を検出する。

    pipで入るlibclangはClangの組み込みヘッダーを含まないため、これが無いと
    <stdio.h>などを含むだけでC言語の解析が全体として失敗する。
    見つからない場合は何も足さず、その分の制約は解析結果のエラーとして表れる。
    """

    for command in (["clang", "-print-resource-dir"], ["gcc", "-print-file-name=include"], ["cc", "-print-file-name=include"]):
        found = _query_compiler(command)
        if not found:
            continue
        include_dir = Path(found) / "include" if command[1] == "-print-resource-dir" else Path(found)
        if include_dir.is_dir() and (include_dir / "stddef.h").exists():
            return ["-isystem", str(include_dir)]
    return []


_HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx"})
_INCLUDE_LINE = re.compile(r'^\s*#\s*include\s*["<]([^">]+)[">]', re.MULTILINE)

# 後ろにパスを取るオプション（`-I dir` のように別の引数で渡される形と、`-Idir` のように連結した形の両方がある）
_PATH_OPTIONS = ("-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-isysroot", "-F", "-iframework")


# 解析には不要で、ファイルを書き出す（または書き出そうとする）オプション。取り除く（対象の環境に書き込まない: AGENTS.md 1.1-4）
_OUTPUT_FLAGS = frozenset({"-M", "-MM", "-MD", "-MMD", "-MP", "-MG", "-save-temps", "-pipe"})
_OUTPUT_OPTIONS_WITH_VALUE = frozenset({"-o", "-MF", "-MT", "-MQ", "-MJ", "-Xclang"})


def _command_file(command: cindex.CompileCommand) -> str:
    """コンパイルコマンドの対象ファイルの絶対パス。`file` が相対パスの場合は `directory` 基準。"""

    path = Path(command.filename)
    return str((path if path.is_absolute() else Path(command.directory) / path).resolve())


def _strip_output_options(args: list[str]) -> list[str]:
    result: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
        elif arg in _OUTPUT_FLAGS:
            continue
        elif arg in _OUTPUT_OPTIONS_WITH_VALUE:
            skip_next = True
        elif arg.startswith(("-MF", "-MT", "-MQ", "-MJ")) and len(arg) > 3:
            continue  # -MFdeps/x.d のような連結形
        elif arg.startswith("-o") and len(arg) > 2 and not arg.startswith("-opt"):
            continue
        elif arg == "-pedantic-errors" or arg == "-Werror" or arg.startswith("-Werror="):
            continue  # 警告をエラーに格上げするビルド設定は、解析では警告のままにする（エラーは構文解析の失敗として扱うため）
        elif arg.startswith("-W") and not arg.startswith(("-Wp,", "-Wl,", "-Wa,")):
            continue  # 警告の選択は構文解析に影響しない。コンパイラ固有の警告（-Wshadow=local など）は、libclangが警告を出力して騒がしくなる
        else:
            result.append(arg)
    return result


def _absolutize_paths(args: list[str], directory: str) -> list[str]:
    """コンパイルコマンドの相対パスを、コマンドの `directory`（コンパイラを実行した場所）基準の絶対パスにする。

    解析は別の場所で行うため、`-I.` のような相対パスは、そのままだと別のディレクトリを指す。
    out-of-tree ビルド（生成された config.h がビルド用ディレクトリにある）で特に必要になる。
    """

    if not directory:
        return args
    base = Path(directory)

    def absolute(value: str) -> str:
        return value if Path(value).is_absolute() else str(base / value)

    result: list[str] = []
    pending = False
    for arg in args:
        if pending:
            result.append(absolute(arg))
            pending = False
            continue
        if arg in _PATH_OPTIONS:
            result.append(arg)
            pending = True
            continue
        for option in sorted(_PATH_OPTIONS, key=len, reverse=True):
            if arg.startswith(option) and len(arg) > len(option) and not arg.startswith("-include-"):
                result.append(option + absolute(arg[len(option):]))
                break
        else:
            result.append(arg)
    return result


def _system_include_args() -> list[str]:
    """標準ヘッダー（<stdio.h>等）を見つけるための、実行環境ごとの引数。

    pipで入るlibclangは、コンパイラが暗黙に行うシステムヘッダーの探索（macOSのSDK、組み込みヘッダー）を
    行わない。macOSでは `xcrun --show-sdk-path`、Linuxでは `clang`/`gcc` の設定値の問い合わせ
    （いずれも読み取り専用のメタデータ取得で、対象のコードはコンパイルも実行もしない）で補う。
    検出できない場合は何も足さず、その分の解析精度の制約は解析エラーとして表れる。
    """

    builtin = _compiler_builtin_include_args()  # <stdarg.h>・<stddef.h> など、コンパイラ組み込みのヘッダー
    if platform.system() != "Darwin":
        return builtin
    sdk_path = _query_compiler(["xcrun", "--show-sdk-path"])
    return [*(["-isysroot", sdk_path] if sdk_path else []), *builtin]


def _with_system_includes(args: list[str], system_args: list[str]) -> list[str]:
    """コンパイルコマンドに、システムヘッダーの探索に必要な引数を足す（指定済みなら足さない）。"""

    if not system_args or any(a in ("-nostdinc", "-isysroot", "--sysroot") or a.startswith("--sysroot=") for a in args):
        return args
    return [*system_args, *args]


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


def _first_line(text: str | None) -> str:
    """ドキュメントコメント（briefコメント）の先頭の非空行。無ければ空文字。"""

    for line in (text or "").splitlines():
        if line.strip():
            line = line.strip()
            return line if len(line) <= 160 else line[:159] + "…"
    return ""


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
        self._listed: set[str] | None = None
        self._includers: dict[str, list[tuple[Path, str]]] | None = None
        self._system_args = _system_include_args()
        self._default_args = [*self._system_args, *_DEFAULT_ARGS]

    def parse(self, unit: SourceUnit, result: FileAnalysis) -> cindex.TranslationUnit | None:
        """ファイルを構文解析する（解析時にも、関数単位の問い合わせ時にも同じ設定で使う）。

        ヘッダーは、取り込む側のソースの設定と文脈で解析する。警告・エラーは result に記録する。
        ファイル自身にエラーがある場合は None を返す（エラーのあるファイルを正常に解析したものとして扱わない）。
        """

        absolute_path = unit.absolute_path
        target_name = str(Path(str(absolute_path)).resolve())
        parse_path = absolute_path
        includer = self._includer_of(absolute_path) if absolute_path.suffix in _HEADER_SUFFIXES else None
        if includer is not None:
            # ヘッダーは、取り込む側のソースが前提とするマクロ・型（config.h など）を持たず、単独では解析できないことが多い。
            # コンパイルデータベース（configure とビルドの記録）にある、取り込む側のソースの設定と文脈で解析する。
            args, _ = self._resolve_args(includer)
            parse_path = includer
            result.warnings.append(
                f"ヘッダーを、取り込む側の{includer.name}のコンパイル設定と文脈で解析しました（推定。他の取り込み元では内容が異なる可能性があります）。"
            )
        else:
            args, args_warning = self._resolve_args(absolute_path)
            if args_warning:
                result.warnings.append(args_warning)

        try:
            translation_unit = self._index.parse(
                str(parse_path),
                args=args,
                unsaved_files=[(str(absolute_path), unit.content)],
                options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
            )
        except cindex.TranslationUnitLoadError as exc:
            result.errors.append(f"構文解析に失敗しました: {exc}")
            return None

        def own_diagnostic(diag: cindex.Diagnostic) -> bool:
            """取り込む側の文脈で解析した場合、ヘッダー自身の診断だけを、このファイルの成否に関わるものとして扱う。"""

            if parse_path == absolute_path:
                return True
            location_file = diag.location.file
            return location_file is not None and _resolved(location_file.name) == target_name

        fatal_diagnostics = [
            diag
            for diag in translation_unit.diagnostics
            if diag.severity >= cindex.Diagnostic.Error and own_diagnostic(diag)
        ]
        if fatal_diagnostics:
            for diag in fatal_diagnostics:
                result.errors.append(f"{diag.location}: {diag.spelling}")
            return None

        outside = 0
        for diag in translation_unit.diagnostics:
            if not own_diagnostic(diag):
                outside += diag.severity >= cindex.Diagnostic.Error
            elif diag.severity == cindex.Diagnostic.Warning:
                result.warnings.append(f"{diag.location}: {diag.spelling}")
        if outside:
            result.warnings.append(f"取り込む側の{parse_path.name}の解析で、ヘッダー以外の場所に{outside}件のエラーがありました（このヘッダーの結果には含めていません）。")
        return translation_unit

    def analyze_file(self, unit: SourceUnit) -> FileAnalysis:
        result = FileAnalysis()
        absolute_path = unit.absolute_path
        target_name = str(Path(str(absolute_path)).resolve())
        translation_unit = self.parse(unit, result)
        if translation_unit is None:
            return result

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

    @staticmethod
    def _function_key(referenced) -> str | None:
        """関数の照合キー（USR）。システムヘッダーで宣言された関数には、その印を付ける（標準ライブラリ関数など）。

        コンパイラが、最初の宣言（canonical）をシステムヘッダーに見つけた関数は、プロジェクトの外で定義される。
        プロジェクトの `.c` 内に同じ関数のプロトタイプ宣言があっても、それを「解決先」にはしない。
        """

        usr = referenced.get_usr()
        if not usr:
            return None
        try:
            if referenced.canonical.location.is_in_system_header or referenced.location.is_in_system_header:
                return usr + KEY_SYSTEM_HEADER
        except (AttributeError, ValueError):
            pass
        return usr

    def _collect_call(self, cursor, source, callee_locations, add_reference) -> None:
        callee = self._callee_chain(cursor)
        if callee is None:
            return
        if callee.kind == cindex.CursorKind.DECL_REF_EXPR:
            callee_locations.add((callee.extent.start.line, callee.extent.start.column))
            referenced = callee.referenced
            if referenced is not None and referenced.kind == cindex.CursorKind.FUNCTION_DECL:
                usr = self._function_key(referenced)
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
            usr = self._function_key(referenced) or usr
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

    def _includer_of(self, header: Path) -> Path | None:
        """compile_commands.json に載っているソースのうち、このヘッダーを `#include` しているもの（決定的に1つ）。

        取り込み元が複数ある場合は、同じディレクトリのもの、次にパス順で最初のものを選ぶ。
        """

        if self._compilation_database is None:
            return None
        if self._includers is None:
            self._includers = {}
            for listed in sorted(self._listed_files()):
                path = Path(listed)
                if path.suffix in _HEADER_SUFFIXES:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for match in _INCLUDE_LINE.finditer(text):
                    spec = match.group(1)
                    self._includers.setdefault(Path(spec).name, []).append((path, spec))
        candidates = []
        parts = header.resolve().parts
        for source, spec in self._includers.get(header.name, []):
            tail = [p for p in Path(spec).parts if p not in ("..", ".")]
            if tuple(parts[len(parts) - len(tail):]) == tuple(tail):
                candidates.append(source)
        if not candidates:
            return None
        return sorted(candidates, key=lambda c: (c.resolve().parent != header.resolve().parent, str(c)))[0]

    def _resolve_args(self, absolute_path: Path) -> tuple[list[str], str | None]:
        """解析に使う引数と、精度に関する注記（コンパイルコマンドが完全に対応していれば None）。

        1. compile_commands.json に当該ファイルのコマンドがあれば、それを使う。
        2. 無い場合、libclang は同じディレクトリなどの近隣のコマンドから補間したものを返す。ヘッダーや、
           そのビルドで対象外だったファイルが、隣のソースと同じ設定で書かれていることが多いため使うが、
           完全な設定ではないので、推定である旨を注記する。
        3. それも無ければ、デフォルト引数で解析する。
        """

        if self._compilation_database is None:
            return list(self._default_args), "compile_commands.jsonが見つからないため、デフォルト引数で解析しました。解析精度に制約がある可能性があります。"
        commands = self._compilation_database.getCompileCommands(str(absolute_path))
        if commands:
            if str(absolute_path.resolve()) in self._listed_files():
                return self._command_args(commands[0]), None
            return self._command_args(commands[0]), (
                f"compile_commands.jsonに{absolute_path.name}の設定が無いため、近隣のファイルのコンパイル設定"
                "（-I・-Dなど）から補間したものを使いました（推定）。解析精度に制約がある可能性があります。"
            )
        return list(self._default_args), (
            f"compile_commands.jsonに{absolute_path.name}の設定が無いため、デフォルト引数で解析しました。解析精度に制約がある可能性があります。"
        )

    def _command_args(self, command: cindex.CompileCommand) -> list[str]:
        args = list(command.arguments)[1:]  # コンパイラ実行ファイル名を除く
        args = [arg for arg in args if arg != command.filename]
        return _with_system_includes(_absolutize_paths(_strip_output_options(args), command.directory), self._system_args)

    def _listed_files(self) -> set[str]:
        """compile_commands.json に実際に載っているファイル（補間されたものを除く）。"""

        if self._listed is None:
            assert self._compilation_database is not None
            self._listed = {_command_file(c) for c in self._compilation_database.getAllCompileCommands() or []}
        return self._listed

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
            summary=_first_line(cursor.brief_comment),
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

from __future__ import annotations

import platform
import re
import subprocess
from pathlib import Path

import clang.cindex as cindex

from codeinsight.analysis.c_flow_analysis import _operator as _assign_operator
from codeinsight.analysis.ids import KEY_SYSTEM_HEADER, KEY_TEMPLATE, KEY_VIRTUAL, IdAllocator, build_symbol
from codeinsight.analysis.language_adapter import FileAnalysis, SourceUnit
from codeinsight.analysis.libclang_select import configure as configure_libclang
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

_DEFAULT_ARGS = ["-std=gnu11"]  # GNU 拡張を許す（`-std=c11` の厳密モードでは、glibc が setenv・getopt などの POSIX 関数を宣言せず、実際のCのコードが解析に失敗する）
_C23_ARG = "-std=gnu23"
_MISSING_HEADER_HINT = (
    "ヘッダーが見つからないため解析できません。ビルドで生成されるヘッダー（config.h など）が無いか、このホストに無いシステムヘッダーの可能性があります。"
    "対象の configure とビルドを（別の場所で）実行し、compile_commands.json を渡すと解決することがあります（自動では実行しません）。"
)
NOTE_FUNCTION_POINTER = "関数ポインタ経由の呼び出しであり、呼び出し先を静的に確定できない"
NOTE_FUNCTION_POINTER_MEMBER = "構造体メンバの関数ポインタ経由の呼び出しであり、呼び出し先を確定できない"


def _is_default_std(args: list[str]) -> bool:
    """規格を利用者（コンパイル設定）が指定せず、既定の C11 を使っているか。"""

    return _DEFAULT_ARGS[0] in args and not any(a.startswith(("-std=", "--std=")) and a != _DEFAULT_ARGS[0] for a in args)


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


_WRAPPER_KINDS = frozenset({cindex.CursorKind.UNEXPOSED_EXPR, cindex.CursorKind.PAREN_EXPR, cindex.CursorKind.CSTYLE_CAST_EXPR})


def _strip_wrappers(cursor: cindex.Cursor) -> cindex.Cursor:
    """暗黙の変換・括弧・キャストを外した、中身の式。"""

    while cursor.kind in _WRAPPER_KINDS:
        inner = [c for c in cursor.get_children() if c.kind != cindex.CursorKind.TYPE_REF]
        if len(inner) != 1:
            break
        cursor = inner[0]
    return cursor


def _slot_of(expression: cindex.Cursor) -> str | None:
    """関数ポインタを保持する「置き場」の照合キー（構造体のフィールド、またはグローバル/static変数のUSR）。特定できなければ None。

    ローカル変数・引数は、関数をまたいで代入と呼び出しを結び付けられないため、置き場にしない。
    """

    node = _strip_wrappers(expression)
    referenced = node.referenced
    if referenced is None:
        return None
    if node.kind == cindex.CursorKind.MEMBER_REF_EXPR and referenced.kind == cindex.CursorKind.FIELD_DECL:
        return referenced.get_usr() or None
    if node.kind == cindex.CursorKind.DECL_REF_EXPR:
        return _slot_of_declaration(referenced)
    return None


def _is_function_pointer(declaration: cindex.Cursor) -> bool:
    kind = declaration.type.get_canonical()
    return kind.kind == cindex.TypeKind.POINTER and kind.get_pointee().kind in (cindex.TypeKind.FUNCTIONPROTO, cindex.TypeKind.FUNCTIONNOPROTO)


def _slot_of_declaration(declaration: cindex.Cursor) -> str | None:
    """変数・引数の宣言から、置き場の照合キー。グローバル/static変数はUSR、関数ポインタ型のローカル変数は `local:`、
    関数ポインタ型の引数は `param:<関数のUSR>:<位置>`（その関数の呼び出し元が渡している関数を、結び付けるため）。"""

    if declaration.kind == cindex.CursorKind.VAR_DECL:
        parent = declaration.semantic_parent
        if parent is not None and parent.kind == cindex.CursorKind.TRANSLATION_UNIT:
            return declaration.get_usr() or None
        if _is_function_pointer(declaration):
            usr = declaration.get_usr()
            return f"local:{usr}" if usr else None
    elif declaration.kind == cindex.CursorKind.PARM_DECL and _is_function_pointer(declaration):
        function = declaration.semantic_parent
        if function is not None and function.kind == cindex.CursorKind.FUNCTION_DECL and function.get_usr():
            parameters = [c for c in function.get_children() if c.kind == cindex.CursorKind.PARM_DECL]
            for position, parameter in enumerate(parameters):
                if parameter == declaration:
                    return f"param:{function.get_usr()}:{position}"
    return None


def _function_refs_in(expression: cindex.Cursor) -> list[cindex.Cursor]:
    """式が（`&` を付けても付けなくても）関数の名前そのもの、または `c ? f : g` のような選択なら、その DECL_REF_EXPR（複数あれば全部）。"""

    node = _strip_wrappers(expression)
    if node.kind == cindex.CursorKind.CONDITIONAL_OPERATOR:
        parts = list(node.get_children())
        return [ref for branch in parts[1:] for ref in _function_refs_in(branch)]
    if node.kind == cindex.CursorKind.UNARY_OPERATOR:
        inner = list(node.get_children())
        if len(inner) == 1 and _assign_operator(node) == "&":
            node = _strip_wrappers(inner[0])
    if node.kind == cindex.CursorKind.DECL_REF_EXPR and node.referenced is not None and node.referenced.kind == cindex.CursorKind.FUNCTION_DECL:
        return [node]
    return []


def _is_null(node: cindex.Cursor) -> bool:
    """NULL・0・`(T *)0`（関数ポインタに何も入れない代入）。"""

    node = _strip_wrappers(node)
    if node.kind in (cindex.CursorKind.INTEGER_LITERAL, cindex.CursorKind.GNU_NULL_EXPR, cindex.CursorKind.CXX_NULL_PTR_LITERAL_EXPR):
        return True
    if node.kind == cindex.CursorKind.CSTYLE_CAST_EXPR:
        inner = [c for c in node.get_children() if c.kind != cindex.CursorKind.TYPE_REF]
        return len(inner) == 1 and _is_null(inner[0])
    return False


def _classify_value(expression: cindex.Cursor) -> tuple[list[cindex.Cursor], bool]:
    """関数ポインタへ代入される式の、(関数の名前の参照の一覧, 関数名でない値（変数・戻り値・引数の転送など）を含むか)。
    `c ? f : g` は枝ごとに分け、NULL は無視する。"""

    node = _strip_wrappers(expression)
    if node.kind == cindex.CursorKind.CONDITIONAL_OPERATOR:
        refs: list[cindex.Cursor] = []
        unknown = False
        for branch in list(node.get_children())[1:]:
            branch_refs, branch_unknown = _classify_value(branch)
            refs += branch_refs
            unknown = unknown or branch_unknown
        return refs, unknown
    refs = _function_refs_in(node)
    if refs:
        return refs, False
    return [], not _is_null(node)


def _field_usrs_in_order(init_list: cindex.Cursor) -> list[str]:
    """初期化子リストの型が構造体のとき、そのフィールドのUSRを宣言順に返す（位置による初期化の対応づけ用）。"""

    declaration = init_list.type.get_canonical().get_declaration()
    if declaration is None or declaration.kind != cindex.CursorKind.STRUCT_DECL:
        return []
    return [f.get_usr() for f in declaration.get_children() if f.kind == cindex.CursorKind.FIELD_DECL]


_TYPE_DECL_KINDS = frozenset(
    {
        cindex.CursorKind.STRUCT_DECL,
        cindex.CursorKind.UNION_DECL,
        cindex.CursorKind.ENUM_DECL,
        cindex.CursorKind.TYPEDEF_DECL,
        cindex.CursorKind.CLASS_DECL,
        cindex.CursorKind.CLASS_TEMPLATE,
        cindex.CursorKind.TYPE_ALIAS_DECL,
    }
)

_CLASS_KINDS = frozenset({cindex.CursorKind.CLASS_DECL, cindex.CursorKind.CLASS_TEMPLATE})
_METHOD_KINDS = frozenset({cindex.CursorKind.CXX_METHOD, cindex.CursorKind.CONSTRUCTOR, cindex.CursorKind.DESTRUCTOR, cindex.CursorKind.CONVERSION_FUNCTION})
_CALLABLE_KINDS = frozenset({cindex.CursorKind.FUNCTION_DECL, cindex.CursorKind.FUNCTION_TEMPLATE, *_METHOD_KINDS})
_CPP_DEFAULT_ARGS = ["-x", "c++", "-std=c++17"]


def _cpp_qualified_name(cursor: cindex.Cursor) -> str:
    """C++の修飾名（意味上の親の名前空間・クラスで修飾。無名の名前空間は含めない）。"""

    parts = [cursor.spelling or "<anonymous>"]
    semantic = cursor.semantic_parent
    while semantic is not None and semantic.kind != cindex.CursorKind.TRANSLATION_UNIT:
        if semantic.spelling:
            parts.append(semantic.spelling)
        semantic = semantic.semantic_parent
    return "::".join(reversed(parts))


def _cxx_include_args() -> list[str]:
    """コンパイラが使うC++標準ライブラリのヘッダーの場所（`<vector>` など）。コンパイラの設定値の問い合わせだけで、対象のコードは扱わない。

    pipで入るlibclangはC++標準ライブラリの場所を知らないため、`clang++`/`g++` に空の入力を前処理させて検索パスを得る（読み取り専用）。
    """

    for compiler in ("clang++", "g++", "c++"):
        try:
            completed = subprocess.run([compiler, "-E", "-x", "c++", "-v", "-"], input="", capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        paths: list[str] = []
        active = False
        for line in completed.stderr.splitlines():
            if line.startswith("#include <...> search starts here"):
                active = True
            elif line.startswith("End of search list"):
                break
            elif active and line.startswith(" "):
                path = line.strip().replace(" (framework directory)", "")
                if "(framework directory)" not in line and Path(path).is_dir():
                    paths += ["-isystem", path]
        if paths:
            return paths
    return []

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
    _is_cpp = False

    def __init__(self, compile_commands_dir: Path | None = None) -> None:
        configure_libclang()  # コンパイルデータベースの読み込み（libclang の使用）より前に行う
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
        if self._is_cpp:
            self._system_args = [*self._system_args, *_cxx_include_args()]
            self._default_args = [*self._system_args, *_CPP_DEFAULT_ARGS]
        else:
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

        def parse_with(parse_args: list[str]) -> cindex.TranslationUnit | None:
            try:
                return self._index.parse(
                    str(parse_path),
                    args=parse_args,
                    unsaved_files=[(str(absolute_path), unit.content)],
                    options=cindex.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD,
                )
            except cindex.TranslationUnitLoadError as exc:
                result.errors.append(f"構文解析に失敗しました: {exc}")
                return None

        translation_unit = parse_with(args)
        if translation_unit is None:
            return None

        def in_system_header(diag: cindex.Diagnostic) -> bool:
            try:
                return bool(diag.location.is_in_system_header)
            except (AttributeError, ValueError):
                return False

        def own_diagnostic(diag: cindex.Diagnostic) -> bool:
            """取り込む側の文脈で解析した場合、ヘッダー自身の診断だけを、このファイルの成否に関わるものとして扱う。

            システムヘッダーの中のエラー（libclangの版と、新しい標準ライブラリ・SDKの食い違いなど）は、対象のファイル自身の失敗とは
            しない。件数を警告に記録し、標準ライブラリの型・関数の解決が不完全になりうることを示す。
            """

            if in_system_header(diag):
                return False
            if parse_path == absolute_path:
                return True
            location_file = diag.location.file
            return location_file is not None and _resolved(location_file.name) == target_name

        def fatal_of(tu: cindex.TranslationUnit) -> list[cindex.Diagnostic]:
            return [d for d in tu.diagnostics if d.severity >= cindex.Diagnostic.Error and own_diagnostic(d)]

        fatal_diagnostics = fatal_of(translation_unit)
        if fatal_diagnostics and not self._is_cpp and _is_default_std(args):
            # 規格を指定しない（C11の既定）で失敗したときだけ、C23 として解析し直す。C23 でも失敗すれば、元のエラーを残す。
            retried = parse_with([*(a for a in args if a != _DEFAULT_ARGS[0]), _C23_ARG])
            if retried is not None and not fatal_of(retried):
                translation_unit, fatal_diagnostics = retried, []
                result.warnings.append(
                    "C11 では構文エラーになったため、C23 として解析しました（推定。コンパイル設定が無く、規格を確定できません）。"
                )
        if fatal_diagnostics:
            for diag in fatal_diagnostics:
                result.errors.append(f"{diag.location}: {diag.spelling}")
            if any("file not found" in d.spelling for d in fatal_diagnostics):
                result.errors.append(_MISSING_HEADER_HINT)
            return None

        outside = 0
        system_errors = 0
        for diag in translation_unit.diagnostics:
            if in_system_header(diag):
                system_errors += diag.severity >= cindex.Diagnostic.Error
            elif not own_diagnostic(diag):
                outside += diag.severity >= cindex.Diagnostic.Error
            elif diag.severity == cindex.Diagnostic.Warning:
                result.warnings.append(f"{diag.location}: {diag.spelling}")
        if system_errors:
            result.warnings.append(
                f"システムヘッダーの中にエラーが{system_errors}件ありました（libclangの版と、標準ライブラリ・SDKの版の食い違いなど）。"
                "このファイルの解析は続けましたが、標準ライブラリの型・関数の解決が不完全な可能性があります。"
            )
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
        symbols_by_usr: dict[str, Symbol] = {}  # C++: クラスの外で定義されたメソッドの親（クラス・名前空間）を探すため

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
            slot_key: str | None = None,
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
                    slot_key=slot_key,
                )
            )

        slot_hints: dict[tuple[int, int], str] = {}  # 関数名の式の位置 → それを代入・初期化している置き場

        current_source: list[Symbol | None] = [None]  # いま参照を集めている関数（不明な代入元の記録に使う）

        def hint_slot(slot: str | None, value: cindex.Cursor) -> None:
            if not slot:
                return
            refs, unknown = _classify_value(value)
            for function_ref in refs:
                slot_hints[(function_ref.extent.start.line, function_ref.extent.start.column)] = slot
            if unknown and current_source[0] is not None:
                # 関数名でない値が代入されている（変数・戻り値・引数の転送）。置き場の全部の候補は列挙できないため、目印の参照を残す
                add_reference(
                    ReferenceKind.SLOT_SOURCE, value, current_source[0], "<関数名でない値>", None, ResolutionStatus.UNRESOLVED,
                    "関数ポインタの置き場に、関数名でない値（変数・戻り値・引数の転送など）が代入されている", slot,
                )

        def hint_assignment(cursor: cindex.Cursor) -> None:
            children = list(cursor.get_children())
            if len(children) == 2 and _assign_operator(cursor) == "=":
                hint_slot(_slot_of(children[0]), children[1])

        def hint_declaration(cursor: cindex.Cursor) -> None:
            """`op f = mul;`（関数ポインタ型のローカル変数の初期化）。"""

            values = [c for c in cursor.get_children() if c.kind not in (cindex.CursorKind.TYPE_REF, cindex.CursorKind.PARM_DECL)]
            if values and cursor.semantic_parent is not None and cursor.semantic_parent.kind != cindex.CursorKind.TRANSLATION_UNIT:
                hint_slot(_slot_of_declaration(cursor), values[-1])

        def hint_call_arguments(cursor: cindex.Cursor) -> None:
            """直接呼び出しの実引数のうち、関数の名前そのものを、呼び出し先の引数の置き場に結び付ける（`apply(helper, v)`）。"""

            callee = cursor.referenced
            if callee is None or callee.kind != cindex.CursorKind.FUNCTION_DECL:
                return
            usr = self._function_key(callee)
            if not usr or usr.endswith(KEY_SYSTEM_HEADER):
                return  # 標準ライブラリなど、プロジェクトの外の関数に渡す関数は、置き場を持たない
            arguments = [a for a in cursor.get_children()][1:]  # 先頭は呼び出し対象の式
            parameters = [c for c in callee.get_children() if c.kind == cindex.CursorKind.PARM_DECL]
            for position, argument in enumerate(arguments):
                if position < len(parameters) and _is_function_pointer(parameters[position]):  # 関数ポインタ型の引数だけ
                    hint_slot(f"param:{usr}:{position}", argument)

        def hint_initializer(init_list: cindex.Cursor) -> None:
            fields = _field_usrs_in_order(init_list)
            position = 0
            for item in init_list.get_children():
                parts = list(item.get_children())
                if parts and parts[0].kind == cindex.CursorKind.MEMBER_REF and len(parts) == 2:  # 指示付き初期化 `.name = value`
                    designator = parts[0].referenced
                    slot = designator.get_usr() if designator is not None else None
                    hint_slot(slot or None, parts[1])
                    if slot in fields:
                        position = fields.index(slot) + 1
                else:  # 位置による初期化
                    if position < len(fields):
                        hint_slot(fields[position], item)
                    position += 1

        def collect_reference(cursor: cindex.Cursor, source: Symbol | None) -> None:
            if source is None:
                return
            current_source[0] = source
            kind = cursor.kind
            if kind == cindex.CursorKind.CALL_EXPR:
                hint_call_arguments(cursor)
            elif kind == cindex.CursorKind.VAR_DECL:
                hint_declaration(cursor)
            if kind == cindex.CursorKind.BINARY_OPERATOR:
                hint_assignment(cursor)
            elif kind == cindex.CursorKind.INIT_LIST_EXPR:
                hint_initializer(cursor)
            if kind == cindex.CursorKind.CALL_EXPR:
                self._collect_call(cursor, source, callee_locations, add_reference)
            elif kind == cindex.CursorKind.DECL_REF_EXPR:
                self._collect_decl_ref(cursor, source, callee_locations, add_reference, slot_hints)
            elif kind == cindex.CursorKind.CXX_BASE_SPECIFIER:
                base = cursor.referenced
                usr = base.get_usr() if base is not None else ""
                if usr:
                    add_reference(ReferenceKind.INHERITANCE, cursor, source, base.spelling, usr, ResolutionStatus.UNRESOLVED)
                else:
                    add_reference(ReferenceKind.INHERITANCE, cursor, source, cursor.spelling, None, ResolutionStatus.UNRESOLVED, "基底クラスを静的に確定できない（テンプレートの引数など）")
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
                symbol = self._build_symbol(ids, unit.file_id, child, parent_symbol, symbols_by_usr)
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
                if symbol.usr:
                    symbols_by_usr.setdefault(symbol.usr, symbol)
                if child.kind == cindex.CursorKind.VAR_DECL:
                    current_source[0] = symbol
                    hint_declaration(child)  # ローカルの関数ポインタの初期化（子の式を訪ねる前に、置き場を結び付ける）
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
            try:
                included = cursor.get_included_file()
            except AssertionError:  # バインディングは、見つからなかったファイル（null）を受け付けず、AssertionError で落ちる
                included = None
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
        if self._is_cpp:
            # C++: メンバ関数・コンストラクタ・演算子の呼び出しは、呼び出し式が参照する宣言から、呼び出し先を特定する。
            referenced = cursor.referenced
            if referenced is not None and referenced.kind in _CALLABLE_KINDS:
                usr = self._function_key(referenced)
                if usr and referenced.kind == cindex.CursorKind.FUNCTION_DECL and referenced.get_num_template_arguments() > 0 and not usr.endswith(KEY_SYSTEM_HEADER):
                    usr = KEY_TEMPLATE + _cpp_qualified_name(referenced)  # 関数テンプレートの特殊化
                if usr:
                    if referenced.kind in _METHOD_KINDS and referenced.is_virtual_method():
                        usr += KEY_VIRTUAL
                    for node in cursor.get_children():
                        callee_locations.add((node.extent.start.line, node.extent.start.column))
                    add_reference(ReferenceKind.CALL, cursor, source, referenced.spelling, usr, ResolutionStatus.UNRESOLVED)
                    return
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
            note = NOTE_FUNCTION_POINTER
            name = callee.spelling or "<関数ポインタ>"
        elif callee.kind == cindex.CursorKind.MEMBER_REF_EXPR:
            note = NOTE_FUNCTION_POINTER_MEMBER
            name = callee.spelling or "<関数ポインタ>"
        else:
            note = "呼び出し対象が式の評価結果であり、静的に確定できない"
            name = callee.spelling or "<式>"
        add_reference(
            ReferenceKind.CALL, cursor, source, name, None, ResolutionStatus.UNRESOLVED, note, _slot_of(callee)
        )

    def _collect_decl_ref(self, cursor, source, callee_locations, add_reference, slot_hints=None) -> None:
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
                slot_key=(slot_hints or {}).get((cursor.extent.start.line, cursor.extent.start.column)),
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
        while args and args[-1] == "--":  # ファイル名の前の区切り。ファイル名を除いた後に残ると、libclang が解析に失敗する
            args.pop()
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
        symbols_by_usr: dict[str, Symbol] | None = None,
    ) -> Symbol | None:
        kind = self._map_kind(cursor)
        if kind is None:
            return None
        name = cursor.spelling or "<anonymous>"
        parent_qualified = parent_symbol.qualified_name if parent_symbol else None
        qualified_name = f"{parent_qualified}::{name}" if parent_qualified else name
        base_classes: tuple[str, ...] = ()
        if self._is_cpp:
            # クラスの外で定義されたメソッド（`void A::f() {}`）は、意味上の親（クラス・名前空間）の名前で修飾し、親のシンボルに結び付ける。
            qualified_name = _cpp_qualified_name(cursor)
            if parent_symbol is None and cursor.semantic_parent is not None and symbols_by_usr:
                parent_symbol = symbols_by_usr.get(cursor.semantic_parent.get_usr() or "")
            if cursor.kind in _CLASS_KINDS:
                base_classes = tuple(c.spelling for c in cursor.get_children() if c.kind == cindex.CursorKind.CXX_BASE_SPECIFIER)
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
            base_classes=base_classes,
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
        if ck in (cindex.CursorKind.TYPEDEF_DECL, cindex.CursorKind.TYPE_ALIAS_DECL):
            return SymbolKind.TYPEDEF
        if self._is_cpp:
            if ck in _CLASS_KINDS:
                return SymbolKind.CLASS if cursor.is_definition() else None
            if ck == cindex.CursorKind.NAMESPACE:
                return SymbolKind.NAMESPACE
            if ck in _METHOD_KINDS or ck == cindex.CursorKind.FUNCTION_TEMPLATE:
                in_class = cursor.semantic_parent is not None and cursor.semantic_parent.kind in _CLASS_KINDS | {cindex.CursorKind.STRUCT_DECL}
                if not cursor.is_definition():
                    return SymbolKind.FUNCTION_DECLARATION
                return SymbolKind.METHOD if in_class or ck in _METHOD_KINDS else SymbolKind.FUNCTION
        if ck == cindex.CursorKind.MACRO_DEFINITION:
            return SymbolKind.MACRO
        if ck == cindex.CursorKind.VAR_DECL:
            semantic_parent = cursor.semantic_parent
            if (
                semantic_parent is not None
                and semantic_parent.kind in (cindex.CursorKind.TRANSLATION_UNIT, cindex.CursorKind.NAMESPACE)
            ):
                if cursor.storage_class == cindex.StorageClass.STATIC:
                    return SymbolKind.STATIC_VARIABLE
                return SymbolKind.GLOBAL_VARIABLE
            return SymbolKind.LOCAL_VARIABLE
        return None


class CppAnalyzer(CAnalyzer):
    """C++のシンボル抽出器（libclang）。Cと同じ仕組みで、クラス・名前空間・メソッド・継承・呼び出しを扱う。

    既知の制約（Cの制約に加えて）：

    * テンプレートは、定義そのもの（クラステンプレート・関数テンプレート）を抽出する。特殊化・インスタンス化ごとの
      呼び出し先は区別しない。テンプレートの引数に依存する呼び出しは、呼び出し先を確定できないものとして記録する。
    * 仮想関数の呼び出しは、宣言された関数に「推定」として解決する（実際の呼び出し先は、派生クラスのオーバーライドになりうる）。
    * 拡張子 `.h` のヘッダーは、拡張子だけではCかC++か区別できないため、Cとして扱う。C++のヘッダーは `.hpp`・`.hh`・`.hxx` を使う。
    * 関数単位の制御フロー・データフローは、Cのみに対応している。
    """

    language = Language.CPP
    _is_cpp = True

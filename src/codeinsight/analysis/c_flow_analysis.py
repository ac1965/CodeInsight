"""C言語の関数単位の制御フロー・データフロー・状態・終了経路・リスクの解析（Clang AST、決定論的）。

libclang の AST だけを用い、対象の関数のソースから静的に確認できる事実を取り出す。
Pythonの解析（flow_analysis）と同じく、流れ非依存（flow-insensitive）の近似であり、実行順序・到達可能性・値は
保証しない。次は追えない（確定として扱わない）:
  - ポインタを介した書き込み先の実体（エイリアス）、関数ポインタ経由の呼び出し
  - マクロ展開の内部、条件付きコンパイルで除外された箇所
  - 同名の変数が入れ子のスコープで再宣言された場合の区別（同じ名前としてまとめる）
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field

import clang.cindex as ci

from codeinsight.analysis import flow_analysis as fa

CK = ci.CursorKind

_EXIT_CALLS = frozenset({"exit", "_exit", "_Exit", "abort", "quick_exit", "err", "errx", "verr", "verrx"})  # err 系は常にプロセスを終了する（BSD/GNU）
_JUMP_CALLS = frozenset({"longjmp", "siglongjmp", "_longjmp"})
_ASSERT_CALLS = frozenset({"assert", "__assert_fail", "__assert_rtn", "__assert", "_assert", "__assert_perror_fail"})
# 呼び出し元の引数のうち、書き込み先になる引数の位置（libcの代表的な関数）
_MUTATING_ARGS = {
    "memset": (0,), "memcpy": (0,), "memmove": (0,), "bzero": (0,), "strcpy": (0,), "strncpy": (0,), "strcat": (0,),
    "strncat": (0,), "sprintf": (0,), "snprintf": (0,), "vsprintf": (0,), "vsnprintf": (0,), "fgets": (0,),
    "fread": (0,), "read": (1,), "recv": (1,), "recvfrom": (1,), "getline": (0,), "qsort": (0,), "gets": (0,),
}
_UNSAFE_LIBC = {
    "gets": "入力の長さを制限できない（常に危険）",
    "strcpy": "コピー先の大きさを確認しない",
    "strcat": "連結先の大きさを確認しない",
    "sprintf": "書き込み先の大きさを確認しない（snprintfを検討）",
    "vsprintf": "書き込み先の大きさを確認しない（vsnprintfを検討）",
    "tmpnam": "競合に弱い一時ファイル名（mkstempを検討）",
    "tempnam": "競合に弱い一時ファイル名（mkstempを検討）",
    "mktemp": "競合に弱い一時ファイル名（mkstempを検討）",
    "getwd": "バッファの大きさを確認しない",
    "alloca": "スタックを任意の大きさで消費する",
    "strtok": "再入不可（内部状態を持つ）",
}
_COMMAND_EXEC = frozenset({"system", "popen", "execl", "execlp", "execle", "execv", "execvp", "execvpe"})
_FORMAT_POSITION = {"printf": 0, "fprintf": 1, "sprintf": 1, "snprintf": 2, "syslog": 1, "dprintf": 1, "vprintf": 0}
_ALLOCATORS = frozenset({"malloc", "calloc", "realloc", "strdup", "strndup", "aligned_alloc", "reallocarray"})
_SCANF = frozenset({"scanf", "sscanf", "fscanf"})
_MARKERS = ("TODO", "FIXME", "XXX", "HACK")
_COMPOUND_OPS = frozenset({"+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="})
_ERROR_RETURN = re.compile(r"^(-\s*\d+|-\s*E[A-Z0-9_]+|NULL|\(\s*void\s*\*\s*\)\s*0|false|FALSE|0|-1)$")


# --- 取り出し ---


@dataclass
class CFunction:
    """解析対象の関数の定義（AST）と、その位置に対応するソース。"""

    cursor: ci.Cursor
    source: bytes
    name: str

    @property
    def lines(self) -> list[str]:
        return self.source.decode("utf-8", errors="replace").splitlines()


def _resolved(path: str) -> str:
    from pathlib import Path

    return str(Path(path).resolve())


_FUNCTION_KINDS = frozenset(
    {CK.FUNCTION_DECL, CK.CXX_METHOD, CK.CONSTRUCTOR, CK.DESTRUCTOR, CK.CONVERSION_FUNCTION, CK.FUNCTION_TEMPLATE}
)
_CONTAINER_KINDS = frozenset({CK.NAMESPACE, CK.CLASS_DECL, CK.STRUCT_DECL, CK.CLASS_TEMPLATE, CK.LINKAGE_SPEC, CK.UNION_DECL})


def find_function(translation_unit: ci.TranslationUnit, target_path: str, name: str, start_line: int, source: bytes) -> CFunction | None:
    """ファイル内の、名前と開始行が一致する関数定義を探す。

    C は、関数定義がファイル直下にある（ヘッダーの内部までは走査しない）。C++ は、名前空間・クラス（メソッド・コンストラクタ・
    デストラクタ・演算子・関数テンプレートを含む）の中も探す。
    """

    stack = list(reversed(list(translation_unit.cursor.get_children())))
    while stack:
        cursor = stack.pop()
        try:
            kind = cursor.kind
            if kind in _CONTAINER_KINDS:
                stack.extend(reversed(list(cursor.get_children())))
                continue
            if kind not in _FUNCTION_KINDS or not cursor.is_definition() or cursor.spelling != name:
                continue
        except ValueError:
            continue  # Python版libclangが知らない種類のカーソル（ライブラリのバージョン差）。関数定義ではない
        location = cursor.location.file
        if location is None or _resolved(location.name) != target_path:
            continue
        if cursor.extent.start.line == start_line:
            return CFunction(cursor, source, name)
    return None


@dataclass(frozen=True)
class CMacroUse:
    name: str
    line: int  # 最初の使用の行
    count: int
    definition_path: str  # マクロの定義のあるファイル（絶対パス。取れなければ空）
    definition_line: int
    body: str  # 定義のソース上の文字（引数リスト・本体。長ければ切り詰める）
    system: bool  # システムヘッダーで定義されたマクロ（標準ライブラリなど）


def macros_used(function: CFunction) -> list[CMacroUse]:
    """関数の中で使われているマクロ（呼び出し・展開される識別子）と、その定義。

    展開の内部（マクロが生む `if`・呼び出し・代入）は、展開後のASTに基づく位置で記録されるため、定義側の位置と対応しない。
    このため、**マクロの定義のソースを示して、展開の内容を読めるようにする**（展開結果そのものは作らない）。
    """

    tu = function.cursor.translation_unit
    file = function.cursor.location.file
    if file is None:
        return []
    target = _resolved(file.name)
    start, end = function.cursor.extent.start.line, function.cursor.extent.end.line
    found: dict[str, CMacroUse] = {}
    for node in tu.cursor.get_children():
        try:
            if node.kind != CK.MACRO_INSTANTIATION:
                continue
        except ValueError:
            continue
        location = node.location
        if location.file is None or _resolved(location.file.name) != target or not start <= location.line <= end:
            continue
        definition = node.referenced
        path, line, body, system = "", 0, "", False
        if definition is not None:
            if definition.location.file is not None:
                path, line = _resolved(definition.location.file.name), definition.location.line
            try:
                system = bool(definition.location.is_in_system_header)
            except (AttributeError, ValueError):
                system = False
            tokens = list(definition.get_tokens())[1:]  # 先頭は名前
            pieces: list[str] = []
            previous_end = None
            for token in tokens:  # ソース上の空白の有無を保って、つなぐ
                if previous_end is not None and token.extent.start.offset > previous_end:
                    pieces.append(" ")
                pieces.append(token.spelling)
                previous_end = token.extent.end.offset
            body = "".join(pieces).strip()
            body = body if len(body) <= 120 else body[:119] + "…"
        previous = found.get(node.spelling)
        found[node.spelling] = CMacroUse(node.spelling, previous.line if previous else location.line, (previous.count if previous else 0) + 1, path, line, body, system)
    return sorted(found.values(), key=lambda m: (m.system, m.line, m.name))


@dataclass(frozen=True)
class CFieldAccess:
    owner: str  # フィールドを持つ構造体・共用体・クラスの名前（無名なら <匿名>）
    line: int
    mode: str  # read / write / readwrite（複合代入・++/--）/ address（&で取られた。書き換えられうる）/ init（指示付き初期化 `.f = v`）
    text: str


def field_accesses(translation_unit: ci.TranslationUnit, target_path: str, source: bytes, field: str, owner: str | None = None) -> list[CFieldAccess]:
    """ファイルの中の、フィールド `field`（`owner` の指定があればその構造体のもの）の読み書き。

    書き込み: `=` の左辺（`p->f`・`s.f`・`p->f[i]` を含む）、複合代入・`++`/`--`（readwrite）、`&p->f`（address）、`.f = v` の初期化（init）。
    読み取り: それ以外の参照。位置による初期化（`{a, b}`）は、どのフィールドに入るかの対応を、ここでは追わない。
    """

    results: list[CFieldAccess] = []

    def line_text(cursor: ci.Cursor) -> str:
        start, end = cursor.extent.start.offset, cursor.extent.end.offset
        snippet = " ".join(source[start:end].decode("utf-8", errors="replace").split())
        return snippet if len(snippet) <= 80 else snippet[:79] + "…"

    def matches(node: ci.Cursor) -> str | None:
        declaration = node.referenced
        if declaration is None or declaration.kind != CK.FIELD_DECL or declaration.spelling != field:
            return None
        parent = declaration.semantic_parent
        name = parent.spelling if parent is not None and parent.spelling else "<匿名>"
        return name if owner is None or name == owner else None

    def visit(cursor: ci.Cursor, context: str, statement: ci.Cursor) -> None:
        kind = cursor.kind
        if kind == CK.MEMBER_REF_EXPR:
            name = matches(cursor)
            if name is not None:
                results.append(CFieldAccess(name, cursor.location.line, context, line_text(statement)))
            for child in _children(cursor):
                visit(child, "read", statement)
            return
        children = _children(cursor)
        if kind in (CK.BINARY_OPERATOR, CK.COMPOUND_ASSIGNMENT_OPERATOR) and len(children) == 2:
            operator = _operator(cursor)
            if operator == "=" or operator in _COMPOUND_OPS:
                visit(children[0], "write" if operator == "=" else "readwrite", cursor)
                visit(children[1], "read", cursor)
                return
        if kind == CK.UNARY_OPERATOR and children:
            operator = _operator(cursor)
            if operator in ("++", "--"):
                visit(children[0], "readwrite", cursor)
                return
            if operator == "&":
                visit(children[0], "address", cursor)
                return
        if kind == CK.INIT_LIST_EXPR:
            for item in children:
                parts = _children(item)
                if parts and parts[0].kind == CK.MEMBER_REF and len(parts) == 2:  # 指示付き初期化 `.f = v`
                    declaration = parts[0].referenced
                    if declaration is not None and declaration.kind == CK.FIELD_DECL and declaration.spelling == field:
                        parent = declaration.semantic_parent
                        name = parent.spelling if parent is not None and parent.spelling else "<匿名>"
                        if owner is None or name == owner:
                            results.append(CFieldAccess(name, parts[0].location.line, "init", line_text(item)))
                    visit(parts[1], "read", item)
                else:
                    visit(item, "read", item)
            return
        if kind in (CK.UNEXPOSED_EXPR, CK.PAREN_EXPR, CK.CSTYLE_CAST_EXPR, CK.ARRAY_SUBSCRIPT_EXPR) and context != "read" and children:
            visit(children[0], context, statement)  # `p->f[i] = v`・`(T)p->f` のように、書き込み先を包む式を通り抜ける
            for extra in children[1:]:
                visit(extra, "read", statement)
            return
        for child in children:
            visit(child, "read", cursor if kind in (CK.CALL_EXPR, CK.RETURN_STMT, CK.DECL_STMT, CK.IF_STMT) else statement)

    for top in translation_unit.cursor.get_children():
        try:
            location = top.location.file
        except ValueError:
            continue
        if location is None or _resolved(location.name) != target_path:
            continue
        visit(top, "read", top)
    seen: set[tuple[int, str, str]] = set()
    unique = []
    for access in sorted(results, key=lambda a: (a.line, a.mode)):
        key = (access.line, access.mode, access.owner)
        if key not in seen:
            seen.add(key)
            unique.append(access)
    return unique


def _function_refs_for(expression: ci.Cursor) -> list[ci.Cursor]:
    """式が関数の名前そのもの（`&f` を含む）なら、その参照。`c ? f : g` は全部。ほかは空。"""

    node = _unwrap(expression)
    while node.kind in (CK.UNEXPOSED_EXPR, CK.PAREN_EXPR, CK.CSTYLE_CAST_EXPR):
        inner = [c for c in _children(node) if c.kind != CK.TYPE_REF]
        if len(inner) != 1:
            break
        node = inner[0]
    if node.kind == CK.CONDITIONAL_OPERATOR:
        return [ref for branch in _children(node)[1:] for ref in _function_refs_for(branch)]
    if node.kind == CK.UNARY_OPERATOR and _operator(node) == "&":
        inner = _children(node)
        node = _unwrap(inner[0]) if inner else node
    return [node] if node.kind == CK.DECL_REF_EXPR and node.referenced is not None and node.referenced.kind == CK.FUNCTION_DECL else []


def text(function: CFunction, cursor: ci.Cursor, limit: int = 80) -> str:
    """カーソルの範囲のソースを、空白をまとめた1行で返す。"""

    start, end = cursor.extent.start.offset, cursor.extent.end.offset
    snippet = function.source[start:end].decode("utf-8", errors="replace")
    snippet = " ".join(snippet.split())
    return snippet if len(snippet) <= limit else snippet[: limit - 1] + "…"


def _children(cursor: ci.Cursor) -> list[ci.Cursor]:
    return list(cursor.get_children())


_OPERATOR_IN_GAP = re.compile(r"(<<=|>>=|&&|\|\||==|!=|<=|>=|<<|>>|[-+*/%&|^]=|[=<>+\-*/%&|^,])")


@functools.lru_cache(maxsize=32)
def _file_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def _gap_text(left: ci.Cursor, right: ci.Cursor) -> str:
    """2つの式の間にあるソース上の文字（演算子と括弧・空白）。同じファイルの中にある場合のみ。"""

    file = left.extent.end.file
    if file is None or right.extent.start.file is None or right.extent.start.file.name != file.name:
        return ""
    start, end = left.extent.end.offset, right.extent.start.offset
    if not 0 <= start < end <= start + 64:
        return ""
    return _file_bytes(file.name)[start:end].decode("utf-8", errors="replace")


def _operator(cursor: ci.Cursor) -> str:
    """二項演算子・代入演算子・単項演算子の記号（左辺の直後のトークン）。"""

    children = _children(cursor)
    if cursor.kind == CK.UNARY_OPERATOR:
        tokens = [t.spelling for t in cursor.get_tokens()]
        if not tokens:  # マクロが展開した演算子には、ソース上のトークンが無い
            return ""
        for op in ("++", "--", "&", "*", "-", "!", "~", "+"):
            if op in (tokens[0], tokens[-1]):
                return op
        return ""
    if len(children) < 2:
        return ""
    lhs_end = children[0].extent.end.offset
    for token in cursor.get_tokens():
        if token.extent.start.offset >= lhs_end and token.spelling != ")":
            return token.spelling
    # 左辺にマクロの呼び出しを含む式は、libclangがトークンを返さない。左辺と右辺の間のソースから読み取る
    gap = _gap_text(children[0], children[1])
    found = _OPERATOR_IN_GAP.search(gap.strip(" \t\r\n()"))
    return found.group(1) if found else ""


def call_name(cursor: ci.Cursor) -> str:
    """呼び出し式の関数名。マクロで別の関数に置き換わる場合（strcpy → __builtin___strcpy_chk）は、ソースに書かれた名前。"""

    tokens = list(cursor.get_tokens())
    name = tokens[0].spelling if tokens and re.match(r"^[A-Za-z_]\w*$", tokens[0].spelling) else (cursor.spelling or "<関数ポインタ>")
    fortified = re.match(r"^__builtin___(\w+?)_chk$", name)  # SDKのヘッダーが strcpy を __builtin___strcpy_chk に置き換える
    return fortified.group(1) if fortified else name


def _call_arguments(cursor: ci.Cursor) -> list[ci.Cursor]:
    """呼び出しの実引数（先頭の呼び出し先の式を除く）。"""

    children = _children(cursor)
    return children[1:] if children else []


def _unwrap(cursor: ci.Cursor) -> ci.Cursor:
    while cursor.kind in (CK.UNEXPOSED_EXPR, CK.PAREN_EXPR, CK.CSTYLE_CAST_EXPR) and _children(cursor):
        cursor = _children(cursor)[-1] if cursor.kind == CK.CSTYLE_CAST_EXPR else _children(cursor)[0]
    return cursor


def _variables_in(cursor: ci.Cursor) -> list[ci.Cursor]:
    """式に現れる変数（関数名は除く）の参照。"""

    found = []
    for node in cursor.walk_preorder():
        if node.kind == CK.DECL_REF_EXPR and node.referenced is not None and node.referenced.kind in (CK.VAR_DECL, CK.PARM_DECL):
            found.append(node)
    return found


def _is_ternary(cursor: ci.Cursor) -> bool:
    """ソースに `?` が書かれている三項演算子（ヘッダーのマクロが展開した三項演算子は除く）。"""

    if cursor.kind != CK.CONDITIONAL_OPERATOR:
        return False
    if any(t.spelling == "?" for t in cursor.get_tokens()):
        return True
    children = _children(cursor)  # 条件にマクロ呼び出しを含む場合は、トークンが取れない。条件と真の式の間のソースを見る
    return len(children) >= 2 and "?" in _gap_text(children[0], children[1])


def _is_literal_format(cursor: ci.Cursor) -> bool:
    """引数が、実行時に変わらない文字列（文字列リテラル、またはマクロが展開した文字列リテラルの連結）か。

    変数・引数・関数の戻り値を含む式は、リテラルではない。`cond ? "a" : "b"` のように、リテラルだけから成る式はリテラルとみなす。
    """

    has_string = False
    for node in cursor.walk_preorder():
        if node.kind == CK.STRING_LITERAL:
            has_string = True
        elif node.kind == CK.DECL_REF_EXPR and node.referenced is not None and node.referenced.kind in (CK.VAR_DECL, CK.PARM_DECL):
            return False
        elif node.kind == CK.CALL_EXPR:
            return False
    return has_string


def _is_exit_call(name: str) -> str:
    if name in _EXIT_CALLS:
        return "terminate"
    if name in _JUMP_CALLS:
        return "longjmp"
    if name in _ASSERT_CALLS:
        return "assert"
    return ""


# --- 制御フロー ---


_EVAL_READY: list[bool] = []


def _eval_int(cursor: ci.Cursor) -> int | None:
    """式が、コンパイル時に決まる整数なら、その値（libclang の定数評価）。マクロ・enum・`sizeof`・定数式を含む。決まらなければ None。"""

    import ctypes

    try:
        lib = ci.conf.lib
        if not _EVAL_READY:
            lib.clang_Cursor_Evaluate.argtypes = [ci.Cursor]
            lib.clang_Cursor_Evaluate.restype = ctypes.c_void_p
            lib.clang_EvalResult_getKind.argtypes = [ctypes.c_void_p]
            lib.clang_EvalResult_getKind.restype = ctypes.c_int
            lib.clang_EvalResult_getAsLongLong.argtypes = [ctypes.c_void_p]
            lib.clang_EvalResult_getAsLongLong.restype = ctypes.c_longlong
            lib.clang_EvalResult_dispose.argtypes = [ctypes.c_void_p]
            lib.clang_EvalResult_dispose.restype = None
            _EVAL_READY.append(True)
        handle = lib.clang_Cursor_Evaluate(cursor)
        if not handle:
            return None
        try:
            return int(lib.clang_EvalResult_getAsLongLong(handle)) if lib.clang_EvalResult_getKind(handle) == 1 else None  # CXEval_Int
        finally:
            lib.clang_EvalResult_dispose(handle)
    except Exception:  # noqa: BLE001 - 定数評価が使えない libclang では、定数として扱わない（確認できなかっただけ）
        return None


def _can_leave(body: ci.Cursor) -> bool:
    """ループの本体から抜ける手段（break・return・goto・exit 系の呼び出し・throw）があるか。"""

    for node in body.walk_preorder():
        if node.kind in (CK.BREAK_STMT, CK.RETURN_STMT, CK.GOTO_STMT, CK.CXX_THROW_EXPR):
            return True
        if node.kind == CK.CALL_EXPR and _is_exit_call(call_name(node)) in ("terminate", "longjmp"):
            return True
    return False


def analyze_return_values(function: CFunction) -> tuple[list[int], bool]:
    """関数の `return` の値のうち、定数に決まるものの一覧と、定数に決まらない `return` があるか（値を考慮した近似）。

    マクロ・enum・定数式は評価する。`return;`（値なし）は数えない。条件・呼び出しの結果などで決まる値は「決まらない」。
    """

    values: set[int] = set()
    unknown = False
    for node in function.cursor.walk_preorder():
        if node.kind != CK.RETURN_STMT:
            continue
        parts = _children(node)
        if not parts:
            continue
        value = _eval_int(parts[0])
        if value is None:
            unknown = True
        else:
            values.add(value)
    return sorted(values), unknown


def analyze_control_flow(function: CFunction) -> fa.ControlFlowSummary:
    summary = fa.ControlFlowSummary()
    counts: dict[str, int] = {}
    decisions = 0
    max_depth = 0
    lines = function.lines
    body = next((c for c in _children(function.cursor) if c.kind == CK.COMPOUND_STMT), None)

    def add(kind: str, cursor: ci.Cursor, depth: int, detail: str = "", line: int | None = None) -> None:
        nonlocal max_depth
        start = line or cursor.extent.start.line
        summary.items.append(fa.FlowItem(kind, start, max(cursor.extent.end.line, start), depth, detail))
        counts[kind] = counts.get(kind, 0) + 1
        max_depth = max(max_depth, depth)

    def header_text(cursor: ci.Cursor, body_cursor: ci.Cursor) -> str:
        start, end = cursor.extent.start.offset, body_cursor.extent.start.offset
        snippet = " ".join(function.source[start:end].decode("utf-8", errors="replace").split())
        snippet = re.sub(r"^(for|while)\s*", "", snippet)  # 種類は kind に出すので、キーワードは重ねて表示しない
        return snippet if len(snippet) <= 80 else snippet[:79] + "…"

    def expressions(cursor: ci.Cursor, depth: int) -> None:
        """文に含まれる式から、終了呼び出しと三項演算子を拾う（入れ子の文には入らない）。"""

        nonlocal decisions
        for node in cursor.walk_preorder():
            if node.kind == CK.CALL_EXPR:
                name = call_name(node)
                kind = _is_exit_call(name)
                if kind:
                    add("exit" if kind != "assert" else "assert", node, depth, f"{name}()")
            elif node.kind == CK.CXX_THROW_EXPR:
                add("raise", node, depth, text(function, node, 60))  # C++: throw（`throw;` は再送出）
            elif node.kind == CK.BINARY_OPERATOR and _operator(node) in ("&&", "||"):
                decisions += 1  # 条件に限らず、式の中の短絡評価も分岐として数える（拡張された循環的複雑度）
            elif _is_ternary(node):
                decisions += 1
                add("ternary", node, depth, text(function, node, 60))

    def visit_if(cursor: ci.Cursor, depth: int, elif_: bool) -> None:
        nonlocal decisions
        parts = _children(cursor)
        if len(parts) < 2:
            return
        condition, then = parts[0], parts[1]
        decisions += 1
        add("elif" if elif_ else "if", cursor, depth, text(function, condition))
        constant = _eval_int(condition)
        if constant is not None:
            where = cursor.extent.start.line
            if constant:
                summary.hints.append(fa.Hint("constant-condition", where, f"if ({text(function, condition, 50)}) は常に真（定数 {constant}）。else 側は実行されない"))
            else:
                summary.hints.append(fa.Hint("constant-condition", where, f"if ({text(function, condition, 50)}) は常に偽（定数 0）。本体は実行されない"))
        expressions(condition, depth + 1)
        visit(then, depth + 1)
        if len(parts) > 2:
            other = parts[2]
            if other.kind == CK.IF_STMT:
                visit_if(other, depth, True)
            else:
                else_line = cursor.extent.start.line
                for token in cursor.get_tokens():
                    if token.spelling == "else" and then.extent.end.offset <= token.extent.start.offset <= other.extent.start.offset:
                        else_line = token.extent.start.line
                add("else", other, depth, "", line=else_line)
                visit(other, depth + 1)

    def check_fallthrough(compound: ci.Cursor, depth: int) -> None:
        """case ラベルの文の並びが break/return/goto/continue で終わらず、次のラベルへ落ちる箇所。"""

        statements = _children(compound)
        label_positions = [i for i, s in enumerate(statements) if s.kind in (CK.CASE_STMT, CK.DEFAULT_STMT)]
        for order, position in enumerate(label_positions[:-1]):
            following = label_positions[order + 1]
            group = [statements[position], *statements[position + 1: following]]
            last = group[-1]
            if last.kind == CK.CASE_STMT or last.kind == CK.DEFAULT_STMT:
                inner = _children(last)
                last = inner[-1] if inner else last
            if last.kind in (CK.BREAK_STMT, CK.RETURN_STMT, CK.GOTO_STMT, CK.CONTINUE_STMT):
                continue
            if last.kind == CK.COMPOUND_STMT and _children(last) and _children(last)[-1].kind in (CK.BREAK_STMT, CK.RETURN_STMT, CK.GOTO_STMT, CK.CONTINUE_STMT):
                continue
            if last.kind == CK.CALL_EXPR and _is_exit_call(call_name(last)) == "terminate":
                continue
            region = " ".join(lines[statements[position].extent.start.line - 1: statements[following].extent.start.line])
            if re.search(r"fall\s*-?\s*through|FALLTHROUGH", region, re.IGNORECASE):
                continue  # 意図が注記されている
            summary.hints.append(fa.Hint("fallthrough", statements[following].extent.start.line, f"case を抜けて次のラベル（L{statements[following].extent.start.line}）へ続く"))

    def visit(cursor: ci.Cursor, depth: int) -> None:
        nonlocal decisions
        kind = cursor.kind
        if kind == CK.COMPOUND_STMT:
            for child in _children(cursor):
                visit(child, depth)
        elif kind == CK.IF_STMT:
            visit_if(cursor, depth, False)
        elif kind in (CK.FOR_STMT, CK.WHILE_STMT, CK.CXX_FOR_RANGE_STMT):
            parts = _children(cursor)
            body_cursor = parts[-1] if parts else cursor
            decisions += 1
            add("while" if kind == CK.WHILE_STMT else "for", cursor, depth, header_text(cursor, body_cursor))
            if kind == CK.WHILE_STMT and parts:
                constant = _eval_int(parts[0])
                if constant == 0:
                    summary.hints.append(fa.Hint("constant-condition", cursor.extent.start.line, f"while ({text(function, parts[0], 50)}) は常に偽。本体は実行されない"))
                elif constant is not None and not _can_leave(body_cursor):
                    summary.hints.append(fa.Hint("infinite-loop", cursor.extent.start.line, f"while ({text(function, parts[0], 50)}) は常に真で、本体に break・return・goto・exit 系・throw が無い（終わらない可能性）"))
            for part in parts[:-1]:
                expressions(part, depth + 1)
            visit(body_cursor, depth + 1)
        elif kind == CK.CXX_TRY_STMT:  # C++: try / catch（catch は、例外の型で選ばれる分岐として数える）
            parts = _children(cursor)
            add("try", cursor, depth)
            if parts:
                visit(parts[0], depth + 1)
            for handler in parts[1:]:
                if handler.kind != CK.CXX_CATCH_STMT:
                    continue
                decisions += 1
                inner = _children(handler)
                declaration = next((c for c in inner if c.kind == CK.VAR_DECL), None)
                caught = " ".join(declaration.type.spelling.split()) if declaration is not None else "..."
                add("except", handler, depth, caught)
                for part in inner:
                    if part.kind != CK.VAR_DECL:
                        visit(part, depth + 1)
        elif kind == CK.DO_STMT:
            parts = _children(cursor)
            decisions += 1
            add("do", cursor, depth, text(function, parts[-1]) if len(parts) > 1 else "")
            if len(parts) > 1:
                expressions(parts[-1], depth + 1)
            if parts:
                visit(parts[0], depth + 1)
        elif kind == CK.SWITCH_STMT:
            parts = _children(cursor)
            add("switch", cursor, depth, text(function, parts[0]) if parts else "")
            if parts:
                expressions(parts[0], depth + 1)
            for part in parts[1:]:
                if part.kind == CK.COMPOUND_STMT:
                    check_fallthrough(part, depth)
                visit(part, depth + 1)
        elif kind == CK.CASE_STMT:
            parts = _children(cursor)
            decisions += 1
            add("case", cursor, depth, text(function, parts[0]) if parts else "", line=cursor.extent.start.line)
            for part in parts[1:]:
                visit(part, depth + 1)
        elif kind == CK.DEFAULT_STMT:
            add("default", cursor, depth, "", line=cursor.extent.start.line)
            for part in _children(cursor):
                visit(part, depth + 1)
        elif kind == CK.RETURN_STMT:
            parts = _children(cursor)
            expression = text(function, parts[0]) if parts else ""
            add("return", cursor, depth, expression)
            if expression and _ERROR_RETURN.match(expression):
                summary.hints.append(fa.Hint("error-return", cursor.extent.start.line, f"return {expression}（エラー値の候補。呼び出し側が確認しているかは別）"))
            for part in parts:
                expressions(part, depth + 1)
        elif kind == CK.BREAK_STMT:
            add("break", cursor, depth)
        elif kind == CK.CONTINUE_STMT:
            add("continue", cursor, depth)
        elif kind == CK.GOTO_STMT:
            target = next((t.spelling for t in list(cursor.get_tokens())[1:] if re.match(r"^[A-Za-z_]\w*$", t.spelling)), "")
            add("goto", cursor, depth, target)
        elif kind == CK.LABEL_STMT:
            add("label", cursor, depth, cursor.spelling)
            for child in _children(cursor):
                visit(child, depth)
        else:
            expressions(cursor, depth)

    if body is not None:
        visit(body, 0)
    summary.hints.sort(key=lambda h: h.line)
    kinds = counts
    summary.metrics = {
        "lines": function.cursor.extent.end.line - function.cursor.extent.start.line + 1,
        "branches": kinds.get("if", 0) + kinds.get("elif", 0),
        "loops": kinds.get("for", 0) + kinds.get("while", 0) + kinds.get("do", 0),
        "switches": kinds.get("switch", 0), "cases": kinds.get("case", 0), "gotos": kinds.get("goto", 0),
        "ternaries": kinds.get("ternary", 0), "returns": kinds.get("return", 0), "exits": kinds.get("exit", 0), "asserts": kinds.get("assert", 0),
        "cyclomatic": decisions + 1, "max_depth": max_depth,
    }
    for label, key in (("try", "try_blocks"), ("except", "handlers"), ("raise", "raises")):
        if kinds.get(label):  # C++ の例外処理がある場合だけ載せる（C の結果に 0 を並べない）
            summary.metrics[key] = kinds[label]
    return summary


# --- 引数・戻り値 ---


@dataclass(frozen=True)
class CParameter:
    name: str
    type: str
    is_pointer: bool


def parameters(function: CFunction) -> list[CParameter]:
    result = []
    for child in _children(function.cursor):
        if child.kind == CK.PARM_DECL:
            kind = child.type.get_canonical().kind
            result.append(CParameter(child.spelling, child.type.spelling, kind in (ci.TypeKind.POINTER, ci.TypeKind.CONSTANTARRAY, ci.TypeKind.INCOMPLETEARRAY)))
    return result


def return_type(function: CFunction) -> str:
    return function.cursor.result_type.spelling


# --- データフロー ---


@dataclass
class CVariable(fa.VariableInfo):
    scope: str = "local"  # param / local / global / static


def _root_variable(cursor: ci.Cursor) -> ci.Cursor | None:
    """`p->a[i].b`・`*p` などの書き込み先から、元になる変数の参照を取り出す。"""

    node = cursor
    for _ in range(40):
        node = _unwrap(node)
        if node.kind == CK.DECL_REF_EXPR:
            return node if node.referenced is not None and node.referenced.kind in (CK.VAR_DECL, CK.PARM_DECL) else None
        if node.kind in (CK.MEMBER_REF_EXPR, CK.ARRAY_SUBSCRIPT_EXPR):
            children = _children(node)
            if not children:
                return None
            node = children[0]
        elif node.kind == CK.UNARY_OPERATOR and _operator(node) in ("*", "&"):
            children = _children(node)
            if not children:
                return None
            node = children[0]
        else:
            return None
    return None


def _this_member(cursor: ci.Cursor) -> ci.Cursor | None:
    """`x`・`this->x`・`x.y`・`x[i]`・`*x` の書き込み先が、メソッド自身のオブジェクトのメンバなら、そのメンバへの参照。"""

    node = cursor
    for _ in range(40):
        node = _unwrap(node)
        if node.kind == CK.MEMBER_REF_EXPR:
            children = _children(node)
            if not children:  # 暗黙の this
                return node if node.referenced is not None and node.referenced.kind == CK.FIELD_DECL else None
            first = _unwrap(children[0])
            if first.kind == CK.CXX_THIS_EXPR:
                return node
            node = children[0]
        elif node.kind in (CK.ARRAY_SUBSCRIPT_EXPR,) or (node.kind == CK.UNARY_OPERATOR and _operator(node) == "*"):
            children = _children(node)
            if not children:
                return None
            node = children[0]
        else:
            return None
    return None


def _store_kind(target: ci.Cursor) -> str:
    node = _unwrap(target)
    if node.kind == CK.MEMBER_REF_EXPR:
        return "attr_store"
    if node.kind == CK.ARRAY_SUBSCRIPT_EXPR:
        return "subscript_store"
    return "deref_store"


def _scope_of(declaration: ci.Cursor, function_cursor: ci.Cursor) -> str:
    if declaration.kind == CK.PARM_DECL:
        return "param"
    parent = declaration.semantic_parent
    if parent is not None and parent.kind == CK.TRANSLATION_UNIT:
        return "static" if declaration.storage_class == ci.StorageClass.STATIC else "global"
    if declaration.storage_class == ci.StorageClass.STATIC:
        return "static"
    return "local"


def analyze_variables(function: CFunction) -> dict[str, CVariable]:
    variables: dict[str, CVariable] = {}
    root = function.cursor

    def info(declaration: ci.Cursor) -> CVariable:
        name = declaration.spelling
        if name not in variables:
            variables[name] = CVariable(name, is_param=declaration.kind == CK.PARM_DECL, scope=_scope_of(declaration, root))
        return variables[name]

    def names_in(cursor: ci.Cursor) -> tuple[str, ...]:
        seen: list[str] = []
        for ref in _variables_in(cursor):
            if ref.spelling not in seen:
                seen.append(ref.spelling)
        return tuple(seen)

    def flow_sources(expression: ci.Cursor, kind_if_single: str, line: int, target: str) -> None:
        sources = _variables_in(expression)
        plain = _unwrap(expression).kind == CK.DECL_REF_EXPR
        for ref in sources:
            info(ref.referenced).flows.append(fa.Flow(kind_if_single if plain else "derive", line, target))

    for child in _children(root):
        if child.kind == CK.PARM_DECL:
            item = info(child)
            item.definitions.append(fa.Definition(child.extent.start.line, "param", "", ()))

    assignment_lhs: set[int] = set()  # 使用ではなく定義として数える左辺の位置（オフセット）

    def visit(cursor: ci.Cursor) -> None:
        kind = cursor.kind
        if kind == CK.VAR_DECL and cursor is not root:
            item = info(cursor)
            initializer = [c for c in _children(cursor) if c.kind != CK.TYPE_REF]
            if initializer:
                expression = initializer[-1]
                item.definitions.append(fa.Definition(cursor.extent.start.line, "decl_init", text(function, expression, 60), names_in(expression)))
                flow_sources(expression, "copy", cursor.extent.start.line, cursor.spelling)
            else:
                item.definitions.append(fa.Definition(cursor.extent.start.line, "decl", "", ()))
        elif kind in (CK.BINARY_OPERATOR, CK.COMPOUND_ASSIGNMENT_OPERATOR):
            operator = _operator(cursor)
            parts = _children(cursor)
            if len(parts) == 2 and (operator == "=" or operator in _COMPOUND_OPS):
                lhs, rhs = parts
                line = cursor.extent.start.line
                inner = _unwrap(lhs)
                if inner.kind == CK.DECL_REF_EXPR and inner.referenced is not None and inner.referenced.kind in (CK.VAR_DECL, CK.PARM_DECL):
                    item = info(inner.referenced)
                    how = "assign" if operator == "=" else "augassign"
                    item.definitions.append(fa.Definition(line, how, text(function, rhs, 60), names_in(rhs)))
                    if operator == "=":
                        assignment_lhs.add(inner.extent.start.offset)
                    flow_sources(rhs, "copy", line, inner.spelling)
                else:
                    base = _root_variable(lhs)
                    target_text = text(function, lhs, 50)
                    flow_sources(rhs, _store_kind(lhs), line, target_text)
                    if base is not None:
                        info(base.referenced).uses.append(base.extent.start.line)
                        assignment_lhs.add(base.extent.start.offset)
        elif kind == CK.UNARY_OPERATOR and _operator(cursor) in ("++", "--"):
            operand = _unwrap(_children(cursor)[0]) if _children(cursor) else None
            if operand is not None and operand.kind == CK.DECL_REF_EXPR and operand.referenced is not None and operand.referenced.kind in (CK.VAR_DECL, CK.PARM_DECL):
                info(operand.referenced).definitions.append(fa.Definition(cursor.extent.start.line, "incdec", text(function, cursor, 40), (operand.spelling,)))
        elif kind == CK.CALL_EXPR:
            name = call_name(cursor)
            call_text = text(function, cursor, 70)
            for position, argument in enumerate([] if name.startswith("__builtin_") else _call_arguments(cursor)):
                for ref in _variables_in(argument):
                    info(ref.referenced).flows.append(fa.Flow("call_arg", argument.extent.start.line, name, cursor.extent.start.line, call_text, position))
                inner = _unwrap(argument)
                if inner.kind == CK.UNARY_OPERATOR and _operator(inner) == "&":
                    target = _root_variable(inner)
                    if target is not None:
                        info(target.referenced).definitions.append(
                            fa.Definition(cursor.extent.start.line, "address_taken", f"&{target.spelling} を {name}() に渡す（呼び出し先で書き換えられる可能性）", ())
                        )
        elif kind == CK.RETURN_STMT:
            for part in _children(cursor):
                for ref in _variables_in(part):
                    info(ref.referenced).flows.append(fa.Flow("return", cursor.extent.start.line, "return"))
        elif kind == CK.DECL_REF_EXPR and cursor.referenced is not None and cursor.referenced.kind in (CK.VAR_DECL, CK.PARM_DECL):
            if cursor.extent.start.offset not in assignment_lhs:
                info(cursor.referenced).uses.append(cursor.extent.start.line)
        for child in _children(cursor):
            visit(child)

    for child in _children(root):
        if child.kind == CK.COMPOUND_STMT:
            visit(child)
    for item in variables.values():
        item.uses = sorted(set(item.uses))
        unique: dict[tuple, fa.Flow] = {}
        for flow in item.flows:  # マクロ展開で同じ引数が重複して現れる場合を1つにまとめる
            unique.setdefault((flow.kind, flow.line, flow.target, flow.position), flow)
        item.flows = list(unique.values())
    return variables


# --- 状態の変化 ---


@dataclass(frozen=True)
class CStateAccess:
    name: str
    scope: str  # global / static
    mode: str  # write / mutate / read
    line: int
    detail: str


@dataclass(frozen=True)
class CParameterMutation:
    parameter: str
    line: int
    how: str  # field_store / deref_store / subscript_store / incdec / libc_call
    detail: str


@dataclass
class CState:
    accesses: list[CStateAccess] = field(default_factory=list)
    parameter_mutations: list[CParameterMutation] = field(default_factory=list)


@dataclass(frozen=True)
class _Alias:
    """ローカルのポインタ変数が、関数の中の代入から指しうる先（流れ非依存の近似）。"""

    targets: tuple[tuple[str, str], ...]  # (指す先の変数名, global / static / param)
    unknown: bool  # 追えない値（関数の戻り値・フィールドの読み取りなど）も代入されている


def _decl_key(declaration: ci.Cursor) -> tuple[str, int]:
    return declaration.spelling, declaration.extent.start.offset


def _pointer_aliases(function: CFunction) -> dict[tuple[str, int], _Alias]:
    """関数の中で、ローカルのポインタ変数が指しうる先の対応表（流れ非依存・フィールド非依存のポインタ解析）。

    代入の左辺・初期化のすべての値を集め、和集合にする（`T *p = &g; ... p = &h;` なら g と h のどちらか）。扱う値: グローバル・static変数の
    アドレス・配列、ポインタ引数のコピー、別のローカルのポインタのコピー（連鎖をたどる）、`c ? a : b`、ポインタの加減算（同じオブジェクトの中）。
    NULL は無視する。関数の戻り値・フィールドの読み取り・ポインタ値を持つグローバル変数など、追えない値が1つでもあれば `unknown`
    （全部の先は列挙できない）。`&p` を取られた変数は、別の経路で書き換えられうるため、追わない。ローカル変数のアドレスは、状態として意味が無いため対象外。
    """

    root = function.cursor
    assignments: dict[tuple[str, int], list[ci.Cursor]] = {}
    tainted: set[tuple[str, int]] = set()
    declarations: dict[tuple[str, int], ci.Cursor] = {}

    def local_pointer(reference: ci.Cursor) -> ci.Cursor | None:
        declaration = reference.referenced
        if declaration is None or declaration.kind != CK.VAR_DECL or declaration.type.kind != ci.TypeKind.POINTER:
            return None
        return declaration if _scope_of(declaration, root) == "local" else None

    for node in root.walk_preorder():
        if node.kind == CK.VAR_DECL and node.type.kind == ci.TypeKind.POINTER and _scope_of(node, root) == "local":
            declarations[_decl_key(node)] = node
            initializers = [c for c in _children(node) if c.kind not in (CK.TYPE_REF, CK.PARM_DECL)]
            if initializers:
                assignments.setdefault(_decl_key(node), []).append(initializers[-1])
        elif node.kind in (CK.BINARY_OPERATOR, CK.COMPOUND_ASSIGNMENT_OPERATOR):
            parts = _children(node)
            target = _unwrap(parts[0]) if parts else None
            if target is not None and target.kind == CK.DECL_REF_EXPR and local_pointer(target) is not None:
                if node.kind == CK.BINARY_OPERATOR and _operator(node) == "=" and len(parts) == 2:
                    assignments.setdefault(_decl_key(target.referenced), []).append(parts[1])
                # `p += n`・`p -= n` は、同じオブジェクトの中の移動として、代入には数えない
        elif node.kind == CK.UNARY_OPERATOR and _operator(node) == "&":
            children = _children(node)
            inner = _unwrap(children[0]) if children else None
            if inner is not None and inner.kind == CK.DECL_REF_EXPR and local_pointer(inner) is not None:
                tainted.add(_decl_key(inner.referenced))

    def strip(value: ci.Cursor) -> ci.Cursor:
        value = _unwrap(value)
        while value.kind == CK.CSTYLE_CAST_EXPR:
            inner = [c for c in _children(value) if c.kind != CK.TYPE_REF]
            if len(inner) != 1:
                break
            value = _unwrap(inner[0])
        return value

    def resolve(value: ci.Cursor, seen: frozenset) -> tuple[set[tuple[str, str]], bool]:
        value = strip(value)
        if value.kind == CK.CONDITIONAL_OPERATOR:
            targets: set[tuple[str, str]] = set()
            unknown = False
            for branch in _children(value)[1:]:
                branch_targets, branch_unknown = resolve(branch, seen)
                targets |= branch_targets
                unknown = unknown or branch_unknown
            return targets, unknown
        if value.kind in (CK.INTEGER_LITERAL, CK.GNU_NULL_EXPR, CK.CXX_NULL_PTR_LITERAL_EXPR):
            return set(), False
        if value.kind == CK.BINARY_OPERATOR and _operator(value) in ("+", "-"):
            operands = _children(value)
            pointer_operands = [o for o in operands if strip(o).type.kind in (ci.TypeKind.POINTER, ci.TypeKind.CONSTANTARRAY, ci.TypeKind.INCOMPLETEARRAY)]
            return resolve(pointer_operands[0], seen) if len(pointer_operands) == 1 else (set(), True)
        takes_address = value.kind == CK.UNARY_OPERATOR and _operator(value) == "&"
        is_array = value.type.kind in (ci.TypeKind.CONSTANTARRAY, ci.TypeKind.INCOMPLETEARRAY)
        if value.kind == CK.DECL_REF_EXPR and local_pointer(value) is not None:  # 別のローカルのポインタのコピー
            key = _decl_key(value.referenced)
            if key in seen or key in tainted:
                return set(), key not in seen
            found: set[tuple[str, str]] = set()
            unknown = False
            for expression in assignments.get(key, []):
                part_targets, part_unknown = resolve(expression, seen | {key})
                found |= part_targets
                unknown = unknown or part_unknown
            return found, unknown
        base = _root_variable(value)
        if base is None or base.referenced is None:
            return set(), True
        scope = _scope_of(base.referenced, root)
        if scope in ("global", "static") and (takes_address or is_array):
            return {(base.spelling, scope)}, False
        if scope == "param" and value.kind == CK.DECL_REF_EXPR and base.referenced.type.kind == ci.TypeKind.POINTER:
            return {(base.spelling, scope)}, False
        if scope == "local" and (takes_address or is_array):
            return set(), False  # ローカル変数・ローカルの配列を指す（状態としては追わない）
        return set(), True

    aliases: dict[tuple[str, int], _Alias] = {}
    for key in declarations:
        if key in tainted or not assignments.get(key):
            continue
        targets: set[tuple[str, str]] = set()
        unknown = False
        for expression in assignments[key]:
            found_targets, found_unknown = resolve(expression, frozenset({key}))
            targets |= found_targets
            unknown = unknown or found_unknown
        if targets:
            aliases[key] = _Alias(tuple(sorted(targets)), unknown)
    return aliases


def _pointer_note(pointer: str, alias: _Alias) -> str:
    names = ", ".join(name for name, _ in alias.targets)
    if len(alias.targets) == 1 and not alias.unknown:
        return f"ポインタ {pointer} 経由（{pointer} が指す先は {names} だけと推定。他の経路での変更は追えない）"
    extra = "。ほかに、追えない値（関数の戻り値・フィールドの読み取りなど）も代入されており、全部は列挙できない" if alias.unknown else ""
    return f"ポインタ {pointer} 経由（{pointer} は {names} のいずれかを指しうる。流れ非依存のため、実際にどれかは分からない{extra}）"


def analyze_state(function: CFunction) -> CState:
    state = CState()
    root = function.cursor
    param_pointers = {p.name for p in parameters(function) if p.is_pointer}
    written_refs: set[int] = set()

    def classify(reference: ci.Cursor) -> str:
        declaration = reference.referenced
        return _scope_of(declaration, root) if declaration is not None else "local"

    aliases = _pointer_aliases(function)

    def record_write(target_expr: ci.Cursor, line: int, detail: str, mode: str = "write") -> None:
        base = _root_variable(target_expr)
        if base is None and _this_member(target_expr) is not None:
            # C++: メンバ変数への書き込み（`this->x = ...` と、暗黙の this による `x = ...`）。呼び出し元のオブジェクトの変更
            state.parameter_mutations.append(CParameterMutation("this", line, "field_store", detail))
            return
        if base is None or base.referenced is None:
            return
        scope = classify(base)
        written_refs.add(base.extent.start.offset)
        alias = aliases.get(_decl_key(base.referenced)) if scope == "local" and _unwrap(target_expr).kind != CK.DECL_REF_EXPR else None
        if alias is not None:  # `*p = ...`・`p->f = ...` で、p が指しうる先への書き込み（推定。複数ならそれぞれ「いずれかを指しうる」）
            detail = f"{detail} [{_pointer_note(base.spelling, alias)}]"
            destinations = list(alias.targets)
        else:
            destinations = [(base.spelling, scope)]
        for name, destination_scope in destinations:
            if destination_scope in ("global", "static"):
                state.accesses.append(CStateAccess(name, destination_scope, mode, line, detail))
            elif destination_scope == "param" and name in param_pointers and _unwrap(target_expr).kind != CK.DECL_REF_EXPR:
                node = _unwrap(target_expr)
                how = "field_store" if node.kind == CK.MEMBER_REF_EXPR else "subscript_store" if node.kind == CK.ARRAY_SUBSCRIPT_EXPR else "deref_store"
                state.parameter_mutations.append(CParameterMutation(name, line, how, detail))

    def visit(cursor: ci.Cursor) -> None:
        kind = cursor.kind
        if kind in (CK.BINARY_OPERATOR, CK.COMPOUND_ASSIGNMENT_OPERATOR):
            operator = _operator(cursor)
            parts = _children(cursor)
            if len(parts) == 2 and (operator == "=" or operator in _COMPOUND_OPS):
                record_write(parts[0], cursor.extent.start.line, text(function, cursor, 60))
        elif kind == CK.UNARY_OPERATOR and _operator(cursor) in ("++", "--"):
            children = _children(cursor)
            if children:
                record_write(children[0], cursor.extent.start.line, text(function, cursor, 60))
        elif kind == CK.CALL_EXPR:
            name = call_name(cursor)
            arguments = _call_arguments(cursor)
            for position in _MUTATING_ARGS.get(name, ()):
                if position < len(arguments):
                    argument = arguments[position]
                    base = _root_variable(argument)
                    if base is not None and base.referenced is not None:
                        scope = classify(base)
                        if scope in ("global", "static"):
                            state.accesses.append(CStateAccess(base.spelling, scope, "mutate", cursor.extent.start.line, f"{name}() の書き込み先"))
                        elif scope == "param" and base.spelling in param_pointers:
                            state.parameter_mutations.append(CParameterMutation(base.spelling, cursor.extent.start.line, "libc_call", f"{name}() の書き込み先"))
            for argument in arguments:
                inner = _unwrap(argument)
                if inner.kind == CK.UNARY_OPERATOR and _operator(inner) == "&":
                    base = _root_variable(inner)
                    if base is not None and base.referenced is not None and classify(base) in ("global", "static"):
                        state.accesses.append(CStateAccess(base.spelling, classify(base), "mutate", cursor.extent.start.line, f"&{base.spelling} を {name}() に渡す（書き換えられる可能性）"))
        for child in _children(cursor):
            visit(child)

    for child in _children(root):
        if child.kind == CK.COMPOUND_STMT:
            visit(child)
    # 読み取り: 書き込み先でないグローバル・静的変数の参照
    seen_reads: set[tuple[str, int]] = set()
    for node in root.walk_preorder():
        if node.kind == CK.DECL_REF_EXPR and node.referenced is not None and node.referenced.kind == CK.VAR_DECL:
            scope = classify(node)
            if scope in ("global", "static") and node.extent.start.offset not in written_refs and (node.spelling, node.extent.start.line) not in seen_reads:
                seen_reads.add((node.spelling, node.extent.start.line))
                state.accesses.append(CStateAccess(node.spelling, scope, "read", node.extent.start.line, ""))
    state.accesses.sort(key=lambda a: (a.line, a.name))
    return state


# --- 終了・失敗の経路（Cには例外が無い。終了・エラー戻り値・errno） ---


@dataclass(frozen=True)
class CExit:
    kind: str  # terminate / longjmp / assert / error_return / errno / throw（C++）
    line: int
    detail: str
    caught_locally: bool = False  # throw: 同じ関数の try の catch で捕捉されうる（型の一致か catch(...)。呼び出し元の catch は考慮しない）


def _bare_type(spelling: str) -> str:
    """catch・throw の型の比較用（const・参照・空白を除く）。"""

    return re.sub(r"\bconst\b|[&\s]", "", spelling)


def _try_scopes(function: CFunction) -> list[tuple[int, int, list[str]]]:
    """関数内の try ブロックの (本体の開始位置, 終了位置, catch する型の一覧)。`catch (...)` は "..."。"""

    scopes = []
    for node in function.cursor.walk_preorder():
        if node.kind != CK.CXX_TRY_STMT:
            continue
        parts = _children(node)
        if not parts:
            continue
        caught = []
        for handler in parts[1:]:
            if handler.kind == CK.CXX_CATCH_STMT:
                declaration = next((c for c in _children(handler) if c.kind == CK.VAR_DECL), None)
                caught.append(_bare_type(declaration.type.spelling) if declaration is not None else "...")
        scopes.append((parts[0].extent.start.offset, parts[0].extent.end.offset, caught))
    return scopes


def analyze_exits(function: CFunction) -> list[CExit]:
    exits: list[CExit] = []
    scopes = _try_scopes(function)
    for node in function.cursor.walk_preorder():
        if node.kind == CK.CXX_THROW_EXPR:
            operands = _children(node)
            thrown = _bare_type(operands[0].type.spelling) if operands else ""
            enclosing = [caught for start, end, caught in scopes if start <= node.extent.start.offset < end]
            local = bool(thrown) and any("..." in c or thrown in c for c in enclosing)
            if not operands:  # `throw;`: 捕捉した例外の再送出
                local = False
            label = f"throw {operands[0].type.spelling}" if operands else "throw;（捕捉した例外の再送出）"
            exits.append(CExit("throw", node.extent.start.line, label + ("（同じ関数の catch で捕捉されうる）" if local else ""), local))
        elif node.kind == CK.CALL_EXPR:
            name = call_name(node)
            kind = _is_exit_call(name)
            if kind:
                exits.append(CExit(kind, node.extent.start.line, f"{name}()"))
        elif node.kind == CK.RETURN_STMT:
            parts = _children(node)
            expression = text(function, parts[0]) if parts else ""
            if expression and _ERROR_RETURN.match(expression):
                exits.append(CExit("error_return", node.extent.start.line, f"return {expression}"))
        elif node.kind == CK.BINARY_OPERATOR and _operator(node) == "=":
            parts = _children(node)
            if parts and _unwrap(parts[0]).kind == CK.DECL_REF_EXPR and parts[0].spelling == "errno":
                exits.append(CExit("errno", node.extent.start.line, text(function, node, 50)))
    exits.sort(key=lambda e: (e.line, e.kind))
    return exits


def return_statements(function: CFunction) -> list[tuple[int, str]]:
    result = []
    for node in function.cursor.walk_preorder():
        if node.kind == CK.RETURN_STMT:
            parts = _children(node)
            result.append((node.extent.start.line, text(function, parts[0]) if parts else ""))
    return result


def environment_reads(function: CFunction) -> list[tuple[int, str, str]]:
    """getenv("NAME") の読み取り: (行, 名前, 呼び出し)。名前が文字列リテラルでない場合は空の名前。"""

    result = []
    for node in function.cursor.walk_preorder():
        if node.kind == CK.CALL_EXPR and call_name(node) in ("getenv", "secure_getenv"):
            arguments = _call_arguments(node)
            literal = text(function, arguments[0]) if arguments else ""
            name = literal.strip('"') if literal.startswith('"') else ""
            result.append((node.extent.start.line, name, text(function, node, 50)))
    return result


# --- リスク ---


@dataclass(frozen=True)
class CRiskHit:
    rule: str
    line: int
    detail: str


def scan_risks(function: CFunction) -> list[CRiskHit]:
    hits: list[CRiskHit] = []
    root = function.cursor

    allocations: dict[str, int] = {}
    for node in root.walk_preorder():
        if node.kind == CK.CALL_EXPR:
            name = call_name(node)
            line = node.extent.start.line
            arguments = _call_arguments(node)
            if name in _UNSAFE_LIBC:
                hits.append(CRiskHit("unsafe-libc", line, f"{name}(): {_UNSAFE_LIBC[name]}"))
            if name in _SCANF:
                fmt_index = 1 if name in ("sscanf", "fscanf") else 0
                fmt = text(function, arguments[fmt_index]) if len(arguments) > fmt_index else ""
                if re.search(r"%(?!\*)(?:\d+)?s", fmt) and not re.search(r"%\d+s", fmt):
                    hits.append(CRiskHit("unsafe-libc", line, f"{name}(): %s に幅の指定が無い（入力が書き込み先を超える恐れ）"))
            if name in _COMMAND_EXEC:
                literal = bool(arguments) and text(function, arguments[0]).startswith('"')
                hits.append(CRiskHit("command-exec", line, f"{name}(){'（引数は文字列リテラル）' if literal else '（引数が文字列リテラルでない）'}"))
            if name in _FORMAT_POSITION and len(arguments) > _FORMAT_POSITION[name]:
                argument = arguments[_FORMAT_POSITION[name]]
                if not _is_literal_format(argument):
                    hits.append(CRiskHit("format-string", line, f"{name}() の書式が文字列リテラルでない: {text(function, argument) or '（マクロの内部）'}"))
        elif node.kind == CK.CXX_CATCH_STMT:  # C++: 例外の握りつぶし（本体が空、または catch (...) が再送出していない）
            inner = _children(node)
            declaration = next((c for c in inner if c.kind == CK.VAR_DECL), None)
            body = next((c for c in inner if c.kind == CK.COMPOUND_STMT), None)
            caught = " ".join(declaration.type.spelling.split()) if declaration is not None else "..."
            if body is not None and not _children(body):
                hits.append(CRiskHit("swallowed-exception", node.extent.start.line, f"catch ({caught}) の本体が空（例外を握りつぶしている）"))
            elif declaration is None and body is not None and not any(n.kind == CK.CXX_THROW_EXPR for n in body.walk_preorder()):
                hits.append(CRiskHit("swallowed-exception", node.extent.start.line, "catch (...) が、再送出（throw;）せずに例外を捕捉している（意図的な場合あり）"))
        elif node.kind == CK.VAR_DECL or (node.kind == CK.BINARY_OPERATOR and _operator(node) == "="):
            parts = _children(node)
            target_name = node.spelling if node.kind == CK.VAR_DECL else (parts[0].spelling if parts else "")
            value = parts[-1] if parts else None
            if value is not None and target_name:
                for sub in value.walk_preorder():
                    if sub.kind == CK.CALL_EXPR and call_name(sub) in _ALLOCATORS:
                        allocations.setdefault(target_name, sub.extent.start.line)
                        break

    conditions: list[str] = []
    for node in root.walk_preorder():
        if node.kind in (CK.IF_STMT, CK.WHILE_STMT, CK.FOR_STMT, CK.DO_STMT) or _is_ternary(node):
            for part in _children(node)[:1] if node.kind != CK.DO_STMT else _children(node)[-1:]:
                conditions.append(text(function, part, 400))
        elif node.kind == CK.CALL_EXPR and call_name(node) in _ASSERT_CALLS:
            conditions.append(text(function, node, 400))
    for variable, line in allocations.items():
        if not any(re.search(rf"\b{re.escape(variable)}\b", condition) for condition in conditions):
            hits.append(CRiskHit("unchecked-alloc", line, f"{variable} への割り当ての結果を条件で確認していない"))

    for token in root.get_tokens():
        if token.kind == ci.TokenKind.COMMENT and any(marker in token.spelling for marker in _MARKERS):
            hits.append(CRiskHit("todo-marker", token.extent.start.line, " ".join(token.spelling.split())[:80]))

    summary = analyze_control_flow(function)
    for hint in summary.hints:
        if hint.kind == "fallthrough":
            hits.append(CRiskHit("fallthrough", hint.line, hint.detail))
    hits.sort(key=lambda h: (h.line, h.rule))
    return hits

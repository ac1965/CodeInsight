"""C/C++ の設定値（環境変数・コマンドライン引数・設定ファイル・定数マクロ）と、入口・境界（シグナル・atexit・スレッド・ネットワーク）を、
Clang AST から洗い出す（読み取り専用。対象はコンパイルも実行もしない）。

名前・パスは、文字列リテラルで書かれたものだけが対象（動的に組み立てられた名前は `<動的>`）。設定ファイル・コマンドライン引数の一部は、
名前や形による推定（confidence = inferred）。登録される関数は、名前（関数の参照）が書かれているものだけを結び付ける。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import clang.cindex as ci

from codeinsight.analysis.c_flow_analysis import (
    _CONTAINER_KINDS,
    _FUNCTION_KINDS,
    _call_arguments,
    _children,
    _function_refs_for,
    _resolved,
    _unwrap,
    call_name,
)

CK = ci.CursorKind

_ENV_READ = frozenset({"getenv", "secure_getenv", "getenv_s"})
_ENV_WRITE = frozenset({"setenv", "putenv", "unsetenv", "_putenv"})
_OPEN_CALLS = frozenset({"fopen", "freopen", "open", "openat", "access", "stat", "fopen64"})
_CONFIG_PATH = re.compile(r"(\.(conf|cfg|ini|json|ya?ml|toml|rc|env|properties)$)|^/etc/|(^|/)\.[A-Za-z0-9_]+rc$", re.IGNORECASE)
_STRING_COMPARE = frozenset({"strcmp", "strncmp", "strcasecmp", "strncasecmp", "g_strcmp0"})
_LONG_OPTION = re.compile(rb'\{\s*"([A-Za-z0-9][\w-]*)"\s*,\s*(no_argument|required_argument|optional_argument|[012])\s*,')
_THREAD_CREATE = {"pthread_create": 2, "thrd_create": 1}
_ATEXIT = frozenset({"atexit", "on_exit", "at_quick_exit", "pthread_atfork"})
_NETWORK = frozenset({"socket", "bind", "listen", "accept", "accept4", "connect"})
_SIGNAL_FIELDS = frozenset({"sa_handler", "sa_sigaction"})


@dataclass(frozen=True)
class CConfigHit:
    kind: str  # env / env_write / cli_option / config_file / constant
    name: str
    line: int
    default: str = ""
    detail: str = ""
    confidence: str = "confirmed"
    uses: tuple[int, ...] = ()  # 値を代入した変数の、同じ関数内での使用の行


@dataclass(frozen=True)
class CBoundaryHit:
    kind: str  # event / thread / network
    label: str
    line: int
    target: str = ""  # 登録・起動される関数の名前（書かれていれば）
    detail: str = ""
    confidence: str = "confirmed"


@dataclass
class CScanResult:
    config: list[CConfigHit] = field(default_factory=list)
    boundaries: list[CBoundaryHit] = field(default_factory=list)
    functions: dict[str, tuple[int, int]] = field(default_factory=dict)  # 関数名 → (開始行, 終了行)。所有者の特定用


def _literal(cursor: ci.Cursor | None) -> str | None:
    """引数が、文字列リテラルそのもの（括弧・暗黙の変換は許す）なら、その内容（引用符を除く）。`c ? "a" : "b"` や変数は None（動的）。"""

    if cursor is None:
        return None
    node = _unwrap(cursor)
    while node.kind in (CK.UNEXPOSED_EXPR, CK.PAREN_EXPR, CK.CSTYLE_CAST_EXPR):
        inner = [c for c in _children(node) if c.kind != CK.TYPE_REF]
        if len(inner) != 1:
            return None
        node = _unwrap(inner[0])
    if node.kind != CK.STRING_LITERAL:
        return None
    tokens = list(node.get_tokens())
    if not tokens or not tokens[0].spelling.startswith('"'):
        return None
    return "".join(t.spelling[1:-1] for t in tokens if t.spelling.startswith('"'))


def _optstring_options(optstring: str) -> list[tuple[str, str]]:
    """`getopt` の第3引数から、(オプション, 説明)。先頭の `+`・`-`・`:` は動作の指定。"""

    options = []
    text = optstring.lstrip("+-:")
    index = 0
    while index < len(text):
        char = text[index]
        index += 1
        if char == ":":
            continue
        suffix = text[index:index + 2]
        detail = "引数を取る" if suffix == "::" else "引数を取る" if suffix[:1] == ":" else "引数なし"
        if suffix == "::":
            detail = "引数は任意"
        options.append((f"-{char}", detail))
    return options


def scan_c_file(translation_unit: ci.TranslationUnit, target_path: str, source: bytes) -> CScanResult:
    result = CScanResult()
    seen: set[tuple[str, str, int]] = set()

    def add_config(hit: CConfigHit) -> None:
        key = (hit.kind, hit.name, hit.line)
        if key not in seen:
            seen.add(key)
            result.config.append(hit)

    def process_function(function: ci.Cursor) -> None:
        name = function.spelling
        result.functions[name] = (function.extent.start.line, function.extent.end.line)
        assigned: dict[tuple[int, int], str] = {}  # 呼び出しの位置 → その値を代入した変数
        for node in function.walk_preorder():
            if node.kind == CK.VAR_DECL:
                values = [c for c in _children(node) if c.kind not in (CK.TYPE_REF, CK.PARM_DECL)]
                if values and _unwrap(values[-1]).kind == CK.CALL_EXPR:
                    inner = _unwrap(values[-1])
                    assigned[(inner.extent.start.line, inner.extent.start.column)] = node.spelling
            elif node.kind == CK.BINARY_OPERATOR:
                parts = _children(node)
                if len(parts) == 2 and _unwrap(parts[0]).kind == CK.DECL_REF_EXPR and _unwrap(parts[1]).kind == CK.CALL_EXPR:
                    inner = _unwrap(parts[1])
                    assigned[(inner.extent.start.line, inner.extent.start.column)] = _unwrap(parts[0]).spelling
        for node in function.walk_preorder():
            if node.kind == CK.BINARY_OPERATOR:  # sa.sa_handler = fn
                parts = _children(node)
                left = _unwrap(parts[0]) if parts else None
                if len(parts) == 2 and left is not None and left.kind == CK.MEMBER_REF_EXPR and left.spelling in _SIGNAL_FIELDS:
                    refs = _function_refs_for(parts[1])
                    result.boundaries.append(CBoundaryHit("event", "シグナルハンドラの登録（sigaction）", node.extent.start.line, refs[0].spelling if refs else "", left.spelling))
                continue
            if node.kind != CK.CALL_EXPR:
                continue
            callee = call_name(node)
            arguments = _call_arguments(node)
            line = node.extent.start.line
            if callee in _ENV_READ | _ENV_WRITE:
                literal = _literal(arguments[0]) if arguments else None
                variable = assigned.get((node.extent.start.line, node.extent.start.column), "")
                uses: tuple[int, ...] = ()
                if variable and callee in _ENV_READ:
                    uses = tuple(sorted({d.extent.start.line for d in function.walk_preorder() if d.kind == CK.DECL_REF_EXPR and d.spelling == variable and d.extent.start.line != line}))
                kind = "env_write" if callee in _ENV_WRITE else "env"
                add_config(CConfigHit(kind, literal or "<動的>", line, detail=("未設定なら NULL を返す" if kind == "env" else callee) + (f"。変数 {variable} に代入" if variable else ""), uses=uses))
            elif callee in ("getopt", "getopt_long", "getopt_long_only"):
                optstring = _literal(arguments[2]) if len(arguments) > 2 else None
                if optstring is None:
                    add_config(CConfigHit("cli_option", "<動的>", line, detail=f"{callee}（オプション文字列が文字列リテラルでない）", confidence="inferred"))
                for option, detail in _optstring_options(optstring or ""):
                    add_config(CConfigHit("cli_option", option, line, detail=f"{callee}: {detail}"))
            elif callee in _STRING_COMPARE and any("argv" in " ".join(t.spelling for t in a.get_tokens()) for a in arguments):
                for argument in arguments:
                    literal = _literal(argument)
                    if literal and literal.startswith("-") and len(literal) > 1:
                        add_config(CConfigHit("cli_option", literal, line, detail="argv との比較", confidence="inferred"))
            elif callee in _OPEN_CALLS:
                literal = _literal(arguments[0]) if arguments else None
                if literal and _CONFIG_PATH.search(literal):
                    add_config(CConfigHit("config_file", literal, line, detail=f"{callee}（名前による推定。設定ファイルかは断定できない）", confidence="inferred"))
            elif callee in _THREAD_CREATE and len(arguments) > _THREAD_CREATE[callee]:
                refs = _function_refs_for(arguments[_THREAD_CREATE[callee]])
                result.boundaries.append(CBoundaryHit("thread", f"スレッドの生成（{callee}）", line, refs[0].spelling if refs else "", "開始関数が関数の名前でない" if not refs else ""))
            elif callee in _ATEXIT and arguments:
                refs = _function_refs_for(arguments[0])
                result.boundaries.append(CBoundaryHit("event", f"終了時・fork 時に呼ばれる関数の登録（{callee}）", line, refs[0].spelling if refs else ""))
            elif callee == "signal" and len(arguments) > 1:
                refs = _function_refs_for(arguments[1])
                signal_name = source[arguments[0].extent.start.offset:arguments[0].extent.end.offset].decode("utf-8", errors="replace").strip()  # SIGINT などのマクロは、トークンが取れない
                result.boundaries.append(CBoundaryHit("event", f"シグナルハンドラの登録（signal {signal_name}）", line, refs[0].spelling if refs else "", "" if refs else "ハンドラが関数の名前でない（SIG_IGN・SIG_DFL・変数など）"))
            elif callee in _NETWORK:
                result.boundaries.append(CBoundaryHit("network", f"ネットワーク（{callee}）", line))
            elif callee in ("fork", "vfork"):
                result.boundaries.append(CBoundaryHit("thread", f"プロセスの生成（{callee}）", line))

    stack = list(reversed(list(translation_unit.cursor.get_children())))
    while stack:
        cursor = stack.pop()
        try:
            kind = cursor.kind
            if kind in _CONTAINER_KINDS:
                stack.extend(reversed(list(cursor.get_children())))
                continue
            file = cursor.location.file
            if file is None or _resolved(file.name) != target_path:
                continue
            if kind in _FUNCTION_KINDS and cursor.is_definition():
                process_function(cursor)
        except ValueError:
            continue
    for match in _LONG_OPTION.finditer(source):  # struct option の表（getopt_long の長いオプション）
        line = source.count(b"\n", 0, match.start()) + 1
        arg = match.group(2).decode()
        detail = {"no_argument": "引数なし", "0": "引数なし", "required_argument": "引数を取る", "1": "引数を取る"}.get(arg, "引数は任意")
        add_config(CConfigHit("cli_option", f"--{match.group(1).decode()}", line, detail=f"長いオプションの表: {detail}", confidence="inferred"))
    return result


_DEFINE = re.compile(rb"^[ \t]*#[ \t]*define[ \t]+([A-Z][A-Z0-9_]*)[ \t]+((?:\"(?:[^\"\\\n]|\\.)*\")|(?:-?(?:0[xX][0-9A-Fa-f]+|\d+(?:\.\d+)?)[uUlLfF]*))[ \t]*(?:/[*/].*)?$", re.MULTILINE)


def scan_constants(source: bytes) -> list[CConfigHit]:
    """`#define NAME <数値または文字列のリテラル>`（設定値になりうる定数のマクロ）。構文解析はせず、行の形から拾う。"""

    hits = []
    for match in _DEFINE.finditer(source):
        line = source.count(b"\n", 0, match.start()) + 1
        hits.append(CConfigHit("constant", match.group(1).decode(), line, default=match.group(2).decode("utf-8", "replace"), detail="マクロ定義"))
    return hits

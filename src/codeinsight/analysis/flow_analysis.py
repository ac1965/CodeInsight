"""関数・クラス単位の制御フロー・例外・データフロー・状態変化の解析（Python、決定論的）。

標準ライブラリの ast だけを用い、対象の関数/クラスのソースから静的に確認できる事実を取り出す。
流れ非依存（flow-insensitive）の解析であり、実行順序・到達可能性・値は保証しない。
結果は、解析時のファイル内容と同じソースに対してのみ意味を持つ（呼び出し側が古さを確認する）。
"""

from __future__ import annotations

import ast
import builtins
from collections import defaultdict
from dataclasses import dataclass, field
from typing import cast

_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
MUTATING_METHODS = frozenset(
    {"append", "extend", "add", "update", "pop", "remove", "clear", "insert", "setdefault",
     "discard", "sort", "reverse", "popitem", "appendleft", "extendleft", "popleft"}
)
_LOG_NAMES = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log", "print"})
_RETRY_WORDS = ("retry", "retries", "backoff", "attempt")


def find_definition(tree: ast.AST, start_line: int) -> ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | None:
    """開始行が一致する関数/クラス定義を探す。"""

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.lineno == start_line:
            return node
    return None


def unparse(node: ast.AST | None, limit: int = 80) -> str:
    if node is None:
        return ""
    try:
        text = ast.unparse(node)
    except Exception:
        return "<解析不能>"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def end_line_of(node: ast.AST) -> int:
    return getattr(node, "end_lineno", None) or node.lineno  # type: ignore[attr-defined]


# --- 制御フロー ---


@dataclass(frozen=True)
class FlowItem:
    kind: str  # if / elif / else / for / while / try / except / finally / with / match / case /
    #            raise / return / yield / await / break / continue / assert
    line: int
    end_line: int
    depth: int
    detail: str


@dataclass(frozen=True)
class HandlerInfo:
    try_line: int
    line: int
    types: tuple[str, ...]  # 捕捉する例外型。bare except は ("<bare>",)
    swallowed: bool  # 本体が pass/continue/break のみで、例外を握りつぶしている
    reraises: bool  # 本体に再送出(raise)がある
    logs: bool  # 本体でログ・表示の呼び出しがある


@dataclass(frozen=True)
class RaiseInfo:
    line: int
    exception: str  # 送出する例外の表記。引数なしの raise は "<再送出>"
    handlers: tuple[tuple[str, ...], ...]  # この raise を囲む try の例外型の列（内側から）


@dataclass(frozen=True)
class TryRange:
    start: int
    end: int  # try本体の最終行
    handler_types: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Hint:
    kind: str  # retry / timeout / sleep
    line: int
    detail: str


@dataclass
class ControlFlowSummary:
    items: list[FlowItem] = field(default_factory=list)
    handlers: list[HandlerInfo] = field(default_factory=list)
    raises: list[RaiseInfo] = field(default_factory=list)
    tries: list[TryRange] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)
    metrics: dict[str, int] = field(default_factory=dict)


def analyze_control_flow(
    function: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str] | None = None
) -> ControlFlowSummary:
    """source_lines を渡すと、ASTに行が無い `else:` の行を、ソースから探して正確にする。"""

    summary = ControlFlowSummary()
    counts: dict[str, int] = defaultdict(int)
    decisions = 0
    max_depth = 0

    def add(kind: str, node: ast.AST, depth: int, detail: str = "") -> None:
        nonlocal max_depth
        summary.items.append(FlowItem(kind, node.lineno, end_line_of(node), depth, detail))  # type: ignore[attr-defined]
        counts[kind] += 1
        max_depth = max(max_depth, depth)

    def expression_items(statement: ast.AST, depth: int) -> None:
        nonlocal decisions
        stack = [c for c in ast.iter_child_nodes(statement) if not isinstance(c, ast.stmt)]
        while stack:
            current = stack.pop()
            if isinstance(current, _NESTED_SCOPES):
                continue
            if isinstance(current, ast.Await):
                add("await", current, depth, unparse(current.value))
            elif isinstance(current, (ast.Yield, ast.YieldFrom)):
                add("yield", current, depth, unparse(getattr(current, "value", None)))
            elif isinstance(current, ast.BoolOp):
                decisions += len(current.values) - 1
            elif isinstance(current, ast.IfExp):
                decisions += 1
            elif isinstance(current, ast.comprehension):
                decisions += 1 + len(current.ifs)
            elif isinstance(current, ast.Call):
                for keyword in current.keywords:
                    if keyword.arg == "timeout":
                        summary.hints.append(Hint("timeout", current.lineno, f"{unparse(current.func, 50)}(timeout={unparse(keyword.value, 30)})"))
                name = unparse(current.func, 60)
                if name.rsplit(".", 1)[-1] == "sleep":
                    summary.hints.append(Hint("sleep", current.lineno, f"{name}({unparse(current.args[0], 20) if current.args else ''})"))
            stack.extend(ast.iter_child_nodes(current))

    def walk(body: list[ast.stmt], depth: int, handler_stack: tuple[tuple[str, ...], ...]) -> None:
        nonlocal decisions
        for statement in body:
            if isinstance(statement, ast.If):
                decisions += 1
                add("if", statement, depth, unparse(statement.test))
                expression_items(statement, depth)
                walk(statement.body, depth + 1, handler_stack)
                orelse = statement.orelse
                while len(orelse) == 1 and isinstance(orelse[0], ast.If):
                    decisions += 1
                    add("elif", orelse[0], depth, unparse(orelse[0].test))
                    walk(orelse[0].body, depth + 1, handler_stack)
                    orelse = orelse[0].orelse
                if orelse:
                    else_line = _else_line(source_lines, orelse[0].lineno)
                    summary.items.append(FlowItem("else", else_line, end_line_of(orelse[-1]), depth, ""))
                    counts["else"] += 1
                    walk(orelse, depth + 1, handler_stack)
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
                decisions += 1
                kind = "while" if isinstance(statement, ast.While) else "for"
                detail = unparse(statement.test) if isinstance(statement, ast.While) else f"{unparse(statement.target, 30)} in {unparse(statement.iter, 50)}"
                add(kind, statement, depth, detail)
                expression_items(statement, depth)
                walk(statement.body, depth + 1, handler_stack)
                if statement.orelse:
                    walk(statement.orelse, depth + 1, handler_stack)
                if _loop_retries(statement):
                    summary.hints.append(Hint("retry", statement.lineno, "ループ内のtry/exceptで例外を受けて繰り返す（リトライの可能性）"))
            elif isinstance(statement, ast.Try) or statement.__class__.__name__ == "TryStar":
                try_stmt = cast("ast.Try", statement)
                types_per_handler = tuple(exception_types_of(h) for h in try_stmt.handlers)
                add("try", statement, depth)
                summary.tries.append(TryRange(statement.lineno, end_line_of(try_stmt.body[-1]), types_per_handler))
                walk(try_stmt.body, depth + 1, (_flatten(types_per_handler), *handler_stack))
                for handler in try_stmt.handlers:
                    decisions += 1
                    types = exception_types_of(handler)
                    add("except", handler, depth, ", ".join(types))
                    summary.handlers.append(handler_info(statement, handler, types))
                    walk(handler.body, depth + 1, handler_stack)
                if try_stmt.orelse:
                    walk(try_stmt.orelse, depth + 1, handler_stack)
                if try_stmt.finalbody:
                    summary.items.append(FlowItem("finally", try_stmt.finalbody[0].lineno, end_line_of(try_stmt.finalbody[-1]), depth, ""))
                    counts["finally"] += 1
                    walk(try_stmt.finalbody, depth + 1, handler_stack)
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                add("with", statement, depth, ", ".join(unparse(i.context_expr, 40) for i in statement.items))
                expression_items(statement, depth)
                walk(statement.body, depth + 1, handler_stack)
            elif isinstance(statement, ast.Match):
                add("match", statement, depth, unparse(statement.subject))
                for case in statement.cases:
                    decisions += 1
                    add("case", case.pattern, depth + 1, unparse(case.pattern, 50))
                    walk(case.body, depth + 2, handler_stack)
            elif isinstance(statement, ast.Raise):
                exception = "<再送出>" if statement.exc is None else _exception_name(statement.exc)
                add("raise", statement, depth, exception)
                summary.raises.append(RaiseInfo(statement.lineno, exception, handler_stack))
                expression_items(statement, depth)
            elif isinstance(statement, ast.Return):
                add("return", statement, depth, unparse(statement.value, 50))
                expression_items(statement, depth)
            elif isinstance(statement, ast.Break):
                add("break", statement, depth)
            elif isinstance(statement, ast.Continue):
                add("continue", statement, depth)
            elif isinstance(statement, ast.Assert):
                add("assert", statement, depth, unparse(statement.test, 50))
                expression_items(statement, depth)
            elif isinstance(statement, _NESTED_SCOPES):
                continue
            else:
                expression_items(statement, depth)

    walk(function.body, 0, ())
    for decorator in function.decorator_list:
        text = unparse(decorator, 60)
        if any(word in text.lower() for word in _RETRY_WORDS):
            summary.hints.append(Hint("retry", decorator.lineno, f"デコレータ {text}"))
    summary.metrics = {
        "branches": counts["if"] + counts["elif"] + counts["case"],
        "loops": counts["for"] + counts["while"],
        "try_blocks": counts["try"],
        "handlers": counts["except"],
        "raises": counts["raise"],
        "returns": counts["return"],
        "awaits": counts["await"],
        "yields": counts["yield"],
        "with_blocks": counts["with"],
        "cyclomatic": 1 + decisions,
        "max_depth": max_depth,
        "lines": end_line_of(function) - function.lineno + 1,
    }
    summary.items.sort(key=lambda item: (item.line, item.depth))
    summary.hints.sort(key=lambda hint: hint.line)
    return summary


def _else_line(source_lines: list[str] | None, first_body_line: int) -> int:
    """`else:` の行番号。ASTには無いため、本体の直前のソース行から探す（無ければ本体の先頭行）。"""

    if source_lines:
        for number in range(first_body_line - 1, max(0, first_body_line - 6), -1):
            if source_lines[number - 1].strip().startswith("else"):
                return number
    return first_body_line


def _flatten(groups: tuple[tuple[str, ...], ...]) -> tuple[str, ...]:
    return tuple(t for group in groups for t in group)


def exception_types_of(handler: ast.ExceptHandler) -> tuple[str, ...]:
    if handler.type is None:
        return ("<bare>",)
    if isinstance(handler.type, ast.Tuple):
        return tuple(unparse(e, 40) for e in handler.type.elts)
    return (unparse(handler.type, 40),)


def _exception_name(expression: ast.expr) -> str:
    target = expression.func if isinstance(expression, ast.Call) else expression
    return unparse(target, 60)


def handler_info(try_node: ast.AST, handler: ast.ExceptHandler, types: tuple[str, ...]) -> HandlerInfo:
    body = handler.body
    swallowed = all(
        isinstance(s, (ast.Pass, ast.Continue, ast.Break))
        or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
        for s in body
    )
    reraises = any(isinstance(n, ast.Raise) for s in body for n in ast.walk(s))
    logs = any(
        isinstance(n, ast.Call) and unparse(n.func, 60).rsplit(".", 1)[-1] in _LOG_NAMES
        for s in body
        for n in ast.walk(s)
    )
    return HandlerInfo(try_node.lineno, handler.lineno, types, swallowed, reraises, logs)  # type: ignore[attr-defined]


def _loop_retries(loop: ast.For | ast.AsyncFor | ast.While) -> bool:
    """ループ内のtryで、例外を受けても関数を抜けず繰り返す形（リトライの可能性）。"""

    for node in ast.walk(loop):
        if isinstance(node, ast.Try) and node is not loop:
            for handler in node.handlers:
                exits = any(isinstance(n, (ast.Raise, ast.Return, ast.Break)) for s in handler.body for n in ast.walk(s))
                if not exits:
                    return True
    return False


# --- 変数のライフサイクル（定義・使用・伝播） ---


@dataclass(frozen=True)
class Definition:
    line: int
    how: str  # param / assign / augassign / for / with / import / except / walrus / delete
    origin: str  # 代入される式の表記（引数・importなどは空）
    depends_on: tuple[str, ...]  # 代入される式に現れる変数名


@dataclass(frozen=True)
class Flow:
    """変数の値が伝わる先。"""

    kind: str  # copy / derive / call_arg / return / yield / attr_store / method_call / subscript_store
    line: int
    target: str  # 伝播先の変数名・呼び出し・属性などの表記
    call_line: int = 0
    call_text: str = ""
    position: int | None = None  # call_arg: 位置引数の番号（0始まり）
    keyword: str | None = None  # call_arg: キーワード引数名
    starred: bool = False


@dataclass
class VariableInfo:
    name: str
    is_param: bool = False
    definitions: list[Definition] = field(default_factory=list)
    uses: list[int] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)


def parameter_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """位置で渡される順の引数名（selfを含む）。キーワード専用・可変長は後ろに付ける。"""

    args = function.args
    names = [a.arg for a in [*args.posonlyargs, *args.args]]
    names += [a.arg for a in args.kwonlyargs]
    return names


def analyze_variables(function: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, VariableInfo]:
    variables: dict[str, VariableInfo] = {}

    def info(name: str) -> VariableInfo:
        return variables.setdefault(name, VariableInfo(name))

    args = function.args
    for argument in [*args.posonlyargs, *args.args, *args.kwonlyargs, *(a for a in (args.vararg, args.kwarg) if a)]:
        item = info(argument.arg)
        item.is_param = True
        item.definitions.append(Definition(function.lineno, "param", "", ()))

    parents: dict[int, ast.AST] = {}
    scope_nodes: list[ast.AST] = []
    stack: list[ast.AST] = list(function.body)
    for statement in function.body:
        parents[id(statement)] = function
    while stack:
        node = stack.pop()
        scope_nodes.append(node)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _NESTED_SCOPES):
                # ネスト関数は別スコープだが、外側の変数を参照しうるため、使用だけは拾う
                _collect_nested_uses(child, info, variables)
                continue
            parents[id(child)] = node
            stack.append(child)

    def names_in(node: ast.AST | None) -> tuple[str, ...]:
        if node is None:
            return ()
        found: list[str] = []
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in found:
                found.append(n.id)
        return tuple(found)

    def target_names(target: ast.AST) -> list[ast.Name]:
        return [n for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)]

    for node in scope_nodes:
        if isinstance(node, ast.Assign):
            sources = names_in(node.value)
            for target in node.targets:
                for name_node in target_names(target):
                    info(name_node.id).definitions.append(Definition(node.lineno, "assign", unparse(node.value, 60), sources))
                    _add_value_flows(info, sources, name_node.id, node.value, node.lineno)
                _store_flows(info, target, sources, node.lineno)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            sources = names_in(node.value)
            info(node.target.id).definitions.append(Definition(node.lineno, "assign", unparse(node.value, 60), sources))
            _add_value_flows(info, sources, node.target.id, node.value, node.lineno)
        elif isinstance(node, ast.AugAssign):
            sources = names_in(node.value)
            if isinstance(node.target, ast.Name):
                info(node.target.id).definitions.append(Definition(node.lineno, "augassign", unparse(node.value, 60), sources))
                _add_value_flows(info, sources, node.target.id, node.value, node.lineno)
            else:
                _store_flows(info, node.target, sources, node.lineno)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            sources = names_in(node.iter)
            for name_node in target_names(node.target):
                info(name_node.id).definitions.append(Definition(node.lineno, "for", unparse(node.iter, 60), sources))
                _add_value_flows(info, sources, name_node.id, node.iter, node.lineno)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for with_item in node.items:
                if with_item.optional_vars is not None:
                    sources = names_in(with_item.context_expr)
                    for name_node in target_names(with_item.optional_vars):
                        info(name_node.id).definitions.append(Definition(node.lineno, "with", unparse(with_item.context_expr, 60), sources))
                        _add_value_flows(info, sources, name_node.id, with_item.context_expr, node.lineno)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                info((alias.asname or alias.name).split(".", 1)[0]).definitions.append(Definition(node.lineno, "import", "", ()))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            info(node.name).definitions.append(Definition(node.lineno, "except", unparse(node.type, 40), ()))
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            sources = names_in(node.value)
            info(node.target.id).definitions.append(Definition(node.lineno, "walrus", unparse(node.value, 60), sources))
            _add_value_flows(info, sources, node.target.id, node.value, node.lineno)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            _record_use(node, parents, info)
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    info(target.id).definitions.append(Definition(node.lineno, "delete", "", ()))
    for item in variables.values():  # 走査順ではなくソースの行順にそろえる
        item.definitions.sort(key=lambda d: d.line)
        item.uses.sort()
        item.flows.sort(key=lambda f: (f.line, f.kind, f.target))
    return variables


def _collect_nested_uses(scope: ast.AST, info, variables) -> None:
    for node in ast.walk(scope):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in variables:
            info(node.id).uses.append(node.lineno)


def _add_value_flows(info, sources: tuple[str, ...], target: str, value: ast.AST | None, line: int) -> None:
    kind = "copy" if isinstance(value, ast.Name) else "derive"
    for source in sources:
        info(source).flows.append(Flow(kind, line, target))


def _store_flows(info, target: ast.AST, sources: tuple[str, ...], line: int) -> None:
    """`obj.attr = <式>` / `obj[k] = <式>` への書き込みを、式に現れる変数の伝播として記録する。"""

    if isinstance(target, ast.Attribute):
        for source in sources:
            info(source).flows.append(Flow("attr_store", line, unparse(target, 50)))
    elif isinstance(target, ast.Subscript):
        for source in sources:
            info(source).flows.append(Flow("subscript_store", line, unparse(target, 50)))
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _store_flows(info, element, sources, line)


def _record_use(node: ast.Name, parents: dict[int, ast.AST], info) -> None:
    item = info(node.id)
    item.uses.append(node.lineno)
    parent = parents.get(id(node))
    child: ast.AST = node
    # 式の入れ子（括弧・タプル・スプラット・キーワード）をたどって、直接の使われ方を判定する。
    while isinstance(parent, (ast.Starred, ast.Tuple, ast.List, ast.keyword)) and id(parent) in parents:
        if isinstance(parent, ast.keyword):
            break
        child, parent = parent, parents[id(parent)]
    if isinstance(parent, ast.keyword):
        call = parents.get(id(parent))
        if isinstance(call, ast.Call):
            item.flows.append(Flow("call_arg", node.lineno, unparse(call.func, 60), call.lineno, unparse(call, 70), None, parent.arg))
    elif isinstance(parent, ast.Call) and child is not parent.func:
        positions = [i for i, a in enumerate(parent.args) if a is child or (isinstance(a, ast.Starred) and a.value is child)]
        starred = isinstance(child, ast.Starred)
        item.flows.append(Flow("call_arg", node.lineno, unparse(parent.func, 60), parent.lineno, unparse(parent, 70), positions[0] if positions else None, None, starred))
    elif isinstance(parent, ast.Return):
        item.flows.append(Flow("return", node.lineno, "return"))
    elif isinstance(parent, (ast.Yield, ast.YieldFrom)):
        item.flows.append(Flow("yield", node.lineno, "yield"))
    elif isinstance(parent, ast.Attribute):
        grand = parents.get(id(parent))
        if isinstance(grand, ast.Call) and grand.func is parent:
            item.flows.append(Flow("method_call", node.lineno, f"{node.id}.{parent.attr}", grand.lineno, unparse(grand, 70)))


# --- 状態の変化（クラス・モジュール） ---


@dataclass(frozen=True)
class StateAccess:
    attribute: str
    method: str
    line: int
    mode: str  # write / mutate / read


def analyze_class_state(classdef: ast.ClassDef) -> list[StateAccess]:
    """クラスの各メソッドが `self.<属性>` をどう使うか（書き込み・破壊的メソッド呼び出し・読み取り）。"""

    accesses: list[StateAccess] = []
    for member in classdef.body:
        if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = [*member.args.posonlyargs, *member.args.args]
        if not positional or any(unparse(d) == "staticmethod" for d in member.decorator_list):
            continue
        accesses.extend(_state_accesses(member, positional[0].arg, member.name))
    return accesses


def _state_accesses(function: ast.AST, receiver: str, method: str) -> list[StateAccess]:
    found: list[StateAccess] = []
    skip: set[int] = set()

    def is_self_attr(node: ast.AST) -> str | None:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == receiver:
            return node.attr
        return None

    for node in ast.walk(function):
        if isinstance(node, _NESTED_SCOPES) and node is not function:
            skip.update(id(n) for n in ast.walk(node))
    for node in ast.walk(function):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Attribute):
            name = is_self_attr(node)
            if name is None:
                continue
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                found.append(StateAccess(name, method, node.lineno, "write"))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            inner = node.func.value
            name = is_self_attr(inner)
            if name is not None and node.func.attr in MUTATING_METHODS:
                found.append(StateAccess(name, method, node.lineno, "mutate"))
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Subscript):
                    name = is_self_attr(target.value)
                    if name is not None:
                        found.append(StateAccess(name, method, node.lineno, "mutate"))
    written = {(a.attribute, a.line) for a in found}
    for node in ast.walk(function):
        if id(node) in skip or not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
            continue
        name = is_self_attr(node)
        if name is not None and (name, node.lineno) not in written:
            found.append(StateAccess(name, method, node.lineno, "read"))
    found.sort(key=lambda a: (a.line, a.attribute, a.mode))
    return found


@dataclass(frozen=True)
class GlobalWrite:
    name: str
    function: str
    line: int
    mode: str  # global_assign / mutate


def analyze_module_state(tree: ast.Module) -> list[GlobalWrite]:
    """モジュール変数を、関数内から書き換える箇所（global宣言での代入・破壊的メソッド呼び出し）。"""

    module_names = {
        t.id
        for stmt in tree.body
        if isinstance(stmt, (ast.Assign, ast.AnnAssign))
        for t in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target])
        if isinstance(t, ast.Name)
    }
    writes: list[GlobalWrite] = []
    for function in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        declared = {name for n in ast.walk(function) if isinstance(n, ast.Global) for name in n.names}
        local = {v for v, i in analyze_variables(function).items() if i.is_param or any(d.how != "param" for d in i.definitions)}
        for node in ast.walk(function):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in declared:
                writes.append(GlobalWrite(node.id, function.name, node.lineno, "global_assign"))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in module_names
                and node.func.value.id not in (local - declared)
                and node.func.attr in MUTATING_METHODS
            ):
                writes.append(GlobalWrite(node.func.value.id, function.name, node.lineno, "mutate"))
    return sorted(set(writes), key=lambda w: (w.line, w.name))


# --- 例外の捕捉判定 ---


def exception_caught(raised: str, handler_types: tuple[str, ...], bases_of=None) -> bool:
    """送出される例外 `raised` が、`handler_types` のいずれかで捕捉されるか。

    bare except / Exception / BaseException は全てを捕捉する（BaseExceptionのみ捕捉する型は無視）。
    組み込み例外は継承関係で判定し、プロジェクト内の例外は bases_of(名前) が返す基底クラス名で判定する。
    """

    for handler in handler_types:
        if handler in ("<bare>", "BaseException", "Exception"):
            return True
        if handler == raised:
            return True
        short_raised, short_handler = raised.rsplit(".", 1)[-1], handler.rsplit(".", 1)[-1]
        if short_raised == short_handler:
            return True
        raised_cls, handler_cls = getattr(builtins, short_raised, None), getattr(builtins, short_handler, None)
        if isinstance(raised_cls, type) and isinstance(handler_cls, type) and issubclass(raised_cls, handler_cls):
            return True
        if bases_of is not None and short_handler in bases_of(raised):
            return True
    return False

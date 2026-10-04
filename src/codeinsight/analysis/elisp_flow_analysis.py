"""Emacs Lispの関数単位の制御フロー・例外（シグナル）・状態。流れ非依存の近似。

S式の構文（elisp_analyzer.read_forms）から得られる構造だけを扱う。実行時にどの経路を通るか、マクロ展開後に何になるかは示さない。
引用されたデータ（'x）とバッククォートのテンプレート（マクロが出力するコード）の中は、この関数自身の処理ではないため辿らない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeGuard

from codeinsight.analysis import flow_analysis as fa
from codeinsight.analysis.elisp_analyzer import _DEFINERS, Atom, Form, _all_forms, _head_name, _quoted_symbol
from codeinsight.analysis.elisp_forms import (
    ALL_LOOP_FORMS,
    BRANCH_FORMS,
    CONDITION_CASE_FORMS,
    HANDLER_FORMS,
    LAMBDA_LIST_SKIP,
    MATCH_FORMS,
    PLACE_BINDERS,
    PLACE_SECOND_FORMS,
    RETURN_FORMS,
    SIGNAL_FORMS,
    TERMINATE_FORMS,
    VARIABLE_BINDERS,
)

_IF = BRANCH_FORMS
_MATCH = MATCH_FORMS
_LOOP = ALL_LOOP_FORMS
_CONDITION_CASE = CONDITION_CASE_FORMS
_HANDLER_FORMS = HANDLER_FORMS
_SIGNALS = SIGNAL_FORMS
_TERMINATE = TERMINATE_FORMS
_RETURNS = RETURN_FORMS
_BINDERS = VARIABLE_BINDERS | PLACE_BINDERS
_WRITES = frozenset({"setq", "setq-local", "setq-default", "setf", "cl-incf", "cl-decf", "incf", "decf", "push", "pop", "cl-pushnew", "add-to-list",
                     "add-to-ordered-list", "cl-callf", "cl-callf2", "cl-rotatef", "cl-shiftf", "cl-remf", "setq-mode-local"})
_MUTATORS = frozenset({"aset", "puthash", "setcar", "setcdr", "nconc", "nreverse", "delq", "delete", "remhash", "clrhash", "cl-delete", "sort", "ring-insert",
                       "fillarray", "cl-nsubstitute"})
_MUTATED_INDEX = {"puthash": 2, "ring-insert": 0}  # 書き換えられるオブジェクトの引数の位置（既定は先頭）
_PLACE_SECOND = PLACE_SECOND_FORMS
_HOOK_FORMS = frozenset({"add-hook", "remove-hook"})
_SET_FORMS = frozenset({"set", "set-default", "make-local-variable", "make-variable-buffer-local", "defvar-local", "defvar", "defconst", "defcustom"})
_LAMBDA_LIST_SKIP = LAMBDA_LIST_SKIP


def function_parts(form: Form) -> tuple[list[str], list[Atom | Form]]:
    """定義フォームから、(仮引数の名前, 本体の式の並び)。"""

    definer = _head_name(form) or ""
    style = _DEFINERS.get(definer, (None, "defun"))[1]
    if style == "defun":
        arglist, body = (form.items[2] if len(form.items) > 2 else None), form.items[3:]
    elif style == "method":
        position = next((k for k, item in enumerate(form.items[2:], 2) if isinstance(item, Form)), len(form.items) - 1)
        arglist, body = form.items[position], form.items[position + 1:]
    else:  # mode など: 引数リストを持たない
        return [], form.items[2:]
    names: list[str] = []
    if isinstance(arglist, Form):
        for item in arglist.items:
            if isinstance(item, Atom) and item.kind == "symbol" and item.text not in _LAMBDA_LIST_SKIP:
                names.append(item.text)
            elif isinstance(item, Form) and item.items and isinstance(item.items[0], Atom):  # cl-defun の (名前 既定値) / cl-defmethod の (名前 型)
                names.append(item.items[0].text)
    return names, body


def find_definition(forms: list[Form], name: str, start_line: int) -> Form | None:
    for top in forms:
        for node in _all_forms(top):
            if node.line == start_line and _head_name(node) in _DEFINERS and not node.quoted:
                second = node.items[1] if len(node.items) > 1 else None
                if isinstance(second, Atom) and second.text == name:
                    return node
    return None


def _executed(form: Form):
    """この式から、評価される部分式を（引用・テンプレートを除いて）辿る。"""

    stack = [form]
    while stack:
        node = stack.pop()
        yield node
        for item in reversed(node.items):
            if isinstance(item, Form) and not item.quoted:
                stack.append(item)


def _is_code(item: Atom | Form) -> TypeGuard[Form]:
    return isinstance(item, Form) and not item.quoted and not item.vector and bool(item.items)


# --- 制御フロー ---


def analyze_control_flow(definition: Form) -> fa.ControlFlowSummary:
    summary = fa.ControlFlowSummary()
    _, body = function_parts(definition)
    for item in body:
        _flow(item, 0, summary)
    items = summary.items
    count = lambda *kinds: sum(1 for i in items if i.kind in kinds)  # noqa: E731
    summary.metrics = {
        "lines": definition.end_line - definition.line + 1,
        "branches": count("if", "cond-clause", "case", "and/or"),
        "loops": count("loop"),
        "handlers": count("except"),
        "signals": count("raise"),
        "returns": count("return"),
        "exits": count("exit"),
        "cyclomatic": 1 + count("if", "cond-clause", "case", "loop", "except", "and/or"),
        "max_depth": max((i.depth for i in items), default=0),
    }
    return summary


def _flow(item: Atom | Form, depth: int, summary: fa.ControlFlowSummary) -> None:
    if not _is_code(item):
        return
    form = item
    name = _head_name(form)
    rest = form.items[1:]
    child_depth = depth

    def add(kind: str, detail: str = "", at: Form | None = None, nest: bool = False) -> None:
        nonlocal child_depth
        target = at or form
        summary.items.append(fa.FlowItem(kind, target.line, target.end_line, depth, detail))
        if nest:
            child_depth = depth + 1

    if name in _IF:
        add("if", f"{name} {_text(rest[0]) if rest else ''}".strip(), nest=True)
    elif name == "cond":
        add("cond", f"{len([c for c in rest if isinstance(c, Form)])}節", nest=True)
        for clause in rest:
            if isinstance(clause, Form) and not clause.quoted and clause.items:
                summary.items.append(fa.FlowItem("cond-clause", clause.line, clause.end_line, depth + 1, _text(clause.items[0])))
                for sub in clause.items:
                    _flow(sub, depth + 2, summary)
        return
    elif name in _MATCH:
        add("match", f"{name} {_text(rest[0]) if rest else ''}".strip(), nest=True)
        for clause in rest[1:]:
            if isinstance(clause, Form) and not clause.quoted and clause.items:
                summary.items.append(fa.FlowItem("case", clause.line, clause.end_line, depth + 1, _text(clause.items[0])))
                for sub in clause.items[1:]:
                    _flow(sub, depth + 2, summary)
        return
    elif name in _LOOP:
        add("loop", f"{name} {_text(rest[0]) if rest and name != 'cl-loop' else ''}".strip(), nest=True)
    elif name in _HANDLER_FORMS:
        add("try", name, nest=True)
        if name in _CONDITION_CASE:
            for sub in rest[1:2]:
                _flow(sub, depth + 1, summary)
            for handler in rest[2:]:
                if isinstance(handler, Form) and handler.items:
                    summary.items.append(fa.FlowItem("except", handler.line, handler.end_line, depth, ", ".join(_conditions(handler.items[0]))))
                    for sub in handler.items[1:]:
                        _flow(sub, depth + 1, summary)
            return
        summary.items.append(fa.FlowItem("except", form.line, form.end_line, depth, {"ignore-errors": "error（握りつぶす）", "with-demoted-errors": "error（メッセージにして続行）",
                                                                                    "ignore-error": _text(rest[0]) if rest else ""}.get(name, "")))
    elif name == "unwind-protect":
        add("try", "unwind-protect", nest=True)
        for sub in rest[:1]:
            _flow(sub, depth + 1, summary)
        if len(rest) > 1:
            summary.items.append(fa.FlowItem("finally", rest[1].line, form.end_line, depth, "unwind-protect の後始末"))
            for sub in rest[1:]:
                _flow(sub, depth + 1, summary)
        return
    elif name == "catch":
        add("catch", _text(rest[0]) if rest else "", nest=True)
    elif name in _SIGNALS:
        add("raise", f"{name} {_text(rest[0]) if rest else ''}".strip())
    elif name in _TERMINATE:
        add("exit", name)
    elif name in _RETURNS:
        add("return", name)
    elif name in ("and", "or") and len([r for r in rest if _is_code(r)]) > 0:
        add("and/or", f"{name}（短絡評価）")
    for sub in rest:
        _flow(sub, child_depth, summary)


def _text(item: Atom | Form) -> str:
    if isinstance(item, Atom):
        return item.text if item.kind != "string" else f'"{item.text[:30]}"'
    head = _text(item.items[0]) if item.items else ""
    return f"({head} …)" if len(item.items) > 1 else f"({head})"


def _conditions(item: Atom | Form) -> list[str]:
    if isinstance(item, Atom):
        return [item.text]
    return [i.text for i in item.items if isinstance(i, Atom)]


# --- 例外（シグナル）・終了 ---


@dataclass(frozen=True)
class ElispSignal:
    kind: str  # signal / terminate / throw
    line: int
    detail: str  # 例: error / (user-error "…") / kill-emacs
    guarded_by: tuple[str, ...]  # この呼び出しを囲む condition-case 等（内側から。「L12 condition-case (error)」）


@dataclass(frozen=True)
class ElispHandler:
    line: int
    form: str  # condition-case / ignore-errors / …
    conditions: tuple[str, ...]
    swallowed: bool  # 本体が nil / ignore だけで、エラーを握りつぶしている（ignore-errors は常に）
    reraises: bool  # 本体で signal / error を再送出している


@dataclass(frozen=True)
class GuardRange:
    start: int
    end: int
    form: str
    conditions: tuple[str, ...]


@dataclass
class ElispExits:
    signals: list[ElispSignal] = field(default_factory=list)
    handlers: list[ElispHandler] = field(default_factory=list)
    guards: list[GuardRange] = field(default_factory=list)
    catches: list[tuple[int, str]] = field(default_factory=list)  # (行, catch のタグ)
    unwind: list[int] = field(default_factory=list)


def analyze_exits(definition: Form) -> ElispExits:
    result = ElispExits()
    _, body = function_parts(definition)
    for item in body:
        _exits(item, result, ())
    return result


def _guard_label(guard: GuardRange) -> str:
    return f"L{guard.start} {guard.form} ({', '.join(guard.conditions)})"


def guards_at(guards: list[GuardRange], line: int) -> tuple[GuardRange, ...]:
    """行を囲む保護（condition-case 等の本体の範囲）。行単位の近似のため、同じ行の別の式も含みうる。"""

    return tuple(sorted((g for g in guards if g.start <= line <= g.end), key=lambda g: g.end - g.start))


def _exits(item: Atom | Form, result: ElispExits, stack: tuple[GuardRange, ...]) -> None:
    if not _is_code(item):
        return
    form = item
    name = _head_name(form)
    rest = form.items[1:]
    labels = tuple(_guard_label(g) for g in reversed(stack))
    if name in _HANDLER_FORMS:
        protected = rest[1:2] if name in _CONDITION_CASE else rest if name != "ignore-error" else rest[1:]
        end = max((p.end_line for p in protected if isinstance(p, Form)), default=form.end_line)
        start = min((p.line for p in protected if isinstance(p, (Atom, Form))), default=form.line)
        if name in _CONDITION_CASE:
            handlers = [h for h in rest[2:] if isinstance(h, Form) and h.items]
            conditions = tuple(c for h in handlers for c in _conditions(h.items[0]))
            for handler in handlers:
                bodies = [s for s in handler.items[1:]]
                swallowed = all((isinstance(b, Atom) and b.text == "nil") or (isinstance(b, Form) and _head_name(b) in ("ignore", "progn") and len(b.items) <= 2)
                                for b in bodies) if bodies else True
                reraises = any(_head_name(n) in ("signal", "error", "user-error") for b in bodies if isinstance(b, Form) for n in _executed(b))
                result.handlers.append(ElispHandler(handler.line, name, tuple(_conditions(handler.items[0])), swallowed, reraises))
        else:
            conditions = {"ignore-errors": ("error",), "with-demoted-errors": ("error",), "ignore-error": tuple(_conditions(rest[0])) if rest else ()}[name]
            result.handlers.append(ElispHandler(form.line, name, conditions, name == "ignore-errors", False))
        guard = GuardRange(start, end, name, conditions)
        result.guards.append(guard)
        inner = (*stack, guard)
        for sub in protected:
            _exits(sub, result, inner)
        if name in _CONDITION_CASE:
            for clause in rest[2:]:
                if isinstance(clause, Form):
                    for sub in clause.items[1:]:
                        _exits(sub, result, stack)
        return
    if name == "unwind-protect":
        result.unwind.append(form.line)
    elif name == "catch" and rest:
        result.catches.append((form.line, _text(rest[0])))
    elif name in _SIGNALS:
        kind = "throw" if name == "throw" else "signal"
        detail = f"{name} {_text(rest[0]) if rest else ''}".strip()
        result.signals.append(ElispSignal(kind, form.line, detail, labels))
    elif name in _TERMINATE:
        result.signals.append(ElispSignal("terminate", form.line, name, labels))
    for sub in rest:
        _exits(sub, result, stack)


# --- 状態 ---


@dataclass(frozen=True)
class ElispAccess:
    name: str
    line: int
    mode: str  # write / mutate / read
    scope: str  # global（グローバル・動的な変数）/ buffer-local / hook
    via: str  # 例: setq / push / add-hook


@dataclass
class ElispState:
    accesses: list[ElispAccess] = field(default_factory=list)
    shadowed: list[str] = field(default_factory=list)  # 局所にも束縛される名前（グローバルへのアクセスかを判定していない）


def _local_names(definition: Form) -> set[str]:
    params, body = function_parts(definition)
    names = set(params)
    for top in body:
        if not isinstance(top, Form):
            continue
        for node in _executed(top):
            head = _head_name(node)
            rest = node.items[1:]
            if head in _BINDERS and rest and isinstance(rest[0], Form):
                for binding in rest[0].items:
                    if isinstance(binding, Atom) and binding.kind == "symbol":
                        names.add(binding.text)
                    elif isinstance(binding, Form) and binding.items and isinstance(binding.items[0], Atom):
                        names.add(binding.items[0].text)
            elif head in ("dolist", "dotimes", "cl-dolist", "cl-dotimes") and rest and isinstance(rest[0], Form) and rest[0].items and isinstance(rest[0].items[0], Atom):
                names.add(rest[0].items[0].text)
            elif head in _CONDITION_CASE and rest and isinstance(rest[0], Atom):
                names.add(rest[0].text)
            elif head in ("lambda", "cl-flet", "cl-labels") and rest and isinstance(rest[0], Form):
                names.update(i.text for i in rest[0].items if isinstance(i, Atom) and i.kind == "symbol" and i.text not in _LAMBDA_LIST_SKIP)
    return names


def analyze_state(definition: Form, known_globals: set[str]) -> ElispState:
    """グローバル（動的）変数への書き込み・書き換え・読み取り。

    読み取りは、プロジェクト内で defvar 等で定義された変数（known_globals）に限る。
    局所にも束縛される名前は、同名の局所変数との区別が静的にできないため、判定せず shadowed に記録する。
    """

    locals_ = _local_names(definition)
    state = ElispState()
    _, body = function_parts(definition)

    def record(name: str, line: int, mode: str, scope: str, via: str) -> None:
        if name in locals_:
            if name not in state.shadowed:
                state.shadowed.append(name)
            return
        state.accesses.append(ElispAccess(name, line, mode, scope, via))

    written: set[tuple[str, int]] = set()
    for top in body:
        if not isinstance(top, Form):
            continue
        for node in _executed(top):
            head = _head_name(node)
            rest = node.items[1:]
            if head in ("setq", "setq-local", "setq-default"):
                scope = "buffer-local" if head == "setq-local" else "global"
                for position in range(0, len(rest), 2):
                    target = rest[position]
                    if isinstance(target, Atom) and target.kind == "symbol":
                        record(target.text, target.line, "write", scope, head)
                        written.add((target.text, target.line))
            elif head in _WRITES and rest:
                place = rest[1] if head in _PLACE_SECOND and len(rest) > 1 else rest[0]  # (push 値 場所) / (add-to-list '場所 値) は場所が第2引数
                if isinstance(place, Atom) and place.kind == "symbol":
                    mode = "mutate" if head in ("push", "pop", "add-to-list", "cl-pushnew", "cl-incf", "cl-decf", "incf", "decf") else "write"
                    record(place.text, place.line, "write" if head in ("setf", "cl-callf") else mode, "global", head)
                    written.add((place.text, place.line))
                elif isinstance(place, Form) and len(place.items) > 1 and isinstance(place.items[1], Atom) and place.items[1].kind == "symbol":
                    record(place.items[1].text, place.items[1].line, "mutate", "global", f"{head} ({_head_name(place)})")  # (setf (alist-get k var) v)
                    written.add((place.items[1].text, place.items[1].line))
            elif head in _MUTATORS and len(rest) > _MUTATED_INDEX.get(head, 0) and isinstance(rest[_MUTATED_INDEX.get(head, 0)], Atom):
                target = rest[_MUTATED_INDEX.get(head, 0)]
                if isinstance(target, Atom) and target.kind == "symbol" and not target.quoted:
                    record(target.text, target.line, "mutate", "global", head)
            elif head in _SET_FORMS and rest:
                quoted = _quoted_symbol(rest[0])
                if quoted and head in ("set", "set-default", "make-local-variable", "make-variable-buffer-local"):
                    record(quoted, node.line, "write", "global", head)
            elif head in _HOOK_FORMS and rest:
                quoted = _quoted_symbol(rest[0])
                if quoted:
                    record(quoted, node.line, "write", "hook", f"{head} → {_text(rest[1]) if len(rest) > 1 else ''}")
    # 読み取り: プロジェクトで定義されたグローバル変数への参照（書き込みの位置は除く）
    for top in body:
        if not isinstance(top, Form):
            continue
        for node in _executed(top):
            for item in node.items:
                if isinstance(item, Atom) and item.kind == "symbol" and not item.quoted and item.text in known_globals and (item.text, item.line) not in written:
                    if node.items and item is node.items[0]:
                        continue  # 関数名の位置
                    record(item.text, item.line, "read", "global", "参照")
    state.accesses.sort(key=lambda a: (a.line, a.name))
    return state

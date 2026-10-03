"""Emacs Lispの関数単位のデータフロー（変数の定義・使用・伝播）・戻り値・リスクの手がかり。流れ非依存の近似。

実行順序・条件・値は考慮しない。マクロの展開後、`pcase`・`cl-destructuring-bind` のパターンで束縛される変数、
別名（同じオブジェクトを指す別の変数）、リストやハッシュテーブルの要素は追えない。引用されたデータの中は辿らない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from codeinsight.analysis import flow_analysis as fa
from codeinsight.analysis.elisp_analyzer import _NO_CALL, Atom, Form, _head_name, _quoted_symbol
from codeinsight.analysis.elisp_flow_analysis import _CONDITION_CASE, _executed, _is_code, _local_names, function_parts

_BINDERS = frozenset({"let", "let*", "letrec", "dlet", "lexical-let", "if-let", "if-let*", "when-let", "when-let*", "and-let*", "while-let"})
_SETQ = frozenset({"setq", "setq-local", "setq-default"})
_AUGMENT = frozenset({"push", "add-to-list", "cl-pushnew", "cl-incf", "cl-decf", "incf", "decf", "add-to-ordered-list"})
_PLACE_SECOND = frozenset({"push", "cl-pushnew"})
_STORE_FIRST = {"aset": 2, "setcar": 1, "setcdr": 1, "nconc": 1, "puthash": 2, "set-char-table-range": 2}  # 書き込み先（オブジェクト）の引数の位置
_STORE_TARGET = {"puthash": 2}
_TAIL_PASS = frozenset({"progn", "let", "let*", "letrec", "dlet", "save-excursion", "save-restriction", "save-current-buffer", "with-temp-buffer",
                        "cl-block", "with-no-warnings", "with-suppressed-warnings", "eval-when-compile", "eval-and-compile", "save-match-data"})


# --- 表記 ---


def render(item: Atom | Form, limit: int = 70) -> str:
    text = _render(item)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _render(item: Atom | Form) -> str:
    if isinstance(item, Atom):
        if item.kind == "string":
            return '"' + " ".join(item.text.split())[:40] + '"'
        return ("'" if item.quoted else "") + ("#'" if item.function_quote else "") + item.text
    inner = " ".join(_render(i) for i in item.items)
    return ("'" if item.quoted else "") + ("[" + inner + "]" if item.vector else "(" + inner + ")")


def _symbols(item: Atom | Form) -> list[str]:
    """式の中で評価される変数（記号）の名前。引用・関数名の位置・キーワードは除く。"""

    names: list[str] = []
    if isinstance(item, Atom):
        if item.kind == "symbol" and not item.quoted and not item.function_quote and not item.text.startswith(":") and item.text not in ("nil", "t"):
            names.append(item.text)
        return names
    if item.quoted or item.vector:
        return names
    for position, sub in enumerate(item.items):
        if position == 0 and isinstance(sub, Atom):
            continue  # 関数名
        names.extend(_symbols(sub))
    return names


# --- 戻り値 ---


@dataclass(frozen=True)
class ElispReturn:
    line: int
    text: str
    note: str = ""  # 暗黙の nil など


def tail_expressions(body: list[Atom | Form]) -> list[tuple[Atom | Form | None, int, str]]:
    """本体の最後に評価され、値になりうる式（None は暗黙の nil。行は由来の式の行）。"""

    if not body:
        return [(None, 0, "本体が空")]
    return _tails(body[-1])


def _tails(item: Atom | Form) -> list[tuple[Atom | Form | None, int, str]]:
    if not _is_code(item):
        return [(item, getattr(item, "line", 0), "")]
    name = _head_name(item)
    rest = item.items[1:]
    if name in _TAIL_PASS:
        return _tails(rest[-1]) if rest else [(None, item.line, "")]
    if name in ("with-current-buffer", "with-temp-file", "with-output-to-string", "prog2", "save-window-excursion"):
        return _tails(rest[-1]) if len(rest) > 1 else [(None, item.line, "")]
    if name == "prog1" and rest:
        return _tails(rest[0])
    if name == "if" and len(rest) >= 2:
        result = _tails(rest[1])
        return result + (_tails(rest[-1]) if len(rest) > 2 else [(None, item.line, f"L{item.line} の if の条件が偽で else が無い場合の nil")])
    if name in ("when", "unless") and rest:
        body: list[tuple[Atom | Form | None, int, str]] = _tails(rest[-1]) if len(rest) > 1 else [(None, item.line, "")]
        return body + [(None, item.line, f"L{item.line} の {name} の条件が成り立たない場合の nil")]
    if name == "cond":
        result = []
        exhaustive = False
        for clause in rest:
            if isinstance(clause, Form) and not clause.quoted and clause.items:
                test = clause.items[0]
                result += _tails(clause.items[-1]) if len(clause.items) > 1 else [(test, clause.line, "条件式の値")]
                exhaustive = isinstance(test, Atom) and test.text in ("t", ":else", "otherwise")
        return result + ([] if exhaustive else [(None, item.line, f"L{item.line} の cond のどの節も成り立たない場合の nil")])
    if name in _CONDITION_CASE and len(rest) >= 2:
        result = _tails(rest[1])
        for handler in rest[2:]:
            if isinstance(handler, Form) and len(handler.items) > 1:
                result += _tails(handler.items[-1])
        return result
    if name == "unwind-protect" and rest:
        return _tails(rest[0])
    if name in ("pcase", "cl-case", "pcase-exhaustive", "cl-ecase", "ecase") and len(rest) > 1:
        result = []
        for clause in rest[1:]:
            if isinstance(clause, Form) and len(clause.items) > 1:
                result += _tails(clause.items[-1])
        return result + ([] if name.endswith(("exhaustive", "ecase")) else [(None, item.line, f"L{item.line} の {name} のどの節も一致しない場合の nil")])
    if name == "and" and rest:
        return _tails(rest[-1]) + [(None, item.line, f"L{item.line} の and の途中の式が偽の場合の nil")]
    if name == "or":
        return [(sub, sub.line, "or の各式のうち最初の非nil") for sub in rest] or [(None, item.line, "")]
    if name in ("dolist", "dotimes", "while", "cl-loop") and name != "cl-loop":
        return [(None, item.line, f"{name} は（結果式がなければ）nil を返す")]
    return [(item, item.line, "")]


def returns_of(definition: Form) -> list[ElispReturn]:
    _, body = function_parts(definition)
    results: list[ElispReturn] = []
    for expr, line, note in tail_expressions(body):
        if expr is None:
            results.append(ElispReturn(line or definition.end_line, "nil", note or "暗黙の nil"))
        else:
            results.append(ElispReturn(getattr(expr, "line", line), render(expr), note))
    seen = {(r.line, r.text) for r in results}
    for top in body:
        if not isinstance(top, Form):
            continue
        for node in _executed(top):
            if _head_name(node) in ("cl-return", "cl-return-from") and not node.quoted:
                value = node.items[-1] if len(node.items) > (2 if _head_name(node) == "cl-return-from" else 1) else None
                text = render(value) if value is not None else "nil"
                if (node.line, text) not in seen:
                    results.append(ElispReturn(node.line, text, f"{_head_name(node)} による早期の戻り"))
    return sorted(results, key=lambda r: (r.line, r.text))


# --- データフロー ---


@dataclass
class ElispVariable(fa.VariableInfo):
    scope: str = "local"  # param / local / global


class _Dataflow:
    def __init__(self, definition: Form, known_globals: set[str]) -> None:
        self.params, self.body = function_parts(definition)
        self.locals = _local_names(definition)
        self.globals = known_globals
        self.info: dict[str, ElispVariable] = {}
        for name in self.params:
            self._var(name).is_param = True
            self._var(name).definitions.append(fa.Definition(definition.line, "param", "", ()))
            self._var(name).scope = "param"

    def _var(self, name: str) -> ElispVariable:
        if name not in self.info:
            self.info[name] = ElispVariable(name)
        return self.info[name]

    def define(self, name: str, line: int, how: str, expr: Atom | Form | None) -> None:
        deps = tuple(dict.fromkeys(_symbols(expr))) if expr is not None else ()
        origin = render(expr) if expr is not None else ""
        self._var(name).definitions.append(fa.Definition(line, how, origin, deps))
        if expr is None:
            return
        if isinstance(expr, Atom) and deps == (expr.text,):
            self._var(expr.text).flows.append(fa.Flow("copy", line, name))
        else:
            for dep in deps:
                self._var(dep).flows.append(fa.Flow("derive", line, name))

    def run(self) -> dict[str, ElispVariable]:
        for item in self.body:
            self.walk(item)
        for expr, _, _ in tail_expressions(self.body):
            if expr is None:
                continue
            for name in dict.fromkeys(_symbols(expr)):
                text = render(expr)
                self._var(name).flows.append(fa.Flow("return", getattr(expr, "line", 0), text))
        tracked = self.locals | self.globals
        result = {}
        for name, variable in self.info.items():
            if name not in tracked and not variable.is_param:
                continue
            variable.scope = "param" if variable.is_param else ("local" if name in self.locals else "global")
            variable.uses = sorted(set(variable.uses))
            result[name] = variable
        return result

    def use(self, atom: Atom) -> None:
        if atom.kind == "symbol" and not atom.quoted and not atom.function_quote and not atom.text.startswith(":") and atom.text not in ("nil", "t"):
            self._var(atom.text).uses.append(atom.line)

    def walk(self, item: Atom | Form) -> None:
        if isinstance(item, Atom):
            self.use(item)
            return
        if item.quoted or item.vector or not item.items:
            return
        name = _head_name(item)
        rest = item.items[1:]
        if name is None:
            for sub in item.items:
                self.walk(sub)
            return
        if name in _BINDERS:
            bindings = rest[0] if rest else None
            if isinstance(bindings, Form):
                for binding in bindings.items:
                    if isinstance(binding, Atom) and binding.kind == "symbol":
                        self.define(binding.text, binding.line, name, None)
                    elif isinstance(binding, Form) and binding.items and isinstance(binding.items[0], Atom) and binding.items[0].kind == "symbol":
                        init = binding.items[1] if len(binding.items) > 1 else None
                        self.define(binding.items[0].text, binding.line, name, init)
                        if init is not None:
                            self.walk(init)
                    elif isinstance(binding, Form):
                        for sub in binding.items:
                            self.walk(sub)
            for sub in rest[1:]:
                self.walk(sub)
            return
        if name in _SETQ:
            for position in range(0, len(rest) - 1, 2):
                target, value = rest[position], rest[position + 1]
                if isinstance(target, Atom) and target.kind == "symbol":
                    self.define(target.text, target.line, name, value)
                self.walk(value)
            return
        if name in _AUGMENT and rest:
            place_index = 1 if name in _PLACE_SECOND and len(rest) > 1 else 0
            place = rest[place_index]
            augment_value: Atom | Form | None = rest[1 - place_index] if len(rest) > 1 else None
            if isinstance(place, Atom) and place.kind == "symbol":
                quoted_place = place.text
                self.define(quoted_place, place.line, name, augment_value)
                self.use(place)  # 読み取りと書き込みの両方
            for sub in rest:
                if sub is not place:
                    self.walk(sub)
            return
        if name == "setf":
            for position in range(0, len(rest) - 1, 2):
                place, value = rest[position], rest[position + 1]
                if isinstance(place, Atom) and place.kind == "symbol":
                    self.define(place.text, place.line, "setf", value)
                elif isinstance(place, Form) and len(place.items) > 1 and isinstance(place.items[1], Atom) and place.items[1].kind == "symbol":
                    root = place.items[1].text
                    self._var(root).definitions.append(fa.Definition(place.line, "mutate", render(place), tuple(dict.fromkeys(_symbols(value)))))
                    for dep in dict.fromkeys(_symbols(value)):
                        self._var(dep).flows.append(fa.Flow("attr_store", place.line, f"{root}（{render(place)}）"))
                    self.use(place.items[1])
                    for sub in place.items[2:]:
                        self.walk(sub)
                self.walk(value)
            return
        if name in ("dolist", "dotimes", "cl-dolist", "cl-dotimes") and rest and isinstance(rest[0], Form) and len(rest[0].items) >= 2 and isinstance(rest[0].items[0], Atom):
            spec = rest[0]
            variable = spec.items[0]
            assert isinstance(variable, Atom)
            self.define(variable.text, spec.line, "for", spec.items[1])
            for sub in spec.items[1:]:
                self.walk(sub)
            for sub in rest[1:]:
                self.walk(sub)
            return
        if name in _CONDITION_CASE and rest and isinstance(rest[0], Atom) and rest[0].text != "nil":
            self.define(rest[0].text, item.line, "except", None)
            for sub in rest[1:2]:
                self.walk(sub)
            for handler in rest[2:]:
                if isinstance(handler, Form):
                    for sub in handler.items[1:]:
                        self.walk(sub)
            return
        if name in ("lambda", "cl-flet", "cl-labels") and rest:
            if name == "lambda" and isinstance(rest[0], Form):
                for arg in rest[0].items:
                    if isinstance(arg, Atom) and arg.kind == "symbol" and not arg.text.startswith("&"):
                        self.define(arg.text, arg.line, "lambda", None)
                for sub in rest[1:]:
                    self.walk(sub)
                return
        if name in ("cond", "pcase", "cl-case", "pcase-exhaustive", "cl-ecase", "ecase", "pcase-let", "pcase-let*", "cl-destructuring-bind", "named-let"):
            # 節・パターン: パターンの記号は変数の使用ではない。式（先頭の被検査式と、各節の本体）だけを辿る
            if name == "cond":
                for clause in rest:
                    if isinstance(clause, Form) and not clause.quoted:
                        for sub in clause.items:
                            self.walk(sub)
            else:
                for position, sub in enumerate(rest):
                    if position == 0 and name not in ("pcase-let", "pcase-let*", "cl-destructuring-bind", "named-let"):
                        self.walk(sub)
                    elif name in ("pcase-let", "pcase-let*") and position == 0 and isinstance(sub, Form):
                        for binding in sub.items:
                            if isinstance(binding, Form) and len(binding.items) > 1:
                                self.walk(binding.items[1])
                    elif name == "cl-destructuring-bind" and position == 1:
                        self.walk(sub)
                    elif isinstance(sub, Form) and not sub.quoted and position > 0:
                        for clause_item in (sub.items[1:] if name not in ("pcase-let", "pcase-let*", "cl-destructuring-bind", "named-let") else [sub]):
                            self.walk(clause_item)
            return
        if name in _STORE_FIRST and len(rest) >= _STORE_FIRST[name]:
            target_index = _STORE_TARGET.get(name, 0)
            store_target: Atom | Form | None = rest[target_index] if target_index < len(rest) else None
            if isinstance(store_target, Atom) and store_target.kind == "symbol":
                values = [sub for position, sub in enumerate(rest) if position != target_index]
                deps = list(dict.fromkeys(n for v in values for n in _symbols(v)))
                self._var(store_target.text).definitions.append(fa.Definition(item.line, "mutate", f"({name} …)", tuple(deps)))
                for dep in deps:
                    self._var(dep).flows.append(fa.Flow("attr_store", item.line, f"{store_target.text}（{name}）"))
        if name not in _NO_CALL and name not in ("funcall", "apply"):
            for position, arg in enumerate(rest):
                if isinstance(arg, Atom) and arg.kind == "symbol" and not arg.quoted and not arg.text.startswith(":") and arg.text not in ("nil", "t"):
                    self._var(arg.text).flows.append(fa.Flow("call_arg", arg.line, name, item.line, f"({name} …)", position))
        elif name in ("funcall", "apply") and rest:
            for position, arg in enumerate(rest[1:]):
                if isinstance(arg, Atom) and arg.kind == "symbol" and not arg.quoted and not arg.text.startswith(":") and arg.text not in ("nil", "t"):
                    callee = _quoted_symbol(rest[0]) or (rest[0].text if isinstance(rest[0], Atom) and rest[0].function_quote else name)
                    self._var(arg.text).flows.append(fa.Flow("call_arg", arg.line, callee, item.line, f"({name} … )", position))
        for sub in rest:
            self.walk(sub)


def analyze_variables(definition: Form, known_globals: set[str]) -> dict[str, ElispVariable]:
    return _Dataflow(definition, known_globals).run()


# --- リスクの手がかり ---


@dataclass(frozen=True)
class ElispRiskHit:
    rule: str
    line: int
    detail: str


_SHELL = frozenset({"shell-command", "shell-command-to-string", "async-shell-command", "call-process-shell-command", "process-lines-shell"})
_BUILT = frozenset({"format", "concat", "format-message", "mapconcat", "string-join"})
_REDEFINE = frozenset({"fset", "advice-add", "defadvice", "cl-letf", "cl-letf*", "defalias"})
_TODO = re.compile(r";.*\b(TODO|FIXME|XXX|HACK)\b")


def scan_risks(definition: Form, lines: list[str] | None = None) -> list[ElispRiskHit]:
    hits: list[ElispRiskHit] = []
    _, body = function_parts(definition)
    for top in body:
        if not isinstance(top, Form):
            continue
        for node in _executed(top):
            name = _head_name(node)
            rest = node.items[1:]
            if name == "eval" and rest and not (isinstance(rest[0], Form) and rest[0].quoted) and not (isinstance(rest[0], Atom) and rest[0].kind != "symbol"):
                hits.append(ElispRiskHit("eval-exec", node.line, f"eval {render(rest[0], 40)}"))
            elif name in _SHELL and rest:
                built = any(isinstance(a, Form) and _head_name(a) in _BUILT for a in rest[:1])
                detail = f"{name} {render(rest[0], 50)}" + ("（文字列を組み立てたコマンド）" if built else "")
                hits.append(ElispRiskHit("command-exec", node.line, detail))
            elif name in ("call-process", "start-process", "make-process", "process-file") and rest:
                shell = any(isinstance(a, Atom) and a.kind == "string" and a.text in ("sh", "bash", "zsh", "/bin/sh", "/bin/bash", "-c") for a in rest)
                if shell:
                    hits.append(ElispRiskHit("command-exec", node.line, f"{name} … シェルを介するコマンド"))
            elif name in _REDEFINE:
                target = _quoted_symbol(rest[0]) if rest else None
                hits.append(ElispRiskHit("global-redefinition", node.line, f"{name} {target or (render(rest[0], 40) if rest else '')}（関数の定義・挙動をグローバルに変更する）"))
            elif name == "add-hook" and len(rest) >= 2:
                function = rest[1]
                if isinstance(function, Form) and not function.quoted and _head_name(function) == "lambda":
                    hits.append(ElispRiskHit("anonymous-hook", node.line, f"add-hook {render(rest[0], 30)} に無名関数（再評価で重複し、remove-hook で外せない）"))
            elif name in ("sleep-for",):
                hits.append(ElispRiskHit("blocking-call", node.line, f"{name} {render(rest[0], 20) if rest else ''}（Emacs全体が待機する）"))
            elif name == "ignore-errors":
                hits.append(ElispRiskHit("swallowed-exception", node.line, "ignore-errors（すべてのエラーを握りつぶす）"))
            elif name in _CONDITION_CASE:
                for handler in rest[2:]:
                    if not (isinstance(handler, Form) and handler.items):
                        continue
                    conditions = [handler.items[0].text] if isinstance(handler.items[0], Atom) else [i.text for i in handler.items[0].items if isinstance(i, Atom)]
                    bodies = handler.items[1:]
                    swallowed = all((isinstance(b, Atom) and b.text == "nil") or (isinstance(b, Form) and _head_name(b) == "ignore") for b in bodies) if bodies else True
                    if "t" in conditions:
                        hits.append(ElispRiskHit("bare-except", handler.line, "condition-case が t（quit を含むすべてのシグナル）を捕捉"))
                    if swallowed:
                        hits.append(ElispRiskHit("swallowed-exception", handler.line, f"condition-case の {', '.join(conditions)} を捕捉して何もしない"))
    if lines is not None:
        for number in range(definition.line, definition.end_line + 1):
            if number - 1 < len(lines) and (match := _TODO.search(lines[number - 1])):
                hits.append(ElispRiskHit("todo-marker", number, match.group(0).strip()[:100]))
    unique = {(h.rule, h.line, h.detail): h for h in hits}
    return sorted(unique.values(), key=lambda h: (h.line, h.rule))

"""Emacs Lispの関数の制御フロー図（基本ブロックと分岐の有向グラフ）。S式の構文から作る近似。

式の中に埋め込まれた分岐（`(setq x (if …))` など）は分解せず、1つの処理として示す。マクロの展開後（独自マクロ・
`use-package` など）と、`cl-loop` の内部は示さない。エラー（シグナル）は、囲む `condition-case` へ、なければ「シグナルで終了」へつなぐ。
"""

from __future__ import annotations

from codeinsight.analysis.elisp_analyzer import Atom, Form, _head_name
from codeinsight.analysis.elisp_dataflow_analysis import render
from codeinsight.analysis.elisp_flow_analysis import function_parts
from codeinsight.analysis.elisp_forms import (
    CONDITION_CASE_FORMS,
    EXHAUSTIVE_MATCH_SUFFIXES,
    IF_FORMS,
    LOOP_FORMS,
    MATCH_FORMS,
    RETURN_FORMS,
    SIGNAL_FORMS,
    TERMINATE_FORMS,
    WHEN_FORMS,
)
from codeinsight.application.cfg_base import CfgBase, Exit, Loop
from codeinsight.application.graph_builder import GraphModel

_IF = IF_FORMS
_WHEN = WHEN_FORMS
_MATCH = MATCH_FORMS
_LOOPS = LOOP_FORMS
_SIGNALS = frozenset(SIGNAL_FORMS)
_RETURNS = RETURN_FORMS
_TERMINATE = TERMINATE_FORMS
_WRAPPERS = frozenset({"progn", "save-excursion", "save-restriction", "save-current-buffer", "with-temp-buffer", "cl-block", "with-no-warnings",
                       "with-suppressed-warnings", "eval-when-compile", "eval-and-compile", "save-match-data", "prog1", "catch"})
_WRAPPERS_SKIP_FIRST = frozenset({"let", "let*", "letrec", "dlet", "lexical-let", "with-current-buffer", "with-temp-file", "with-output-to-string", "cl-letf",
                                  "cl-letf*", "condition-case-wrapper"})


class LispCfgBuilder(CfgBase):
    def build(self, definition: Form, path: str, title: str) -> GraphModel:
        self._reset(path)
        params, body = function_parts(definition)
        name = definition.items[1].text if len(definition.items) > 1 and isinstance(definition.items[1], Atom) else "?"
        start = self._node("terminal", f"開始: {name}", definition.line)
        end = self._node("terminal", "終了", definition.end_line)
        self._end = end
        body = list(body)
        while body and (
            (isinstance(body[0], Atom) and body[0].kind == "string" and len(body) > 1)  # docstring
            or (isinstance(body[0], Form) and _head_name(body[0]) in ("interactive", "declare"))
        ):
            body.pop(0)
        self._connect(self._block(body, [(start, "")]), end)
        return GraphModel(
            title=f"制御フロー: {title}",
            graph_kind="flow",
            nodes=self._nodes,
            edges=self._edges,
            notes=[
                "S式の構文から作った制御フローの近似です。実行時に通る経路・到達可能性は示しません。",
                "式の中に埋め込まれた分岐、マクロの展開後、cl-loop の内部は分解しません。",
                "エラー（signal・error など）は、囲む condition-case へ、なければ「シグナルで終了」へつなぎます。",
            ],
            meta={"function": name, "path": path},
        )

    def _label(self, form: Form | Atom, prefix: str = "", limit: int = 50) -> str:
        return f"L{form.line}: {prefix}{render(form, limit)}".rstrip()

    def _block(self, items: list[Atom | Form], entering: list[Exit]) -> list[Exit]:
        exits = entering
        pending: list[Atom | Form] = []

        def flush() -> None:
            nonlocal exits
            if not pending:
                return
            first = pending[0]
            more = f"  (+{len(pending) - 1}式)" if len(pending) > 1 else ""
            node = self._node("block", self._label(first) + more, first.line)
            self._connect(exits, node)
            exits = [(node, "")]
            pending.clear()

        for item in items:
            if not exits and not pending:
                break  # 到達できない式（シグナル・return の後）は図に含めない
            if not isinstance(item, Form) or item.quoted or item.vector or not item.items:
                pending.append(item)
                continue
            name = _head_name(item)
            if name is None or not self._is_control(name):
                pending.append(item)
                continue
            flush()
            if not exits:
                break
            exits = self._form(item, name, exits)
        flush()
        return exits

    def _clause_body(self, items: list[Atom | Form], decision: str, label: str) -> list[Exit]:
        """節の本体。本体が空なら、節の値（条件式の値など）で次へ進む。本体がシグナル・returnで終わるなら、次へは進まない。"""

        if not items:
            return [(decision, label)]
        return self._block(list(items), [(decision, label)])

    @staticmethod
    def _is_control(name: str) -> bool:
        return (name in _IF or name in _WHEN or name == "cond" or name in _MATCH or name in _LOOPS or name in CONDITION_CASE_FORMS or name in _SIGNALS
                or name in _RETURNS or name in _TERMINATE or name in _WRAPPERS or name in _WRAPPERS_SKIP_FIRST
                or name in ("ignore-errors", "unwind-protect", "with-demoted-errors"))

    def _form(self, form: Form, name: str, entering: list[Exit]) -> list[Exit]:
        rest = form.items[1:]
        exits: list[Exit]
        if name in _WRAPPERS or name in _WRAPPERS_SKIP_FIRST:
            body = rest[1:] if name in _WRAPPERS_SKIP_FIRST else rest
            if name == "catch":
                node = self._node("block", self._label(form, "", 30), form.line)
                self._connect(entering, node)
                return self._block(body[1:] if body else [], [(node, "")])
            return self._block(list(body), entering)
        if name in _IF:
            decision = self._node("decision", self._label(form.items[1] if rest else form, f"{name} "), form.line)
            self._connect(entering, decision)
            exits = self._block(rest[1:2], [(decision, "真")])
            if len(rest) > 2:
                exits += self._block(list(rest[2:]), [(decision, "偽")])
            else:
                exits.append((decision, "偽"))
            return exits
        if name in _WHEN:
            decision = self._node("decision", self._label(form.items[1] if rest else form, f"{name} "), form.line)
            self._connect(entering, decision)
            when_exits = self._block(list(rest[1:]), [(decision, "偽（nil）" if name == "unless" else "真")])
            return when_exits + [(decision, "真" if name == "unless" else "偽（nil）")]
        if name == "cond":
            exits = []
            previous: list[Exit] = entering
            for clause in rest:
                if not (isinstance(clause, Form) and clause.items) or clause.quoted:
                    continue
                test = clause.items[0]
                decision = self._node("decision", self._label(test, "cond "), clause.line)
                self._connect(previous, decision)
                exits += self._clause_body(clause.items[1:], decision, "真")
                previous = [(decision, "偽")]
                if isinstance(test, Atom) and test.text in ("t", ":else", "otherwise"):
                    previous = []
                    break
            return exits + previous
        if name in _MATCH:
            decision = self._node("decision", self._label(form.items[1] if rest else form, f"{name} "), form.line)
            self._connect(entering, decision)
            exits = []
            for clause in rest[1:]:
                if isinstance(clause, Form) and clause.items and not clause.quoted:
                    exits += self._clause_body(clause.items[1:], decision, f"case {render(clause.items[0], 24)}")
            if not name.endswith(EXHAUSTIVE_MATCH_SUFFIXES):
                exits.append((decision, "どれにも一致しない"))
            return exits
        if name in _LOOPS:
            head = self._node("decision", self._label(form.items[1] if rest else form, f"{name} ", 44), form.line)
            self._connect(entering, head)
            loop = Loop(head, [])
            self._loops.append(loop)
            body_exits = self._block(list(rest[1:]), [(head, "繰り返す")])
            self._loops.pop()
            self._connect([(n, "次の繰り返し") for n, _ in body_exits], head)
            return [(head, "終了")] + loop.breaks
        if name in CONDITION_CASE_FORMS:
            try_node = self._node("block", f"L{form.line}: {name} {render(rest[0], 14) if rest else ''}".strip(), form.line)
            self._connect(entering, try_node)
            handler_nodes: list[tuple[Form, str]] = []
            for handler in rest[2:]:
                if isinstance(handler, Form) and handler.items:
                    conditions = render(handler.items[0], 30)
                    node = self._node("decision", f"L{handler.line}: 捕捉 {conditions}", handler.line)
                    handler_nodes.append((handler, node))
                    self._edge(try_node, node, "シグナル")
            self._handlers.append([node for _, node in handler_nodes])
            exits = self._block(list(rest[1:2]), [(try_node, "")])
            self._handlers.pop()
            for handler, node in handler_nodes:
                exits += self._clause_body(handler.items[1:], node, "")
            return exits
        if name in ("ignore-errors", "with-demoted-errors"):
            try_node = self._node("block", f"L{form.line}: {name}", form.line)
            self._connect(entering, try_node)
            swallow = self._node("decision", f"L{form.line}: エラーを握りつぶして続行", form.line)
            self._edge(try_node, swallow, "シグナル")
            self._handlers.append([swallow])
            exits = self._block(list(rest), [(try_node, "")])
            self._handlers.pop()
            return exits + [(swallow, "")]
        if name == "unwind-protect":
            guard = self._node("block", f"L{form.line}: unwind-protect", form.line)
            self._connect(entering, guard)
            body_exits = self._block(list(rest[:1]), [(guard, "")])
            cleanup_items = list(rest[1:])
            if not cleanup_items:
                return body_exits
            cleanup = self._node("block", f"L{cleanup_items[0].line}: 後始末（unwind-protect）", cleanup_items[0].line)
            self._connect(body_exits, cleanup)
            self._edge(guard, cleanup, "シグナル時も実行")
            return self._block(cleanup_items, [(cleanup, "")])
        if name in _SIGNALS:
            node = self._node("block", self._label(form, "", 44), form.line)
            self._connect(entering, node)
            self._terminate_raise(node, end_label="シグナルで終了", edge_label="シグナル")
            return []
        if name in _RETURNS:
            node = self._node("block", self._label(form, "", 44), form.line)
            self._connect(entering, node)
            self._edge(node, self._end)
            return []
        if name in _TERMINATE:
            node = self._node("block", self._label(form, "", 44), form.line)
            self._connect(entering, node)
            self._edge(node, self._end, "Emacsを終了")
            return []
        node = self._node("block", self._label(form), form.line)
        self._connect(entering, node)
        return [(node, "")]

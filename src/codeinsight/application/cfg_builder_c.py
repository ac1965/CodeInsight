"""Cの関数の制御フロー図（基本ブロックと分岐の有向グラフ）。Clang ASTの構文から作る近似。

マクロの展開内部、条件式の中の `&&`・`||`・三項演算子（短絡評価）は分解せず、条件を1つの分岐として示す。
関数ポインタ経由の呼び出し先、`setjmp`/`longjmp` による非局所ジャンプは示さない。`exit`/`abort` などの終了呼び出しは、終了へつなぐ。
"""

from __future__ import annotations

import clang.cindex as ci

from codeinsight.analysis import c_flow_analysis as cf
from codeinsight.application.cfg_base import CfgBase, Exit, Loop
from codeinsight.application.graph_builder import GraphModel

CK = ci.CursorKind


class CCfgBuilder(CfgBase):
    def build(self, function: cf.CFunction, path: str, title: str) -> GraphModel:
        self._reset(path)
        self._function = function
        self._breaks: list[list[Exit]] = []
        self._switches: list[str] = []
        self._labels: dict[str, str] = {}
        start = self._node("terminal", f"開始: {function.name}()", function.cursor.extent.start.line)
        end = self._node("terminal", "終了", function.cursor.extent.end.line)
        self._end = end
        body = next((c for c in cf._children(function.cursor) if c.kind == CK.COMPOUND_STMT), None)
        exits = self._block(cf._children(body) if body is not None else [], [(start, "")])
        self._connect(exits, end)
        return GraphModel(
            title=f"制御フロー: {title}",
            graph_kind="flow",
            nodes=self._nodes,
            edges=self._edges,
            notes=[
                "Clang ASTの構文から作った制御フローの近似です。実行時に通る経路・到達可能性は示しません。",
                "条件の中の && ・ || ・三項演算子（短絡評価）は分解しません。マクロの展開内部、関数ポインタの呼び出し先、setjmp/longjmp は示しません。",
                "exit・abort などの終了呼び出しは、終了へつなぎます。case が break なしで続く箇所は「落ち込み」の辺で示します。",
            ],
            meta={"function": function.name, "path": path},
        )

    def _text(self, cursor: ci.Cursor, limit: int = 50) -> str:
        return cf.text(self._function, cursor, limit)

    def _label(self, cursor: ci.Cursor, prefix: str = "", limit: int = 50) -> str:
        return f"L{cursor.extent.start.line}: {prefix}{self._text(cursor, limit)}".rstrip()

    def _label_node(self, name: str, line: int) -> str:
        if name not in self._labels:
            self._labels[name] = self._node("block", f"L{line}: {name}:", line)
        return self._labels[name]

    def _block(self, statements: list[ci.Cursor], entering: list[Exit]) -> list[Exit]:
        exits = entering
        pending: list[ci.Cursor] = []

        def flush() -> None:
            nonlocal exits
            if not pending:
                return
            first = pending[0]
            more = f"  (+{len(pending) - 1}文)" if len(pending) > 1 else ""
            node = self._node("block", self._label(first) + more, first.extent.start.line)
            self._connect(exits, node)
            exits = [(node, "")]
            pending.clear()

        for statement in statements:
            labelled = statement.kind in (CK.CASE_STMT, CK.DEFAULT_STMT, CK.LABEL_STMT)  # ラベルは、直前から到達できなくても、分岐・goto から到達する
            if not exits and not pending and not labelled:
                break
            kind = statement.kind
            if kind in (CK.DECL_STMT, CK.NULL_STMT) or (kind not in _CONTROL and not self._is_exit(statement)):
                pending.append(statement)
                continue
            flush()
            exits = self._statement(statement, exits)
        flush()
        return exits

    def _is_exit(self, statement: ci.Cursor) -> bool:
        return statement.kind == CK.CALL_EXPR and cf._is_exit_call(cf.call_name(statement)) == "terminate"

    def _statement(self, statement: ci.Cursor, entering: list[Exit]) -> list[Exit]:
        kind = statement.kind
        parts = cf._children(statement)
        if kind == CK.COMPOUND_STMT:
            return self._block(parts, entering)
        if kind == CK.IF_STMT and len(parts) >= 2:
            decision = self._node("decision", self._label(parts[0], "if "), statement.extent.start.line)
            self._connect(entering, decision)
            exits = self._block(self._as_list(parts[1]), [(decision, "True")])
            if len(parts) > 2:
                exits += self._block(self._as_list(parts[2]), [(decision, "False")])
            else:
                exits.append((decision, "False"))
            return exits
        if kind in (CK.WHILE_STMT, CK.FOR_STMT) and parts:
            body = parts[-1]
            header = " ".join(self._function.source[statement.extent.start.offset:body.extent.start.offset].decode("utf-8", errors="replace").split())
            head = self._node("decision", f"L{statement.extent.start.line}: {header[:50] + ('…' if len(header) > 50 else '')}", statement.extent.start.line)
            self._connect(entering, head)
            return self._loop(head, body, [(head, "繰り返す")], head)
        if kind == CK.DO_STMT and len(parts) >= 2:
            start = self._node("block", f"L{statement.extent.start.line}: do", statement.extent.start.line)
            self._connect(entering, start)
            condition = self._node("decision", self._label(parts[-1], "while "), parts[-1].extent.start.line)
            loop = Loop(condition, [])
            self._loops.append(loop)
            self._breaks.append(loop.breaks)
            body_exits = self._block(self._as_list(parts[0]), [(start, "")])
            self._breaks.pop()
            self._loops.pop()
            self._connect(body_exits, condition)
            self._edge(condition, start, "True（繰り返す）")
            return [(condition, "False")] + loop.breaks
        if kind == CK.SWITCH_STMT and parts:
            decision = self._node("decision", self._label(parts[0], "switch "), statement.extent.start.line)
            self._connect(entering, decision)
            breaks: list[Exit] = []
            self._breaks.append(breaks)
            self._switches.append(decision)
            body = parts[1] if len(parts) > 1 else None
            exits = self._block(self._as_list(body) if body is not None else [], [])
            self._switches.pop()
            self._breaks.pop()
            has_default = any(c.kind == CK.DEFAULT_STMT for c in (body.walk_preorder() if body is not None else []))
            return exits + breaks + ([] if has_default else [(decision, "どれにも一致しない")])
        if kind in (CK.CASE_STMT, CK.DEFAULT_STMT) and self._switches:
            decision = self._switches[-1]
            label = "default" if kind == CK.DEFAULT_STMT else f"case {self._text(parts[0], 24)}"
            inner = parts if kind == CK.DEFAULT_STMT else parts[1:]
            label_node = self._node("block", f"L{statement.extent.start.line}: {label}", statement.extent.start.line)
            self._edge(decision, label_node, label)
            self._connect([(n, "落ち込み") for n, _ in entering], label_node)  # 直前の case から break なしで続く
            return self._block(inner, [(label_node, "")])
        if kind == CK.RETURN_STMT:
            node = self._node("block", self._label(statement), statement.extent.start.line)
            self._connect(entering, node)
            self._edge(node, self._end)
            return []
        if kind == CK.BREAK_STMT:
            node = self._node("block", f"L{statement.extent.start.line}: break", statement.extent.start.line)
            self._connect(entering, node)
            if self._breaks:
                self._breaks[-1].append((node, "ループ・switch を抜ける"))
            return []
        if kind == CK.CONTINUE_STMT:
            node = self._node("block", f"L{statement.extent.start.line}: continue", statement.extent.start.line)
            self._connect(entering, node)
            if self._loops:
                self._edge(node, self._loops[-1].head, "次の繰り返し")
            return []
        if kind == CK.GOTO_STMT:
            node = self._node("block", self._label(statement), statement.extent.start.line)
            self._connect(entering, node)
            target = next((c for c in statement.walk_preorder() if c.kind == CK.LABEL_REF), None)
            if target is not None:
                self._edge(node, self._label_node(target.spelling, target.extent.start.line), "goto")
            return []
        if kind == CK.LABEL_STMT:
            node = self._label_node(statement.spelling, statement.extent.start.line)
            self._connect(entering, node)
            return self._block(parts, [(node, "")])
        if self._is_exit(statement):
            node = self._node("block", self._label(statement), statement.extent.start.line)
            self._connect(entering, node)
            self._edge(node, self._end, "プロセスを終了")
            return []
        node = self._node("block", self._label(statement), statement.extent.start.line)
        self._connect(entering, node)
        return [(node, "")]

    @staticmethod
    def _as_list(cursor: ci.Cursor) -> list[ci.Cursor]:
        return cf._children(cursor) if cursor.kind == CK.COMPOUND_STMT else [cursor]

    def _loop(self, head: str, body: ci.Cursor, entering: list[Exit], back_to: str) -> list[Exit]:
        loop = Loop(head, [])
        self._loops.append(loop)
        self._breaks.append(loop.breaks)
        body_exits = self._block(self._as_list(body), entering)
        self._breaks.pop()
        self._loops.pop()
        self._connect([(n, "次の繰り返し") for n, _ in body_exits], back_to)
        return [(head, "終了")] + loop.breaks


_CONTROL = frozenset({CK.COMPOUND_STMT, CK.IF_STMT, CK.WHILE_STMT, CK.FOR_STMT, CK.DO_STMT, CK.SWITCH_STMT, CK.CASE_STMT, CK.DEFAULT_STMT,
                      CK.RETURN_STMT, CK.BREAK_STMT, CK.CONTINUE_STMT, CK.GOTO_STMT, CK.LABEL_STMT})

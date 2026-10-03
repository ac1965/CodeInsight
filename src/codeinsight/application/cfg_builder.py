from __future__ import annotations

import ast
from dataclasses import dataclass

from codeinsight.analysis import flow_analysis as fa
from codeinsight.application.graph_builder import EdgeStyle, GraphEdge, GraphModel, GraphNode

_SIMPLE = (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr, ast.Import, ast.ImportFrom,
           ast.Pass, ast.Delete, ast.Global, ast.Nonlocal, ast.Assert)
Exit = tuple[str, str]  # (ノードID, 次へ進む辺のラベル)


@dataclass
class _Loop:
    head: str
    breaks: list[Exit]


class CfgBuilder:
    """関数のASTから、制御フロー図（基本ブロックと分岐の有向グラフ）を組み立てる（Python）。

    構文から作った近似で、実行時の到達可能性は保証しない。tryの内側の文は、どれも例外を
    送出しうるものとして、tryからexceptへの辺（「例外」）で表す。連続する単純な文は1つの
    ブロックにまとめる。
    """

    def build(self, function: ast.FunctionDef | ast.AsyncFunctionDef, path: str, title: str) -> GraphModel:
        self._path = path
        self._nodes: list[GraphNode] = []
        self._edges: list[GraphEdge] = []
        self._loops: list[_Loop] = []
        self._handlers: list[list[str]] = []
        self._counter = 0
        self._raise_exit: str | None = None

        start = self._node("terminal", f"開始: {function.name}()", function.lineno)
        end = self._node("terminal", "終了", getattr(function, "end_lineno", function.lineno))
        self._end = end
        exits = self._block(function.body, [(start, "")])
        self._connect(exits, end)
        return GraphModel(
            title=f"制御フロー: {title}",
            graph_kind="flow",
            nodes=self._nodes,
            edges=self._edges,
            notes=[
                "構文から作った制御フローの近似です。実行時に通る経路・到達可能性は示しません。",
                "tryの内側は、どの文も例外を送出しうるものとして、exceptへの辺（例外）で表しています。",
            ],
            meta={"function": function.name, "path": path},
        )

    # --- 部品 ---

    def _node(self, kind: str, label: str, line: int) -> str:
        node_id = f"n{self._counter}"
        self._counter += 1
        self._nodes.append(GraphNode(node_id, label, kind, self._path, line))
        return node_id

    def _edge(self, source: str, target: str, label: str = "") -> None:
        self._edges.append(
            GraphEdge(source, target, "flow", EdgeStyle.CONFIRMED, (f"{self._path}:{self._nodes[int(source[1:])].line}",), label=label)
        )

    def _connect(self, exits: list[Exit], target: str) -> None:
        for source, label in exits:
            self._edge(source, target, label)

    def _terminate_raise(self, source: str, label: str = "") -> None:
        if self._handlers:
            for handler in self._handlers[-1]:
                self._edge(source, handler, "例外" if not label else label)
        else:
            if self._raise_exit is None:
                self._raise_exit = self._node("terminal", "例外で終了", 0)
            self._edge(source, self._raise_exit, label)

    # --- 文 ---

    def _block(self, statements: list[ast.stmt], entering: list[Exit]) -> list[Exit]:
        exits = entering
        pending: list[ast.stmt] = []

        def flush() -> None:
            nonlocal exits
            if not pending:
                return
            first = pending[0]
            more = f"  (+{len(pending) - 1}文)" if len(pending) > 1 else ""
            node = self._node("block", f"L{first.lineno}: {fa.unparse(first, 48)}{more}", first.lineno)
            self._connect(exits, node)
            exits = [(node, "")]
            pending.clear()

        for statement in statements:
            if not exits and not pending:
                break  # 到達できない文（return/raise/break/continueの後）は図に含めない
            if isinstance(statement, _SIMPLE):
                pending.append(statement)
                continue
            flush()
            if not exits:
                break
            exits = self._statement(statement, exits)
        flush()
        return exits

    def _statement(self, statement: ast.stmt, entering: list[Exit]) -> list[Exit]:
        if isinstance(statement, ast.If):
            return self._if(statement, entering)
        if isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            return self._loop(statement, entering)
        if isinstance(statement, ast.Try) or statement.__class__.__name__ == "TryStar":
            return self._try(statement, entering)
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            node = self._node("block", f"L{statement.lineno}: with {', '.join(fa.unparse(i.context_expr, 30) for i in statement.items)}", statement.lineno)
            self._connect(entering, node)
            return self._block(statement.body, [(node, "")])
        if isinstance(statement, ast.Match):
            return self._match(statement, entering)
        if isinstance(statement, ast.Return):
            node = self._node("block", f"L{statement.lineno}: return {fa.unparse(statement.value, 40)}".rstrip(), statement.lineno)
            self._connect(entering, node)
            self._edge(node, self._end)
            return []
        if isinstance(statement, ast.Raise):
            node = self._node("block", f"L{statement.lineno}: raise {fa.unparse(statement.exc, 40)}".rstrip(), statement.lineno)
            self._connect(entering, node)
            self._terminate_raise(node)
            return []
        if isinstance(statement, ast.Break):
            node = self._node("block", f"L{statement.lineno}: break", statement.lineno)
            self._connect(entering, node)
            if self._loops:
                self._loops[-1].breaks.append((node, "ループ終了"))
            return []
        if isinstance(statement, ast.Continue):
            node = self._node("block", f"L{statement.lineno}: continue", statement.lineno)
            self._connect(entering, node)
            if self._loops:
                self._edge(node, self._loops[-1].head, "次の繰り返し")
            return []
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            node = self._node("block", f"L{statement.lineno}: def/class {statement.name}（定義のみ）", statement.lineno)
            self._connect(entering, node)
            return [(node, "")]
        node = self._node("block", f"L{statement.lineno}: {fa.unparse(statement, 48)}", statement.lineno)
        self._connect(entering, node)
        return [(node, "")]

    def _if(self, statement: ast.If, entering: list[Exit]) -> list[Exit]:
        decision = self._node("decision", f"L{statement.lineno}: {fa.unparse(statement.test, 44)}", statement.lineno)
        self._connect(entering, decision)
        exits = self._block(statement.body, [(decision, "True")])
        if statement.orelse:
            exits += self._block(statement.orelse, [(decision, "False")])
        else:
            exits.append((decision, "False"))
        return exits

    def _loop(self, statement: ast.For | ast.AsyncFor | ast.While, entering: list[Exit]) -> list[Exit]:
        if isinstance(statement, ast.While):
            label = f"L{statement.lineno}: while {fa.unparse(statement.test, 40)}"
        else:
            label = f"L{statement.lineno}: for {fa.unparse(statement.target, 20)} in {fa.unparse(statement.iter, 30)}"
        head = self._node("decision", label, statement.lineno)
        self._connect(entering, head)
        loop = _Loop(head, [])
        self._loops.append(loop)
        body_exits = self._block(statement.body, [(head, "繰り返す")])
        self._loops.pop()
        self._connect([(n, "次の繰り返し") for n, _ in body_exits], head)
        exits: list[Exit] = [(head, "終了")]
        if statement.orelse:
            exits = self._block(statement.orelse, [(head, "終了")])
        return exits + loop.breaks

    def _try(self, statement: ast.Try, entering: list[Exit]) -> list[Exit]:
        try_node = self._node("block", f"L{statement.lineno}: try", statement.lineno)
        self._connect(entering, try_node)
        handler_nodes: list[tuple[ast.ExceptHandler, str]] = []
        for handler in statement.handlers:
            types = ", ".join(fa._handler_types(handler))
            node = self._node("decision", f"L{handler.lineno}: except {types}", handler.lineno)
            handler_nodes.append((handler, node))
            self._edge(try_node, node, "例外")
        self._handlers.append([node for _, node in handler_nodes])
        exits = self._block(statement.body, [(try_node, "")])
        self._handlers.pop()
        if statement.orelse:
            exits = self._block(statement.orelse, exits)
        for handler, node in handler_nodes:
            exits += self._block(handler.body, [(node, "捕捉")])
        if statement.finalbody:
            first = statement.finalbody[0]
            final = self._node("block", f"L{first.lineno}: finally", first.lineno)
            self._connect(exits, final)
            exits = self._block(statement.finalbody, [(final, "")])
        return exits

    def _match(self, statement: ast.Match, entering: list[Exit]) -> list[Exit]:
        decision = self._node("decision", f"L{statement.lineno}: match {fa.unparse(statement.subject, 40)}", statement.lineno)
        self._connect(entering, decision)
        exits: list[Exit] = []
        for case in statement.cases:
            exits += self._block(case.body, [(decision, f"case {fa.unparse(case.pattern, 24)}")])
        exits.append((decision, "どれにも一致しない"))
        return exits

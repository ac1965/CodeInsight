from __future__ import annotations

import ast
import uuid
from pathlib import Path

from codeinsight.analysis.language_adapter import FileAnalysis
from codeinsight.domain import Language, Symbol, SymbolKind


class PythonAnalyzer:
    """標準ライブラリ ast を用いたPythonシンボル抽出器。

    Phase1では静的に確認できる定義（モジュール・クラス・関数・メソッド・
    グローバル変数・クラス変数）のみを対象とする。既知の制約：

    * 関数・メソッド本体内のネストした定義、ローカル変数、インスタンス変数
      （self.attr）は抽出しない（Phase1のスコープ外。ANALYSIS.md参照）。
    * if/try等の制御構文内で条件付きに定義されたクラス・関数は、
      モジュール/クラス直下の文のみを対象とするため抽出されない場合がある。
    * getattr等の動的な属性アクセス、importlibによる動的インポート、
      monkey patchingは、静的解析では確認できないため扱わない。
    * モジュールのqualified_nameはファイル名（stem）のみに基づく簡易的な
      ものであり、パッケージ階層は反映しない。
    """

    language = Language.PYTHON

    def analyze_file(self, file_id: str, absolute_path: Path) -> FileAnalysis:
        result = FileAnalysis()
        try:
            source = absolute_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            result.errors.append(f"ファイルを読み込めません: {exc}")
            return result

        try:
            tree = ast.parse(source, filename=str(absolute_path))
        except SyntaxError as exc:
            result.errors.append(f"構文解析に失敗しました: {exc.msg} (line {exc.lineno})")
            return result

        end_line = getattr(tree, "end_lineno", None) or len(source.splitlines()) or 1
        module_symbol = Symbol(
            symbol_id=str(uuid.uuid4()),
            file_id=file_id,
            name=absolute_path.stem,
            qualified_name=absolute_path.stem,
            kind=SymbolKind.MODULE,
            start_line=1,
            end_line=end_line,
        )
        result.symbols.append(module_symbol)

        self._walk_body(tree.body, file_id, module_symbol, result)
        return result

    def _walk_body(
        self,
        body: list[ast.stmt],
        file_id: str,
        parent: Symbol,
        result: FileAnalysis,
    ) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._handle_function(node, file_id, parent, result)
            elif isinstance(node, ast.ClassDef):
                self._handle_class(node, file_id, parent, result)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and parent.kind in (
                SymbolKind.MODULE,
                SymbolKind.CLASS,
            ):
                self._handle_assignment(node, file_id, parent, result)

    def _handle_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        file_id: str,
        parent: Symbol,
        result: FileAnalysis,
    ) -> None:
        kind = SymbolKind.METHOD if parent.kind == SymbolKind.CLASS else SymbolKind.FUNCTION
        symbol = Symbol(
            symbol_id=str(uuid.uuid4()),
            file_id=file_id,
            name=node.name,
            qualified_name=f"{parent.qualified_name}.{node.name}",
            kind=kind,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            parent_symbol_id=parent.symbol_id,
            decorators=tuple(self._unparse(d) for d in node.decorator_list),
            is_async=isinstance(node, ast.AsyncFunctionDef),
        )
        result.symbols.append(symbol)

    def _handle_class(
        self,
        node: ast.ClassDef,
        file_id: str,
        parent: Symbol,
        result: FileAnalysis,
    ) -> None:
        symbol = Symbol(
            symbol_id=str(uuid.uuid4()),
            file_id=file_id,
            name=node.name,
            qualified_name=f"{parent.qualified_name}.{node.name}",
            kind=SymbolKind.CLASS,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            parent_symbol_id=parent.symbol_id,
            decorators=tuple(self._unparse(d) for d in node.decorator_list),
            base_classes=tuple(self._unparse(b) for b in node.bases),
        )
        result.symbols.append(symbol)
        self._walk_body(node.body, file_id, symbol, result)

    def _handle_assignment(
        self,
        node: ast.Assign | ast.AnnAssign,
        file_id: str,
        parent: Symbol,
        result: FileAnalysis,
    ) -> None:
        kind = (
            SymbolKind.CLASS_VARIABLE
            if parent.kind == SymbolKind.CLASS
            else SymbolKind.GLOBAL_VARIABLE
        )
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for name, lineno, end_lineno in self._assignment_names(target):
                symbol = Symbol(
                    symbol_id=str(uuid.uuid4()),
                    file_id=file_id,
                    name=name,
                    qualified_name=f"{parent.qualified_name}.{name}",
                    kind=kind,
                    start_line=lineno,
                    end_line=end_lineno,
                    parent_symbol_id=parent.symbol_id,
                )
                result.symbols.append(symbol)

    def _assignment_names(self, target: ast.expr):
        # tuple/list代入 (a, b = 1, 2) に対応する。属性代入(self.x = ...)は
        # インスタンス変数でありPhase1のモジュール/クラス変数抽出対象外。
        if isinstance(target, ast.Name):
            yield target.id, target.lineno, target.end_lineno or target.lineno
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                yield from self._assignment_names(elt)

    @staticmethod
    def _unparse(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except Exception:
            return "<解析不能>"

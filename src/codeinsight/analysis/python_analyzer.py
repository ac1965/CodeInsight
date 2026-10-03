from __future__ import annotations

import ast
import builtins
from collections import Counter
from pathlib import PurePosixPath

from codeinsight.analysis.ids import IdAllocator, build_symbol
from codeinsight.analysis.language_adapter import FileAnalysis, SourceUnit
from codeinsight.domain import (
    Confidence,
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

_BUILTIN_NAMES = frozenset(dir(builtins))
_BUILTIN_CONTAINER_ANNOTATIONS = frozenset({"list", "dict", "set", "frozenset", "tuple"})
_LITERAL_RECEIVERS = (
    ast.Constant, ast.JoinedStr, ast.List, ast.Dict, ast.Set, ast.Tuple,
    ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp,
)
_DYNAMIC_IMPORT_FUNCTIONS = frozenset({"importlib.import_module", "builtins.__import__"})

# 参照の照合キーの接頭辞（ReferenceResolverが解釈する）。
KEY_DIRECT = "py:"  # 同一モジュール内で定義された名前（修飾名）
KEY_IMPORT = "pyimport:"  # import経由の名前（import元の修飾名）
KEY_SELF = "pyself:"  # self/cls経由のメソッド呼び出し  pyself:<クラス修飾名>:<属性>
KEY_SUPER = "pysuper:"  # super()経由のメソッド呼び出し  pysuper:<クラス修飾名>:<属性>
KEY_EXPORT = "pyexport:"  # モジュールが公開するimport名  pyexport:<公開名>=<import元の修飾名>
KEY_STAR = "pystar:"  # `from M import *` 経由の名前  pystar:<M;M...>|<名前>
KEY_TYPED = "pytyped:"  # 型注釈/単一代入で型を推定した変数経由  pytyped:<型の照合キー>|<属性>


def module_name_from_path(relative_path: str) -> tuple[str, bool]:
    """相対パスからモジュールの修飾名を求める。戻り値は (名前, __init__.pyか)。

    ``pkg/sub/mod.py`` -> ``pkg.sub.mod``、``pkg/__init__.py`` -> ``pkg``。
    ソースルートの位置は分からないため、``src/pkg/mod.py`` は ``src.pkg.mod`` となる
    （解決時は末尾一致で照合し、一意でなければ AMBIGUOUS とする）。
    """

    path = PurePosixPath(relative_path)
    parts = list(path.parent.parts) if path.parent != PurePosixPath(".") else []
    if path.stem == "__init__":
        return (".".join(parts) or "__init__"), True
    return ".".join([*parts, path.stem]), False


def _join(*parts: str) -> str:
    return ".".join(part for part in parts if part)


class PythonAnalyzer:
    """標準ライブラリ ast を用いたPython解析器（シンボル・参照・依存関係）。

    既知の制約：

    * 関数・メソッド本体内のネストした定義、ローカル変数、インスタンス変数
      （self.attr）はシンボルとして抽出しない。ネスト関数内の呼び出しは外側の
      シンボルからの参照として記録する。
    * if/try等の制御構文内で条件付きに定義されたクラス・関数はシンボルとして
      抽出されない場合がある。
    * 呼び出し先は、名前・import・self/super から静的に辿れる範囲でのみ照合キーを
      作る。変数や式の評価結果に対する呼び出し、getattr等による動的な取得、
      importlibによる動的インポートは未解決（UNRESOLVED）として記録する。
    * 型アノテーションに基づく参照は抽出しない。
    """

    language = Language.PYTHON

    def analyze_file(self, unit: SourceUnit) -> FileAnalysis:
        result = FileAnalysis()
        try:
            source = unit.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            result.errors.append(f"ファイルを読み込めません: {exc}")
            return result

        try:
            tree = ast.parse(source, filename=str(unit.absolute_path))
        except (SyntaxError, ValueError) as exc:
            if isinstance(exc, SyntaxError):
                result.errors.append(f"構文解析に失敗しました: {exc.msg} (line {exc.lineno})")
            else:
                result.errors.append(f"構文解析に失敗しました: {exc}")
            return result

        module_name, is_init = module_name_from_path(unit.relative_path)
        ids = IdAllocator(unit.file_id)
        module_symbol = build_symbol(
            ids,
            unit.file_id,
            name=module_name.rsplit(".", 1)[-1],
            qualified_name=module_name,
            kind=SymbolKind.MODULE,
            start_line=1,
            end_line=len(source.splitlines()) or 1,
        )
        result.symbols.append(module_symbol)

        node_symbols: dict[int, Symbol] = {}
        builder = _SymbolBuilder(unit.file_id, ids, result, node_symbols)
        builder.walk_body(tree.body, module_symbol)

        module_names = {
            s.name for s in result.symbols if s.parent_symbol_id == module_symbol.symbol_id
        }
        bindings, star_modules = self._collect_imports(
            tree, unit.file_id, module_name, is_init, ids, result, module_symbol
        )
        _ReferenceCollector(
            file_id=unit.file_id,
            module_name=module_name,
            ids=ids,
            node_symbols=node_symbols,
            bindings=bindings,
            module_names=module_names,
            module_symbol=module_symbol,
            star_modules=star_modules,
            tree=tree,
            result=result,
        ).visit(tree)
        return result

    # --- import / 依存関係 ---

    def _collect_imports(
        self,
        tree: ast.Module,
        file_id: str,
        module_name: str,
        is_init: bool,
        ids: IdAllocator,
        result: FileAnalysis,
        module_symbol: Symbol,
    ) -> tuple[dict[str, str], list[str]]:
        """import文から依存関係とimport参照を作り、(名前->修飾名の対応表, star import元) を返す。"""

        bindings: dict[str, str] = {}
        star_modules: list[str] = []
        module_level = _module_level_imports(tree)

        def add_export(node: ast.stmt, exposed: str, dotted: str) -> None:
            """モジュールが公開する名前としてのimportを、IMPORT参照で記録する。

            再エクスポート（__init__.py経由のimport）やstar importの解決に使う。
            """

            if id(node) not in module_level:
                return
            key = f"{KEY_EXPORT}{exposed}={dotted}"
            line = node.lineno
            result.references.append(
                Reference(
                    reference_id=ids.reference_id(module_symbol.symbol_id, "import", key, line),
                    source_symbol_id=module_symbol.symbol_id,
                    target_name=dotted,
                    target_key=key,
                    reference_kind=ReferenceKind.IMPORT,
                    source_location=SourceLocation(file_id, line, node.end_lineno or line),
                )
            )

        def add_dependency(
            node: ast.stmt,
            target: str,
            status: ResolutionStatus = ResolutionStatus.UNRESOLVED,
            note: str = "",
            candidate: bool = False,
        ) -> None:
            line = node.lineno
            result.dependencies.append(
                Dependency(
                    dependency_id=ids.dependency_id("import", target, line),
                    source_file_id=file_id,
                    target_name=target,
                    target_key=target if status == ResolutionStatus.UNRESOLVED and not note else None,
                    dependency_kind=DependencyKind.IMPORT,
                    evidence_location=SourceLocation(file_id, line, node.end_lineno or line),
                    resolution_status=status,
                    note=note,
                    is_candidate=candidate,
                )
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add_dependency(node, alias.name)
                    if alias.asname:
                        bindings[alias.asname] = alias.name
                        add_export(node, alias.asname, alias.name)
                    else:
                        top = alias.name.split(".", 1)[0]
                        bindings[top] = top
                        add_export(node, top, top)
            elif isinstance(node, ast.ImportFrom):
                base = self._relative_base(module_name, is_init, node.level, node.module)
                if base is None:
                    add_dependency(
                        node,
                        "." * node.level + (node.module or ""),
                        ResolutionStatus.UNRESOLVED,
                        "相対importがパッケージの範囲を超えている",
                    )
                    continue
                if node.module is not None:
                    add_dependency(node, base)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    # 取り込まれる名前がサブモジュールの可能性があるため、候補として記録する。
                    add_dependency(node, _join(base, alias.name), candidate=True)
                for alias in node.names:
                    if alias.name == "*":
                        # 取り込まれる名前は、import元モジュールを解析してから解決する。
                        if base not in star_modules:
                            star_modules.append(base)
                        continue
                    exposed = alias.asname or alias.name
                    bindings[exposed] = _join(base, alias.name)
                    add_export(node, exposed, bindings[exposed])
        return bindings, star_modules

    @staticmethod
    def _relative_base(
        module_name: str, is_init: bool, level: int, module: str | None
    ) -> str | None:
        if level == 0:
            return module or ""
        package = module_name if is_init else module_name.rpartition(".")[0]
        parts = package.split(".") if package else []
        drop = level - 1
        if drop > len(parts):
            return None
        base = ".".join(parts[: len(parts) - drop])
        return _join(base, module or "")


class _SymbolBuilder:
    """モジュール/クラス直下の定義をシンボルとして抽出する。"""

    def __init__(
        self,
        file_id: str,
        ids: IdAllocator,
        result: FileAnalysis,
        node_symbols: dict[int, Symbol],
    ) -> None:
        self._file_id = file_id
        self._ids = ids
        self._result = result
        self._node_symbols = node_symbols

    def walk_body(self, body: list[ast.stmt], parent: Symbol) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(node, parent)
            elif isinstance(node, ast.ClassDef):
                self._class(node, parent)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and parent.kind in (
                SymbolKind.MODULE,
                SymbolKind.CLASS,
            ):
                self._assignment(node, parent)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, parent: Symbol) -> None:
        kind = SymbolKind.METHOD if parent.kind == SymbolKind.CLASS else SymbolKind.FUNCTION
        symbol = build_symbol(
            self._ids,
            self._file_id,
            name=node.name,
            qualified_name=f"{parent.qualified_name}.{node.name}",
            kind=kind,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            parent=parent,
            decorators=tuple(_unparse(d) for d in node.decorator_list),
            is_async=isinstance(node, ast.AsyncFunctionDef),
        )
        self._result.symbols.append(symbol)
        self._node_symbols[id(node)] = symbol

    def _class(self, node: ast.ClassDef, parent: Symbol) -> None:
        symbol = build_symbol(
            self._ids,
            self._file_id,
            name=node.name,
            qualified_name=f"{parent.qualified_name}.{node.name}",
            kind=SymbolKind.CLASS,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            parent=parent,
            decorators=tuple(_unparse(d) for d in node.decorator_list),
            base_classes=tuple(_unparse(b) for b in node.bases),
        )
        self._result.symbols.append(symbol)
        self._node_symbols[id(node)] = symbol
        self.walk_body(node.body, symbol)

    def _assignment(self, node: ast.Assign | ast.AnnAssign, parent: Symbol) -> None:
        kind = (
            SymbolKind.CLASS_VARIABLE
            if parent.kind == SymbolKind.CLASS
            else SymbolKind.GLOBAL_VARIABLE
        )
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            for name, lineno, end_lineno in _assignment_names(target):
                self._result.symbols.append(
                    build_symbol(
                        self._ids,
                        self._file_id,
                        name=name,
                        qualified_name=f"{parent.qualified_name}.{name}",
                        kind=kind,
                        start_line=lineno,
                        end_line=end_lineno,
                        parent=parent,
                    )
                )


class _ReferenceCollector(ast.NodeVisitor):
    """関数呼び出し・継承関係を、文脈（スコープ）を追跡しながら収集する。"""

    def __init__(
        self,
        *,
        file_id: str,
        module_name: str,
        ids: IdAllocator,
        node_symbols: dict[int, Symbol],
        bindings: dict[str, str],
        module_names: set[str],
        module_symbol: Symbol,
        star_modules: list[str],
        tree: ast.Module,
        result: FileAnalysis,
    ) -> None:
        self._file_id = file_id
        self._module_name = module_name
        self._ids = ids
        self._node_symbols = node_symbols
        self._bindings = bindings
        self._module_names = module_names
        self._result = result
        self._symbols: list[Symbol] = [module_symbol]
        self._locals: list[set[str]] = [set()]
        self._self_names: list[str | None] = [None]
        self._star_modules = star_modules
        self._class_attr_types: dict[str, dict[str, str]] = {}
        self._types: list[dict[str, str]] = [{}]
        # モジュール直下で1回だけ代入される変数の型（global宣言で書き換えられるものは除く）。
        reassigned = {name for n in ast.walk(tree) if isinstance(n, ast.Global) for name in n.names}
        self._module_types: dict[str, str] = {}  # 推定中に参照されるため、先に空で用意する
        self._module_types = {
            name: key
            for name, key in self._infer_scope(tree.body, []).items()
            if name not in reassigned
        }

    # --- スコープ追跡 ---

    def _enclosing_class(self) -> Symbol | None:
        for symbol in reversed(self._symbols):
            if symbol.kind == SymbolKind.CLASS:
                return symbol
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        symbol = self._node_symbols.get(id(node))
        for decorator in node.decorator_list:
            self.visit(decorator)
        if symbol is not None:
            for base in node.bases:
                self._inheritance(base, symbol)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        if symbol is not None:
            self._class_attr_types[symbol.qualified_name] = self._infer_class_attributes(node)
            self._symbols.append(symbol)
            self._locals.append(set())
            self._self_names.append(None)
            self._types.append({})
        for statement in node.body:
            self.visit(statement)
        if symbol is not None:
            self._symbols.pop()
            self._locals.pop()
            self._self_names.pop()
            self._types.pop()

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in [*node.args.defaults, *node.args.kw_defaults]:
            if default is not None:
                self.visit(default)

        symbol = self._node_symbols.get(id(node))
        local_names = _local_names(node)
        self_name: str | None = self._self_names[-1]
        if symbol is not None:
            self._symbols.append(symbol)
            self_name = None
            positional = [*node.args.posonlyargs, *node.args.args]
            is_static = any(_unparse(d) == "staticmethod" for d in node.decorator_list)
            if symbol.kind == SymbolKind.METHOD and positional and not is_static:
                self_name = positional[0].arg
        else:
            local_names = local_names | self._locals[-1]
        # 型の推定は、関数の外側のスコープで名前を解決して行う。
        inferred_types = self._infer_types(node)
        if symbol is None:
            inferred_types = {**self._types[-1], **inferred_types}
        self._locals.append(local_names)
        self._self_names.append(self_name)
        self._types.append(inferred_types)
        for statement in node.body:
            self.visit(statement)
        self._locals.pop()
        self._self_names.pop()
        self._types.pop()
        if symbol is not None:
            self._symbols.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        params = {a.arg for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
        self._locals.append(self._locals[-1] | params)
        self._types.append({k: v for k, v in self._types[-1].items() if k not in params})
        self.visit(node.body)
        self._locals.pop()
        self._types.pop()

    def _infer_types(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
        args = node.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        return self._infer_scope(node.body, params)

    def _infer_scope(self, body: list[ast.stmt], params: list[ast.arg]) -> dict[str, str]:
        """型注釈付きの引数と、1回だけ代入される変数の型を推定する。

        推定結果は照合キー（クラスの名前解決キー、または ``builtin:<型名>``）で返す。
        再代入される名前や、Optional/Union等の複合的な注釈は、静的に型を決められない
        ため対象外とする。
        """

        stores: Counter[str] = Counter()
        assigned: dict[str, ast.expr] = {}
        annotated: dict[str, str] = {}
        stack: list[ast.AST] = list(body)
        while stack:
            current = stack.pop()
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                if not isinstance(current, ast.Lambda):
                    stores[current.name] += 1
                continue
            if isinstance(current, ast.Name) and isinstance(current.ctx, (ast.Store, ast.Del)):
                stores[current.id] += 1
            elif isinstance(current, (ast.Import, ast.ImportFrom)):
                for alias in current.names:
                    stores[(alias.asname or alias.name).split(".", 1)[0]] += 1
            elif (
                isinstance(current, ast.Assign)
                and len(current.targets) == 1
                and isinstance(current.targets[0], ast.Name)
            ):
                assigned[current.targets[0].id] = current.value
            elif isinstance(current, ast.AnnAssign) and isinstance(current.target, ast.Name):
                key = self._type_key(self._annotation_parts(current.annotation))
                if key:
                    annotated[current.target.id] = key  # `x: T = ...` は注釈の型を優先する
            stack.extend(ast.iter_child_nodes(current))

        types: dict[str, str] = {}
        for arg in params:
            if arg.annotation is None or stores[arg.arg] > 0:
                continue
            key = self._type_key(self._annotation_parts(arg.annotation))
            if key:
                types[arg.arg] = key
        for name in assigned.keys() | annotated.keys():
            if stores[name] != 1:
                continue
            value = assigned.get(name)
            key = annotated.get(name) or (self._value_type(value) if value is not None else None)
            if key:
                types[name] = key
        return types

    def _infer_class_attributes(self, node: ast.ClassDef) -> dict[str, str]:
        """クラスの属性（クラス直下の定義と `self.X = ...`）のうち、型を静的に決められるもの。

        属性への代入がクラス全体で1回だけの場合に限り、注釈・生成するクラス・リテラル・
        型注釈付き引数の転記から型を推定する。複数回代入される属性は対象外とする。
        """

        stores: Counter[str] = Counter()
        candidates: dict[str, str] = {}

        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                stores[statement.target.id] += 1
                key = self._type_key(self._annotation_parts(statement.annotation))
                if key is None and statement.value is not None:
                    key = self._value_type(statement.value)
                if key:
                    candidates[statement.target.id] = key
            elif isinstance(statement, ast.Assign):
                for target in statement.targets:
                    if isinstance(target, ast.Name):
                        stores[target.id] += 1
                if len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
                    key = self._value_type(statement.value)
                    if key:
                        candidates[statement.targets[0].id] = key

        for statement in node.body:
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            positional = [*statement.args.posonlyargs, *statement.args.args]
            if not positional or any(_unparse(d) == "staticmethod" for d in statement.decorator_list):
                continue
            self_name = positional[0].arg
            parameter_types = self._infer_scope(
                statement.body, [*positional, *statement.args.kwonlyargs]
            )
            stack: list[ast.AST] = list(statement.body)
            while stack:
                current = stack.pop()
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    continue
                for attribute, value, annotation in self._self_attribute_stores(current, self_name):
                    stores[attribute] += 1
                    key = None
                    if annotation is not None:
                        key = self._type_key(self._annotation_parts(annotation))
                    if key is None and value is not None:
                        if isinstance(value, ast.Name) and value.id in parameter_types:
                            key = parameter_types[value.id]  # self.x = x （xは型注釈付き引数）
                        else:
                            key = self._value_type(value)
                    if key:
                        candidates[attribute] = key
                stack.extend(ast.iter_child_nodes(current))
        return {name: key for name, key in candidates.items() if stores[name] == 1}

    @staticmethod
    def _self_attribute_stores(node: ast.AST, self_name: str):
        """`self.X` への代入を (属性名, 代入される式, 型注釈) で列挙する。"""

        def is_self_attribute(target: ast.AST) -> bool:
            return (
                isinstance(target, ast.Attribute)
                and isinstance(target.ctx, ast.Store)
                and isinstance(target.value, ast.Name)
                and target.value.id == self_name
            )

        if isinstance(node, ast.Assign):
            for target in node.targets:
                if is_self_attribute(target):
                    single = len(node.targets) == 1
                    yield target.attr, (node.value if single else None), None
        elif isinstance(node, ast.AnnAssign) and is_self_attribute(node.target):
            yield node.target.attr, node.value, node.annotation
        elif isinstance(node, (ast.AugAssign,)) and is_self_attribute(node.target):
            yield node.target.attr, None, None
        elif isinstance(node, (ast.For, ast.AsyncFor)) and is_self_attribute(node.target):
            yield node.target.attr, None, None

    def _value_type(self, value: ast.expr) -> str | None:
        """代入される式から型の照合キーを推定する（クラスの生成・リテラル）。"""

        if isinstance(value, ast.Call):
            return self._type_key(self._dotted_parts(value.func))
        if isinstance(value, (ast.List, ast.ListComp)):
            return "builtin:list"
        if isinstance(value, (ast.Dict, ast.DictComp)):
            return "builtin:dict"
        if isinstance(value, (ast.Set, ast.SetComp)):
            return "builtin:set"
        if isinstance(value, ast.Tuple):
            return "builtin:tuple"
        if isinstance(value, (ast.JoinedStr,)) or (
            isinstance(value, ast.Constant) and isinstance(value.value, (str, bytes))
        ):
            return "builtin:str"
        return None

    def _annotation_parts(self, annotation: ast.expr) -> list[str] | None:
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return None
        if (
            isinstance(annotation, ast.Subscript)
            and isinstance(annotation.value, ast.Name)
            and annotation.value.id in _BUILTIN_CONTAINER_ANNOTATIONS
        ):
            annotation = annotation.value  # list[str] -> list
        return self._dotted_parts(annotation)

    def _type_key(self, parts: list[str] | None) -> str | None:
        if parts is None or parts[0] == "super()":
            return None
        key, status, _ = self._resolve_parts(parts, allow_self=False)
        if key and (key.startswith(KEY_DIRECT) or key.startswith(KEY_IMPORT)):
            return key
        if (
            key is None
            and status == ResolutionStatus.EXTERNAL
            and len(parts) == 1
            and isinstance(getattr(builtins, parts[0], None), type)
        ):
            return f"builtin:{parts[0]}"  # str, list, dict など組み込み型
        return None

    # --- 参照の記録 ---

    def visit_Call(self, node: ast.Call) -> None:
        self._call(node)
        self.generic_visit(node)

    def _add_reference(
        self,
        kind: ReferenceKind,
        node: ast.expr,
        symbol: Symbol,
        target_name: str,
        key: str | None,
        status: ResolutionStatus,
        note: str = "",
    ) -> None:
        start = node.lineno
        self._result.references.append(
            Reference(
                reference_id=self._ids.reference_id(
                    symbol.symbol_id, kind.value, key or target_name, start
                ),
                source_symbol_id=symbol.symbol_id,
                target_name=target_name,
                target_key=key,
                reference_kind=kind,
                source_location=SourceLocation(self._file_id, start, node.end_lineno or start),
                resolution_status=status,
                note=note,
            )
        )

    def _call(self, node: ast.Call) -> None:
        symbol = self._symbols[-1]
        func = node.func
        parts = self._dotted_parts(func)
        if parts is None and isinstance(func, ast.Attribute):
            receiver = func.value
            if isinstance(receiver, _LITERAL_RECEIVERS):
                self._add_reference(
                    ReferenceKind.CALL, node, symbol, _unparse(func)[:80], None,
                    ResolutionStatus.EXTERNAL, "組み込み型のメソッド",
                )
                return
            if isinstance(receiver, ast.Call):
                # `クラス(...).メソッド()` は、生成されるクラスのメソッドとして解決する。
                typed = self._typed(self._type_key(self._dotted_parts(receiver.func)), [func.attr])
                if typed is not None:
                    key, status, note = typed
                    self._add_reference(
                        ReferenceKind.CALL, node, symbol, _unparse(func)[:80], key, status, note
                    )
                    return
        if parts is None:
            note = "呼び出し対象が式の評価結果であり、静的に確定できない"
            if isinstance(func, ast.Call) and self._dotted_parts(func.func) == ["getattr"]:
                note = "getattr による動的な属性取得の結果を呼び出している"
            self._add_reference(
                ReferenceKind.CALL,
                node,
                symbol,
                _unparse(func)[:80],
                None,
                ResolutionStatus.UNRESOLVED,
                note,
            )
            return

        if parts == ["super"]:
            return  # super() はメソッド呼び出しの前置であり、単体では意味を持たない
        self._maybe_dynamic_import(node, parts)
        target_name = ".".join(parts)
        key, status, note = self._resolve_parts(parts)
        self._add_reference(ReferenceKind.CALL, node, symbol, target_name, key, status, note)

    def _inheritance(self, base: ast.expr, class_symbol: Symbol) -> None:
        expr = base.value if isinstance(base, ast.Subscript) else base
        parts = self._dotted_parts(expr)
        if parts is None:
            self._add_reference(
                ReferenceKind.INHERITANCE,
                base,
                class_symbol,
                _unparse(base)[:80],
                None,
                ResolutionStatus.UNRESOLVED,
                "基底クラスが式であり、静的に確定できない",
            )
            return
        key, status, note = self._resolve_parts(parts, allow_self=False)
        self._add_reference(
            ReferenceKind.INHERITANCE, base, class_symbol, ".".join(parts), key, status, note
        )

    def _dotted_parts(self, expr: ast.expr) -> list[str] | None:
        """Name/Attribute連鎖を名前の列にする。super().x は先頭を 'super()' とする。"""

        parts: list[str] = []
        current: ast.expr = expr
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        elif (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Name)
            and current.func.id == "super"
            and parts
        ):
            parts.append("super()")
        else:
            return None
        parts.reverse()
        return parts

    def _resolve_parts(
        self, parts: list[str], allow_self: bool = True
    ) -> tuple[str | None, ResolutionStatus, str]:
        head, rest = parts[0], parts[1:]
        suffix = "".join(f".{p}" for p in rest)
        enclosing_class = self._enclosing_class()

        if head == "super()":
            if allow_self and enclosing_class is not None and len(rest) == 1:
                return f"{KEY_SUPER}{enclosing_class.qualified_name}:{rest[0]}", (
                    ResolutionStatus.UNRESOLVED
                ), ""
            return None, ResolutionStatus.UNRESOLVED, "super() 経由の呼び出し先を静的に確定できない"
        if allow_self and head == self._self_names[-1] and head is not None:
            if enclosing_class is not None and len(rest) == 1:
                return f"{KEY_SELF}{enclosing_class.qualified_name}:{rest[0]}", (
                    ResolutionStatus.UNRESOLVED
                ), ""
            if enclosing_class is not None and len(rest) == 2:
                attribute_type = self._class_attr_types.get(enclosing_class.qualified_name, {}).get(
                    rest[0]
                )
                typed = self._typed(attribute_type, [rest[1]])
                if typed is not None:
                    return typed
            return None, ResolutionStatus.UNRESOLVED, (
                "self/cls 経由の属性であり、インスタンス属性の型を静的に確定できない"
            )
        if head in self._locals[-1]:
            typed = self._typed(self._types[-1].get(head), rest)
            if typed is not None:
                return typed
            return None, ResolutionStatus.UNRESOLVED, (
                "ローカル変数・引数経由であり、実体を静的に確定できない"
            )
        if head in self._module_names:
            typed = self._typed(self._module_types.get(head), rest)
            if typed is not None:
                return typed
            return f"{KEY_DIRECT}{self._module_name}.{head}{suffix}", (
                ResolutionStatus.UNRESOLVED
            ), ""
        if head in self._bindings:
            return f"{KEY_IMPORT}{self._bindings[head]}{suffix}", ResolutionStatus.UNRESOLVED, ""
        if head in _BUILTIN_NAMES and not rest:
            return None, ResolutionStatus.EXTERNAL, "組み込み関数・型"
        if self._star_modules:
            # `from M import *` で取り込まれた名前かもしれない。M を解析した後に解決する。
            names = ";".join(self._star_modules)
            return f"{KEY_STAR}{names}|{'.'.join(parts)}", ResolutionStatus.UNRESOLVED, ""
        return None, ResolutionStatus.UNRESOLVED, "名前を静的に解決できない"

    @staticmethod
    def _typed(
        type_key: str | None, rest: list[str]
    ) -> tuple[str | None, ResolutionStatus, str] | None:
        """型が分かっている変数のメソッド呼び出しの照合キー。型が不明ならNone。"""

        if type_key is None or len(rest) != 1:
            return None
        if type_key.startswith("builtin:"):
            return None, ResolutionStatus.EXTERNAL, f"組み込み型({type_key[8:]})のメソッド"
        return f"{KEY_TYPED}{type_key}|{rest[0]}", ResolutionStatus.UNRESOLVED, ""

    # --- 動的インポート ---

    def _maybe_dynamic_import(self, node: ast.Call, parts: list[str]) -> None:
        head = parts[0]
        qualified = _join(self._bindings.get(head, head), *parts[1:])
        if qualified == "__import__":
            qualified = "builtins.__import__"
        if qualified not in _DYNAMIC_IMPORT_FUNCTIONS:
            return
        line = node.lineno
        literal = (
            node.args[0].value
            if node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            else None
        )
        self._result.warnings.append(
            f"line {line}: 動的インポート({'.'.join(parts)})があります。"
            "依存関係は静的に確定できません。"
        )
        if literal is not None:
            dependency = Dependency(
                dependency_id=self._ids.dependency_id("import", literal, line),
                source_file_id=self._file_id,
                target_name=literal,
                target_key=literal,
                dependency_kind=DependencyKind.IMPORT,
                evidence_location=SourceLocation(self._file_id, line, node.end_lineno or line),
                confidence=Confidence.INFERRED,
                note="動的インポート（引数が文字列リテラル）",
            )
        else:
            dependency = Dependency(
                dependency_id=self._ids.dependency_id("import", "<dynamic>", line),
                source_file_id=self._file_id,
                target_name="<dynamic>",
                target_key=None,
                dependency_kind=DependencyKind.IMPORT,
                evidence_location=SourceLocation(self._file_id, line, node.end_lineno or line),
                note="動的インポート（引数が実行時に決まる）",
            )
        self._result.dependencies.append(dependency)


def _assignment_names(target: ast.expr):
    # tuple/list代入 (a, b = 1, 2) に対応する。属性代入(self.x = ...)は
    # インスタンス変数でありモジュール/クラス変数抽出の対象外。
    if isinstance(target, ast.Name):
        yield target.id, target.lineno, target.end_lineno or target.lineno
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _assignment_names(elt)


def _local_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """関数スコープで束縛される名前（引数・代入・ネスト定義など）を集める。"""

    args = node.args
    names = {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]}
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            names.add(extra.arg)
    declared_global: set[str] = set()

    stack: list[ast.AST] = list(node.body)
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(current.name)
            continue  # ネストしたスコープの内部は別スコープ
        if isinstance(current, ast.Lambda):
            continue
        if isinstance(current, ast.Name) and isinstance(current.ctx, (ast.Store, ast.Del)):
            names.add(current.id)
        elif isinstance(current, (ast.Import, ast.ImportFrom)):
            for alias in current.names:
                names.add((alias.asname or alias.name).split(".", 1)[0])
        elif isinstance(current, ast.ExceptHandler) and current.name:
            names.add(current.name)
        elif isinstance(current, (ast.Global, ast.Nonlocal)):
            declared_global.update(current.names)
        stack.extend(ast.iter_child_nodes(current))
    return names - declared_global


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<解析不能>"


def _module_level_imports(tree: ast.Module) -> set[int]:
    """関数・クラスの内側ではない（モジュールの公開名になりうる）import文のid集合。"""

    found: set[int] = set()
    stack: list[ast.AST] = list(tree.body)
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(current, (ast.Import, ast.ImportFrom)):
            found.add(id(current))
            continue
        stack.extend(ast.iter_child_nodes(current))
    return found

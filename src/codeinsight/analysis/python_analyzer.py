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
_DYNAMIC_IMPORT_FUNCTIONS = frozenset({"importlib.import_module", "builtins.__import__"})

# 参照の照合キーの接頭辞（ReferenceResolverが解釈する）。
KEY_DIRECT = "py:"  # 同一モジュール内で定義された名前（修飾名）
KEY_IMPORT = "pyimport:"  # import経由の名前（import元の修飾名）
KEY_SELF = "pyself:"  # self/cls経由のメソッド呼び出し  pyself:<クラス修飾名>:<属性>
KEY_SUPER = "pysuper:"  # super()経由のメソッド呼び出し  pysuper:<クラス修飾名>:<属性>
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
        bindings = self._collect_imports(tree, unit.file_id, module_name, is_init, ids, result)
        _ReferenceCollector(
            file_id=unit.file_id,
            module_name=module_name,
            ids=ids,
            node_symbols=node_symbols,
            bindings=bindings,
            module_names=module_names,
            module_symbol=module_symbol,
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
    ) -> dict[str, str]:
        """import文から依存関係を作り、名前->修飾名の対応表を返す。"""

        bindings: dict[str, str] = {}

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
                    else:
                        top = alias.name.split(".", 1)[0]
                        bindings[top] = top
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
                        result.warnings.append(
                            f"line {node.lineno}: 'from {base} import *' は取り込まれる名前を"
                            "静的に確定しないため、参照解決の対象外です。"
                        )
                        continue
                    bindings[alias.asname or alias.name] = _join(base, alias.name)
        return bindings

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
        self._types: list[dict[str, str]] = [{}]

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
        """型注釈付きの引数と、1回だけ `x = クラス(...)` で代入される変数の型を推定する。

        推定結果は照合キー（クラスの名前解決キー）で返す。再代入される名前や、
        Optional/Union等の複合的な注釈は、静的に型を決められないため対象外とする。
        """

        stores: Counter[str] = Counter()
        constructed: dict[str, ast.Call] = {}
        stack: list[ast.AST] = list(node.body)
        while stack:
            current = stack.pop()
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                if not isinstance(current, ast.Lambda):
                    stores[current.name] += 1
                continue
            if isinstance(current, ast.Name) and isinstance(current.ctx, (ast.Store, ast.Del)):
                stores[current.id] += 1
            elif (
                isinstance(current, ast.Assign)
                and len(current.targets) == 1
                and isinstance(current.targets[0], ast.Name)
                and isinstance(current.value, ast.Call)
            ):
                constructed[current.targets[0].id] = current.value
            stack.extend(ast.iter_child_nodes(current))

        types: dict[str, str] = {}
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if arg.annotation is None or stores[arg.arg] > 0:
                continue
            key = self._type_key(self._annotation_parts(arg.annotation))
            if key:
                types[arg.arg] = key
        for name, call in constructed.items():
            if stores[name] != 1:
                continue
            key = self._type_key(self._dotted_parts(call.func))
            if key:
                types[name] = key
        return types

    def _annotation_parts(self, annotation: ast.expr) -> list[str] | None:
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return None
        return self._dotted_parts(annotation)

    def _type_key(self, parts: list[str] | None) -> str | None:
        if parts is None or parts[0] == "super()":
            return None
        key, _, _ = self._resolve_parts(parts, allow_self=False)
        if key and (key.startswith(KEY_DIRECT) or key.startswith(KEY_IMPORT)):
            return key
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
            return None, ResolutionStatus.UNRESOLVED, (
                "self/cls 経由の属性であり、インスタンス属性の型を静的に確定できない"
            )
        if head in self._locals[-1]:
            type_key = self._types[-1].get(head)
            if type_key is not None and len(rest) == 1:
                return f"{KEY_TYPED}{type_key}|{rest[0]}", ResolutionStatus.UNRESOLVED, ""
            return None, ResolutionStatus.UNRESOLVED, (
                "ローカル変数・引数経由であり、実体を静的に確定できない"
            )
        if head in self._module_names:
            return f"{KEY_DIRECT}{self._module_name}.{head}{suffix}", (
                ResolutionStatus.UNRESOLVED
            ), ""
        if head in self._bindings:
            return f"{KEY_IMPORT}{self._bindings[head]}{suffix}", ResolutionStatus.UNRESOLVED, ""
        if head in _BUILTIN_NAMES and not rest:
            return None, ResolutionStatus.EXTERNAL, "組み込み関数・型"
        return None, ResolutionStatus.UNRESOLVED, "名前を静的に解決できない"

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

from __future__ import annotations

import ast
import tomllib
from dataclasses import dataclass

from codeinsight.analysis import flow_analysis as fa
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.source_scan import ScannedFile, ScanResult, iter_python_files
from codeinsight.domain import Language, Project, Symbol, SymbolKind

KIND_LABELS = {
    "entry": "エントリポイント",
    "cli": "CLIコマンド",
    "http": "HTTPエンドポイント",
    "event": "イベント・コールバックの登録",
    "thread": "スレッド・プロセス・実行器",
    "async": "非同期（asyncio）",
    "cache": "キャッシュ",
}
_HTTP_METHODS = frozenset({"route", "get", "post", "put", "delete", "patch", "websocket", "head", "options"})
_EVENT_METHODS = frozenset(
    {"connect", "register", "subscribe", "add_listener", "add_handler", "add_done_callback", "on", "bind",
     "add_signal_handler", "signal", "setup", "listen"}
)
_NOT_EVENT_RECEIVERS = ("sqlite3", "psycopg", "pymysql", "mysql", "socket", "redis", "pymongo", "ldap")
_EXECUTOR_METHODS = frozenset({"submit", "map", "run_in_executor", "apply_async", "starmap", "map_async"})
_CACHE_DECORATORS = frozenset({"lru_cache", "cache", "cached_property", "cached", "memoize", "memoized"})
_THREAD_BASES = ("QThread", "QRunnable", "Thread")


@dataclass
class BoundaryItem:
    kind: str
    label: str
    path: str
    line: int
    owner: str
    target: Symbol | None = None  # 登録・起動される関数（特定できた場合）
    detail: str = ""
    confidence: str = "confirmed"  # confirmed（構文から確定）/ inferred（名前による推定）


class BoundaryService:
    """プログラムの入口と境界（CLI・HTTP・イベント・スレッド・非同期・キャッシュ）を構文パターンから洗い出す（Python）。

    フレームワークの規約（デコレータ名・メソッド名）に基づくため、独自の仕組みは検出できない。
    登録されるコールバック等は、名前を静的に解決できたものだけ target に結び付ける。
    """

    def scan(
        self,
        project: Project,
        index: ProjectIndex,
        *,
        contains: str | None = None,
        light: bool = False,
    ) -> tuple[list[BoundaryItem], list[str]]:
        """入口と境界を洗い出す。

        light=True はソースを構文解析せず、解析結果（シンボル）とpyprojectだけから分かるものに限る。
        contains を渡すと、その文字列を含むファイルだけを構文解析する（特定のシンボルを登録している箇所の探索用）。
        """

        items: list[BoundaryItem] = []
        result = ScanResult()
        symbols_by_qualified = {s.qualified_name: s for s in index.symbols.values()}
        symbols_by_name: dict[str, list[Symbol]] = {}
        for symbol in index.symbols.values():
            if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS):
                symbols_by_name.setdefault(symbol.name, []).append(symbol)

        items.extend(self._pyproject_entries(project, index, symbols_by_qualified))
        for symbol in index.symbols.values():
            path = index.path_of(symbol.file_id)
            language = index.files[symbol.file_id].language
            if language == Language.C and symbol.kind == SymbolKind.FUNCTION and symbol.name == "main":
                items.append(BoundaryItem("entry", "C の main 関数", path, symbol.start_line, symbol.qualified_name, symbol))
            if symbol.kind == SymbolKind.MODULE and path.endswith("__main__.py"):
                items.append(BoundaryItem("entry", "python -m で実行されるモジュール", path, 1, symbol.qualified_name, symbol))
            if symbol.is_async:
                items.append(BoundaryItem("async", "async 関数", path, symbol.start_line, symbol.qualified_name, symbol))
            if symbol.kind == SymbolKind.CLASS and any(any(t in b for t in _THREAD_BASES) for b in symbol.base_classes):
                items.append(BoundaryItem("thread", f"スレッド系クラス（{', '.join(symbol.base_classes)}）", path, symbol.start_line, symbol.qualified_name, symbol))
            if symbol.kind == SymbolKind.METHOD and symbol.name.startswith("do_") and symbol.name[3:].isupper():
                parent = symbols_by_qualified.get(symbol.qualified_name.rsplit(".", 1)[0])
                if parent is not None and any("RequestHandler" in b for b in parent.base_classes):
                    items.append(BoundaryItem("http", f"{symbol.name[3:]} ハンドラ（http.server）", path, symbol.start_line, symbol.qualified_name, symbol))
            if any(d.split("(", 1)[0].rsplit(".", 1)[-1] in _CACHE_DECORATORS for d in symbol.decorators):
                names = ", ".join(d for d in symbol.decorators if d.split("(", 1)[0].rsplit(".", 1)[-1] in _CACHE_DECORATORS)
                items.append(BoundaryItem("cache", f"キャッシュのデコレータ（{names}）", path, symbol.start_line, symbol.qualified_name, symbol))
            elif symbol.kind in (SymbolKind.CLASS, SymbolKind.FUNCTION, SymbolKind.METHOD) and "cache" in symbol.name.lower():
                items.append(BoundaryItem("cache", "名前に cache を含む定義", path, symbol.start_line, symbol.qualified_name, symbol, confidence="inferred"))

        if not light:
            for scanned in iter_python_files(project, index, result, contains=contains):
                items.extend(self._scan_file(scanned, symbols_by_qualified, symbols_by_name))
        items.sort(key=lambda i: (list(KIND_LABELS).index(i.kind), i.path, i.line))
        return items, result.skipped

    # --- pyproject の scripts ---

    def _pyproject_entries(self, project: Project, index: ProjectIndex, symbols_by_qualified) -> list[BoundaryItem]:
        path = project.root_path / "pyproject.toml"
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return []
        found = []
        for section in ("scripts", "gui-scripts"):
            for command, target in (data.get("project", {}).get(section, {}) or {}).items():
                module, _, function = str(target).partition(":")
                qualified = f"{module}.{function}"
                symbol = symbols_by_qualified.get(qualified) or next(
                    (s for q, s in symbols_by_qualified.items() if q.endswith("." + qualified)), None
                )
                found.append(
                    BoundaryItem(
                        "entry", f"コマンド `{command}`（pyproject.toml の {section}）", "pyproject.toml", 1,
                        qualified, symbol, detail=str(target),
                    )
                )
        return found

    # --- ソースの走査 ---

    def _scan_file(self, scanned: ScannedFile, by_qualified, by_name) -> list[BoundaryItem]:
        path = scanned.source_file.relative_path
        module = scanned.module_name
        found: list[BoundaryItem] = []

        def resolve(expression: ast.AST | None, line: int) -> Symbol | None:
            if expression is None:
                return None
            owner = scanned.owner.at(line)
            if isinstance(expression, ast.Name):
                direct = by_qualified.get(f"{module}.{expression.id}")
                if direct is not None:
                    return direct
                candidates = by_name.get(expression.id, [])
                return candidates[0] if len(candidates) == 1 else None
            if isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name) and expression.value.id in ("self", "cls"):
                if owner is not None and "." in owner.qualified_name:
                    class_name = owner.qualified_name.rsplit(".", 1)[0] if owner.kind == SymbolKind.METHOD else owner.qualified_name
                    return by_qualified.get(f"{class_name}.{expression.attr}")
            return None

        def add(kind, label, line, target=None, detail="", confidence="confirmed") -> None:
            found.append(BoundaryItem(kind, label, path, line, scanned.owner.name_at(line), target, detail, confidence))

        parsers: dict[str, str] = {}
        for node in ast.walk(scanned.tree):
            # if __name__ == "__main__":
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
                left = fa.unparse(node.test.left, 20)
                if left == "__name__" and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in node.test.comparators):
                    calls = [fa.unparse(c.func, 30) for c in ast.walk(node) if isinstance(c, ast.Call)]
                    add("entry", "スクリプトとして直接実行されるとき（__main__ ブロック）", node.lineno, detail="呼び出し: " + ", ".join(dict.fromkeys(calls))[:80])
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Attribute) and call.func.attr == "add_parser" and call.args and isinstance(call.args[0], ast.Constant):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            parsers[target.id] = str(call.args[0].value)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    self._decorator(node, decorator, scanned, add)
            if isinstance(node, ast.Call):
                self._call(node, parsers, resolve, add)
        return found

    def _decorator(self, function, decorator, scanned: ScannedFile, add) -> None:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call else decorator
        if not isinstance(target, ast.Attribute):
            return
        attribute = target.attr
        symbol = next((s for s in [scanned.owner.at(function.lineno)] if s is not None and s.start_line == function.lineno), None)
        if attribute in _HTTP_METHODS and call is not None and call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
            route = call.args[0].value
            methods = next((fa.unparse(k.value, 30) for k in call.keywords if k.arg == "methods"), "")
            label = f"{attribute.upper() if attribute != 'route' else 'ROUTE'} {route}" + (f" methods={methods}" if methods else "")
            add("http", label, function.lineno, symbol, detail=f"@{fa.unparse(decorator, 60)}")
        elif attribute in ("command", "group") and fa.unparse(target.value, 20) != "":
            name = ""
            if call is not None and call.args and isinstance(call.args[0], ast.Constant):
                name = str(call.args[0].value)
            add("cli", f"コマンド {name or function.name}（@{fa.unparse(target, 30)}）", function.lineno, symbol)
        elif attribute in ("on_event", "listener", "on", "task", "scheduled") and call is not None:
            add("event", f"イベントハンドラ @{fa.unparse(target, 30)}({fa.unparse(call.args[0], 30) if call.args else ''})", function.lineno, symbol, confidence="inferred")

    def _call(self, node: ast.Call, parsers: dict[str, str], resolve, add) -> None:
        name = fa.unparse(node.func, 60)
        last = name.rsplit(".", 1)[-1]
        # argparse のサブコマンドと実行関数の対応: <parser>.set_defaults(func=<関数>)
        if last == "set_defaults" and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            for keyword in node.keywords:
                if keyword.arg == "func":
                    sub = parsers.get(node.func.value.id)
                    label = f"サブコマンド `{sub}`" if sub else "既定のコマンド"
                    add("cli", label, node.lineno, resolve(keyword.value, node.lineno), detail="set_defaults(func=...)")
        # スレッド・プロセス
        if last in ("Thread", "Process", "Timer") and any(k.arg == "target" for k in node.keywords):
            target = next(k.value for k in node.keywords if k.arg == "target")
            add("thread", f"{name}(target=...) でスレッド/プロセスを起動", node.lineno, resolve(target, node.lineno), detail=fa.unparse(target, 40))
        elif last in _EXECUTOR_METHODS and node.args:
            callable_arg = node.args[1] if last == "run_in_executor" and len(node.args) > 1 else node.args[0]
            target = resolve(callable_arg, node.lineno)
            if target is not None:
                add("thread", f"{name}(...) で実行器に関数を渡す", node.lineno, target, detail=fa.unparse(callable_arg, 40))
        elif last in ("create_task", "ensure_future", "run", "gather") and name.startswith(("asyncio.", "loop.", "tg.")) and node.args:
            first = node.args[0]
            coroutine = first.func if isinstance(first, ast.Call) else first
            add("async", f"{name}(...) でコルーチンを実行/並行化", node.lineno, resolve(coroutine, node.lineno), detail=fa.unparse(first, 40))
        # イベント・コールバックの登録
        elif last in _EVENT_METHODS and isinstance(node.func, ast.Attribute) and node.args:
            if fa.unparse(node.func.value, 30).startswith(_NOT_EVENT_RECEIVERS):
                return
            for argument in node.args:
                target = resolve(argument, node.lineno)
                if target is not None or isinstance(argument, ast.Lambda):
                    detail = "ラムダ式" if isinstance(argument, ast.Lambda) else fa.unparse(argument, 40)
                    add("event", f"{name}(...) でコールバックを登録", node.lineno, target, detail=detail)
                    break
        elif name == "atexit.register" and node.args:
            add("event", "終了時に実行する関数を登録（atexit）", node.lineno, resolve(node.args[0], node.lineno), detail=fa.unparse(node.args[0], 40))

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from codeinsight.analysis import flow_analysis as fa
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.source_scan import ScanResult, ScannedFile, iter_python_files
from codeinsight.domain import Project, ReferenceKind, ResolutionStatus, Symbol, SymbolKind

_ENV_READ = frozenset({"os.getenv", "getenv", "os.environ.get", "environ.get"})
_ENV_WRITE = frozenset({"os.environ.setdefault", "environ.setdefault", "os.putenv"})
_CONFIG_LOADERS = {
    "tomllib.load": "TOML", "tomllib.loads": "TOML", "tomli.load": "TOML", "yaml.safe_load": "YAML",
    "yaml.load": "YAML", "configparser.ConfigParser": "INI", "dotenv.load_dotenv": ".env", "load_dotenv": ".env",
    "json.load": "JSON（設定かデータかは区別できない）",
}
_TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*\.py$|_test\.py$")
_CONSTANT = re.compile(r"^[A-Z][A-Z0-9_]*$")


@dataclass(frozen=True)
class ConfigUse:
    path: str
    line: int
    owner: str
    how: str  # 値を読む・変数へ代入・引数として参照 など


@dataclass
class ConfigItem:
    kind: str  # env / env_write / cli_option / cli_subcommand / config_file / constant
    name: str
    path: str
    line: int
    owner: str
    default: str = ""
    help: str = ""
    detail: str = ""
    uses: list[ConfigUse] = field(default_factory=list)
    confidence: str = "confirmed"


KIND_LABELS = {
    "env": "環境変数（読み取り）",
    "env_write": "環境変数（設定）",
    "cli_option": "コマンドライン引数",
    "cli_subcommand": "サブコマンド",
    "config_file": "設定ファイルの読み込み",
    "constant": "定数（設定値）",
}


class ConfigService:
    """設定値（環境変数・コマンドライン引数・設定ファイル・定数）の定義箇所と使われる箇所を集める（Python）。

    環境変数名・オプション名は、文字列リテラルで書かれているものだけが対象（動的に組み立てられた名前は
    <動的> と表示する）。使用箇所は、同じ関数内での変数の使用、モジュール定数への参照、argparseの
    名前空間（`args.<dest>`）の属性読み取りから求めた、静的な近似。
    """

    def scan(self, project: Project, index: ProjectIndex) -> tuple[list[ConfigItem], list[str]]:
        items: list[ConfigItem] = []
        result = ScanResult()
        refs_by_target: dict[str, list] = {}
        for reference in index.references:
            if reference.target_symbol_id and reference.resolution_status == ResolutionStatus.RESOLVED:
                refs_by_target.setdefault(reference.target_symbol_id, []).append(reference)
        symbols_by_qualified = {s.qualified_name: s for s in index.symbols.values()}

        for scanned in iter_python_files(project, index, result):
            path = scanned.source_file.relative_path
            items.extend(self._env(scanned, path))
            items.extend(self._cli(scanned, path))
            items.extend(self._loaders(scanned, path))
            if not _TEST_PATH.search(path):
                items.extend(self._constants(scanned, path, index, symbols_by_qualified, refs_by_target))
        items.sort(key=lambda i: (list(KIND_LABELS).index(i.kind), i.name, i.path, i.line))
        return items, result.skipped

    # --- 環境変数 ---

    def _env(self, scanned: ScannedFile, path: str) -> list[ConfigItem]:
        found: list[ConfigItem] = []
        assigned_to = _assignment_targets(scanned.tree)
        for node in ast.walk(scanned.tree):
            call_name = fa.unparse(node.func, 40) if isinstance(node, ast.Call) else ""
            if isinstance(node, ast.Call) and call_name in _ENV_READ | _ENV_WRITE:
                name = _literal(node.args[0]) if node.args else None
                default = fa.unparse(node.args[1], 40) if len(node.args) > 1 else ""
                kind = "env_write" if call_name in _ENV_WRITE else "env"
                item = ConfigItem(kind, name or "<動的>", path, node.lineno, scanned.owner.name_at(node.lineno), default=default)
                item.uses = self._variable_uses(scanned, path, node, assigned_to)
                found.append(item)
            elif (
                isinstance(node, ast.Subscript)
                and fa.unparse(node.value, 30) in ("os.environ", "environ")
                and isinstance(node.ctx, (ast.Load, ast.Store))
            ):
                kind = "env" if isinstance(node.ctx, ast.Load) else "env_write"
                name = _literal(node.slice)
                item = ConfigItem(kind, name or "<動的>", path, node.lineno, scanned.owner.name_at(node.lineno), detail="必須（未設定ならKeyError）" if kind == "env" else "")
                if kind == "env":
                    item.uses = self._variable_uses(scanned, path, node, assigned_to)
                found.append(item)
        return found

    def _variable_uses(self, scanned: ScannedFile, path: str, expression: ast.AST, assigned_to) -> list[ConfigUse]:
        """環境変数の値の使用箇所。引数定義の既定値として使われる場合はそのオプション、
        変数に代入される場合はその変数の、同じ関数内での使用箇所。"""

        for call in ast.walk(scanned.tree):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "add_argument":
                for keyword in call.keywords:
                    if keyword.arg == "default" and any(n is expression for n in ast.walk(keyword.value)):
                        option = next((a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)), "")
                        return [ConfigUse(path, call.lineno, scanned.owner.name_at(call.lineno), f"オプション {option} の既定値として使用")]

        for statement, targets in assigned_to:
            if not any(n is expression for n in ast.walk(statement.value)):
                continue
            scope = scanned.owner.at(statement.lineno)
            uses: list[ConfigUse] = []
            for target in targets:
                if scope is not None and scope.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
                    function = fa.find_definition(scanned.tree, scope.start_line)
                    if function is not None:
                        for line in sorted({n.lineno for n in ast.walk(function) if isinstance(n, ast.Name) and n.id == target and isinstance(n.ctx, ast.Load)}):
                            uses.append(ConfigUse(path, line, scope.qualified_name, f"変数 {target} として使用"))
            return uses
        return []

    # --- コマンドライン引数 ---

    def _cli(self, scanned: ScannedFile, path: str) -> list[ConfigItem]:
        found: list[ConfigItem] = []
        namespaces = _namespace_names(scanned.tree)
        for node in ast.walk(scanned.tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr
            if method == "add_argument" and node.args:
                names = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if not names:
                    continue
                keyword = {k.arg: k.value for k in node.keywords if k.arg}
                dest = _literal(keyword["dest"]) if "dest" in keyword else _dest(names)
                item = ConfigItem(
                    "cli_option", ", ".join(names), path, node.lineno, scanned.owner.name_at(node.lineno),
                    default=fa.unparse(keyword.get("default"), 40),
                    help=(_literal(keyword["help"]) or "")[:70] if "help" in keyword else "",
                    detail=_cli_detail(keyword),
                )
                item.uses = self._namespace_uses(scanned, path, dest, namespaces)
                found.append(item)
            elif method == "add_parser" and node.args and _literal(node.args[0]):
                found.append(
                    ConfigItem("cli_subcommand", _literal(node.args[0]) or "", path, node.lineno, scanned.owner.name_at(node.lineno),
                               help=_keyword_literal(node, "help")[:70])
                )
        # click / typer のデコレータ
        for function in [n for n in ast.walk(scanned.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for decorator in function.decorator_list:
                if isinstance(decorator, ast.Call) and fa.unparse(decorator.func, 30).rsplit(".", 1)[-1] in ("option", "argument"):
                    names = [a.value for a in decorator.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                    if not names:
                        continue
                    dest = _dest(names)
                    item = ConfigItem(
                        "cli_option", ", ".join(names), path, decorator.lineno, scanned.owner.name_at(function.lineno),
                        default=_keyword_literal(decorator, "default", raw=True),
                        help=_keyword_literal(decorator, "help")[:70],
                    )
                    variables = fa.analyze_variables(function)
                    if dest in variables:
                        item.uses = [ConfigUse(path, line, scanned.owner.name_at(function.lineno), f"引数 {dest} として使用") for line in sorted(set(variables[dest].uses))]
                    found.append(item)
        return found

    def _namespace_uses(self, scanned: ScannedFile, path: str, dest: str | None, namespaces: set[str]) -> list[ConfigUse]:
        if not dest:
            return []
        uses = []
        for node in ast.walk(scanned.tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == dest
                and isinstance(node.ctx, ast.Load)
                and isinstance(node.value, ast.Name)
                and node.value.id in namespaces
            ):
                uses.append(ConfigUse(path, node.lineno, scanned.owner.name_at(node.lineno), f"{node.value.id}.{dest} を読む"))
        return sorted(set(uses), key=lambda u: u.line)

    # --- 設定ファイル ---

    def _loaders(self, scanned: ScannedFile, path: str) -> list[ConfigItem]:
        found = []
        for node in ast.walk(scanned.tree):
            if isinstance(node, ast.Call):
                name = fa.unparse(node.func, 40)
                if name in _CONFIG_LOADERS:
                    found.append(
                        ConfigItem("config_file", name, path, node.lineno, scanned.owner.name_at(node.lineno),
                                   detail=_CONFIG_LOADERS[name], confidence="inferred")
                    )
        return found

    # --- 定数 ---

    def _constants(self, scanned: ScannedFile, path: str, index: ProjectIndex, symbols, refs_by_target) -> list[ConfigItem]:
        found = []
        for statement in scanned.tree.body:
            targets = statement.targets if isinstance(statement, ast.Assign) else ([statement.target] if isinstance(statement, ast.AnnAssign) else [])
            value = getattr(statement, "value", None)
            if value is None or not _is_literal(value):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and _CONSTANT.match(target.id):
                    symbol = symbols.get(f"{scanned.module_name}.{target.id}")
                    item = ConfigItem("constant", target.id, path, statement.lineno, scanned.module_name, default=fa.unparse(value, 50))
                    if symbol is not None:
                        for reference in refs_by_target.get(symbol.symbol_id, []):
                            if reference.reference_kind in (ReferenceKind.NAME_REF, ReferenceKind.CALL):
                                owner = index.symbols.get(reference.source_symbol_id)
                                item.uses.append(
                                    ConfigUse(index.path_of(reference.source_location.file_id), reference.source_location.start_line,
                                              owner.qualified_name if owner else "", "参照")
                                )
                        item.uses.sort(key=lambda u: (u.path, u.line))
                    found.append(item)
        return found


def _literal(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _keyword_literal(call: ast.Call, name: str, raw: bool = False) -> str:
    for keyword in call.keywords:
        if keyword.arg == name:
            return fa.unparse(keyword.value, 40) if raw else (_literal(keyword.value) or "")
    return ""


def _dest(names: list[str]) -> str | None:
    long_names = [n for n in names if n.startswith("--")]
    chosen = long_names[0] if long_names else names[0]
    return chosen.lstrip("-").replace("-", "_") or None


def _cli_detail(keyword: dict[str, ast.AST]) -> str:
    parts = []
    if "action" in keyword:
        parts.append(f"action={fa.unparse(keyword['action'], 20)}")
    if "type" in keyword:
        parts.append(f"type={fa.unparse(keyword['type'], 20)}")
    if "choices" in keyword:
        parts.append(f"choices={fa.unparse(keyword['choices'], 40)}")
    if "required" in keyword:
        parts.append(f"required={fa.unparse(keyword['required'], 10)}")
    return ", ".join(parts)


def _is_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _is_literal(k) and _is_literal(v) for k, v in zip(node.keys, node.values))
    if isinstance(node, ast.UnaryOp):
        return _is_literal(node.operand)
    return False


def _assignment_targets(tree: ast.Module) -> list[tuple[ast.Assign, list[str]]]:
    pairs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if names:
                pairs.append((node, names))
    return pairs


def _namespace_names(tree: ast.Module) -> set[str]:
    """`<変数> = <...>.parse_args(...)` で得た名前空間の変数名と、慣用の引数名 args。"""

    names = {"args"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and fa.unparse(node.value.func, 40).endswith(("parse_args", "parse_known_args")):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names

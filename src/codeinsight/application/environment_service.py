from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field

from codeinsight.analysis import flow_analysis as fa
from codeinsight.application.external_service import CATEGORY_LABELS, categorize
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.source_scan import ScanResult, iter_python_files
from codeinsight.domain import DependencyKind, Language, Project, ResolutionStatus

_PLATFORM_EXPRESSIONS = ("sys.platform", "platform.system", "os.name", "platform.machine", "sys.version_info")
_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")


@dataclass(frozen=True)
class Site:
    path: str
    line: int
    detail: str


@dataclass
class EnvironmentReport:
    """プログラムが前提とする実行環境（言語のバージョン・依存ライブラリ・OS・外部コマンド）。"""

    python_requirement: str = ""
    declared: dict[str, str] = field(default_factory=dict)  # 宣言された依存 -> 出所（pyproject / requirements）
    used_external: Counter = field(default_factory=Counter)  # importされる外部のトップレベル名 -> ファイル数
    undeclared: list[str] = field(default_factory=list)  # 使われているが宣言に見つからない
    unused_declared: list[str] = field(default_factory=list)  # 宣言されているがimportが見つからない
    frameworks: dict[str, list[str]] = field(default_factory=dict)  # カテゴリ -> ライブラリ
    platform_checks: list[Site] = field(default_factory=list)
    executables: list[Site] = field(default_factory=list)
    c_system_headers: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


class EnvironmentService:
    """pyproject/requirements・import・OS判定・外部コマンドの呼び出しから、実行環境の前提を洗い出す。

    パッケージ名とimport名が異なるライブラリ（例: beautifulsoup4 と bs4）は名前だけでは対応付けられないため、
    「宣言に見つからない」は候補として扱う。実行して確認はしない（AGENTS.md §4.4）。
    """

    def report(self, project: Project, index: ProjectIndex) -> EnvironmentReport:
        report = EnvironmentReport()
        self._declared(project, report)
        used: Counter = Counter()
        categories: dict[str, set[str]] = {}
        headers: set[str] = set()
        for dependency in index.dependencies:
            if dependency.resolution_status != ResolutionStatus.EXTERNAL or dependency.is_candidate:
                continue
            source = index.files.get(dependency.source_file_id)
            if source is None:
                continue
            if dependency.dependency_kind == DependencyKind.INCLUDE:
                headers.add(dependency.target_name)
                continue
            top = dependency.target_name.split(".", 1)[0]
            if top in sys.stdlib_module_names:
                continue
            used[top] += 1
            category = categorize(dependency.target_name, Language.PYTHON)
            if category and category != "other":
                categories.setdefault(CATEGORY_LABELS[category], set()).add(top)
        report.used_external = used
        report.frameworks = {k: sorted(v) for k, v in categories.items()}
        report.c_system_headers = sorted(headers)
        declared_norm = {_normalize(name) for name in report.declared}
        report.undeclared = sorted(name for name in used if _normalize(name) not in declared_norm and not is_project_module(index, name))
        used_norm = {_normalize(name) for name in used}
        report.unused_declared = sorted(name for name in report.declared if _normalize(name) not in used_norm)

        result = ScanResult()
        for scanned in iter_python_files(project, index, result):
            path = scanned.source_file.relative_path
            for node in ast.walk(scanned.tree):
                if isinstance(node, ast.Compare):
                    text = fa.unparse(node.left, 50)
                    if any(text.startswith(p) for p in _PLATFORM_EXPRESSIONS):
                        comparators = ", ".join(fa.unparse(c, 30) for c in node.comparators)
                        report.platform_checks.append(Site(path, node.lineno, f"{text} と {comparators} を比較"))
                elif isinstance(node, ast.Call):
                    name = fa.unparse(node.func, 40)
                    if name.startswith(("sys.platform", "platform.system")) and name.endswith("startswith"):
                        report.platform_checks.append(Site(path, node.lineno, fa.unparse(node, 60)))
                    elif name == "shutil.which" and node.args:
                        report.executables.append(Site(path, node.lineno, f"shutil.which({fa.unparse(node.args[0], 30)})"))
                    elif name.startswith("subprocess.") and node.args:
                        executable = _executable(node.args[0])
                        if executable:
                            report.executables.append(Site(path, node.lineno, executable))
        report.skipped = result.skipped
        report.platform_checks.sort(key=lambda s: (s.path, s.line))
        report.executables.sort(key=lambda s: (s.path, s.line))
        return report

    def _declared(self, project: Project, report: EnvironmentReport) -> None:
        try:
            data = tomllib.loads((project.root_path / "pyproject.toml").read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        meta = data.get("project", {})
        report.python_requirement = str(meta.get("requires-python", ""))
        for requirement in meta.get("dependencies", []) or []:
            match = _REQ_LINE.match(str(requirement))
            if match:
                report.declared[match.group(1)] = "pyproject.toml の dependencies"
        for group, requirements in (meta.get("optional-dependencies", {}) or {}).items():
            for requirement in requirements:
                match = _REQ_LINE.match(str(requirement))
                if match:
                    report.declared.setdefault(match.group(1), f"pyproject.toml の optional-dependencies[{group}]")
        for path in sorted(project.root_path.glob("requirements*.txt")):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip() and not line.lstrip().startswith(("#", "-")):
                        match = _REQ_LINE.match(line)
                        if match:
                            report.declared.setdefault(match.group(1), path.name)
            except OSError:
                continue


def is_project_module(index: ProjectIndex, top_level: str) -> bool:
    """プロジェクト内のモジュール/パッケージの先頭名か（外部ライブラリではない）。"""

    return any(
        symbol.kind.value == "module" and top_level in symbol.qualified_name.split(".")
        for symbol in index.symbols.values()
    )


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _executable(argument: ast.expr) -> str | None:
    """subprocess の第1引数から、起動する実行ファイル名（リテラルで書かれている場合のみ）。"""

    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value.split()[0] if argument.value.split() else None
    if isinstance(argument, (ast.List, ast.Tuple)) and argument.elts:
        first = argument.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return f"<動的: {fa.unparse(first, 30)}>"
    return None

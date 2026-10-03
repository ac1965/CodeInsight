from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field

from codeinsight.application.config_service import ConfigService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Project

_DOC_SUFFIXES = (".md", ".rst", ".txt", ".org")
_CODE_SPAN = re.compile(r"`([^`\n]{1,80})`")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?$")
_OPTION = re.compile(r"^--?[A-Za-z][A-Za-z0-9\-]*$")
_ENV = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_FILE_EXTENSIONS = frozenset({"json", "md", "py", "txt", "toml", "yaml", "yml", "ini", "cfg", "jar", "epub", "pdf", "html",
                             "css", "js", "ts", "org", "rst", "xml", "csv", "lock", "sh", "c", "h", "sql", "log", "zip"})
_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "build", "dist", "__pycache__"}
_COMMON = frozenset({"True", "False", "None", "self", "cls", "int", "str", "bool", "list", "dict", "set", "tuple", "float",
                     "bytes", "print", "len", "range", "open", "type", "object", "Exception", "ValueError", "TypeError"})


@dataclass(frozen=True)
class Mention:
    kind: str  # identifier / option / env
    token: str
    path: str
    line: int


@dataclass
class SpecReport:
    """文書（md/rst/txt/org）に書かれた識別子・オプション・環境変数と、実装との照合。"""

    missing_in_code: list[Mention] = field(default_factory=list)  # 文書にあるが、実装で見つからない
    undocumented_options: list[tuple[str, str, int]] = field(default_factory=list)  # 実装にあるが、文書に無い (名前, パス, 行)
    undocumented_env: list[tuple[str, str, int]] = field(default_factory=list)
    documents: int = 0
    checked: int = 0


class SpecCheckService:
    """文書の記述と実装のずれの「手がかり」を、文字列の一致で探す（仕様の意味は理解しない）。

    バッククォートで囲まれた識別子・オプション・環境変数だけを対象にする。外部ライブラリの名前や
    概念的な用語は実装に無いのが正常なため、「見つからない」は文書が古い可能性の候補にすぎない。
    """

    def check(self, project: Project, index: ProjectIndex) -> SpecReport:
        report = SpecReport()
        symbol_names = {s.name for s in index.symbols.values()}
        qualified = {s.qualified_name for s in index.symbols.values()}
        items, _ = ConfigService().scan(project, index)
        options = {part.strip(): i for i in items if i.kind == "cli_option" for part in i.name.split(",")}
        envs = {i.name: i for i in items if i.kind in ("env", "env_write")}
        external_names = {name for name in (_external_tops(index))}

        documented: set[str] = set()
        for path in sorted(project.root_path.rglob("*")):
            if path.suffix.lower() not in _DOC_SUFFIXES or not path.is_file():
                continue
            relative = path.relative_to(project.root_path).as_posix()
            if any(part in _SKIP_DIRS or part.startswith(".") for part in path.relative_to(project.root_path).parts):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            report.documents += 1
            for number, text in enumerate(lines, 1):
                for token in _CODE_SPAN.findall(text):
                    token = token.strip()
                    documented.add(token)
                    if _OPTION.match(token):
                        report.checked += 1
                        if token not in options:
                            report.missing_in_code.append(Mention("option", token, relative, number))
                    elif _ENV.match(token):
                        report.checked += 1
                        if token not in envs:
                            report.missing_in_code.append(Mention("env", token, relative, number))
                    elif _IDENT.match(token):
                        name = token.removesuffix("()")
                        short = name.rsplit(".", 1)[-1]
                        top = name.split(".")[0]
                        if "." in name and short.lower() in _FILE_EXTENSIONS:
                            continue  # config.json のようなファイル名
                        if name in _COMMON or short in _COMMON or top in external_names or top in sys.stdlib_module_names or len(short) < 3:
                            continue
                        report.checked += 1
                        if not (name in symbol_names or name in qualified or short in symbol_names or any(q.endswith("." + name) for q in qualified)):
                            report.missing_in_code.append(Mention("identifier", token, relative, number))
        for name, item in sorted(options.items()):
            if name not in documented and name.startswith("--"):
                report.undocumented_options.append((name, item.path, item.line))
        for name, item in sorted(envs.items()):
            if name not in documented and name != "<動的>":
                report.undocumented_env.append((name, item.path, item.line))
        return report


def _external_tops(index: ProjectIndex) -> set[str]:
    return {
        d.target_name.split(".", 1)[0]
        for d in index.dependencies
        if d.resolution_status.value == "external" and not d.is_candidate
    }

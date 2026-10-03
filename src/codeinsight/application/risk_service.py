from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass

from codeinsight.analysis import flow_analysis as fa
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.source_scan import OwnerLookup, ScanResult, iter_python_files
from codeinsight.domain import Project

SEVERITIES = ("low", "medium", "high")
RULES = {
    "bare-except": ("medium", "例外の型を指定しない except（KeyboardInterrupt等も捕捉する）"),
    "swallowed-exception": ("medium", "例外を捕捉して何もしない（握りつぶし）"),
    "mutable-default": ("medium", "可変オブジェクトを引数の既定値にしている（呼び出し間で共有される）"),
    "eval-exec": ("high", "eval/exec による動的なコード実行"),
    "shell-true": ("high", "shell=True でコマンドを実行している（コマンドインジェクションの恐れ）"),
    "unsafe-deserialization": ("medium", "信頼できないデータに使うと危険なデシリアライズ"),
    "blocking-in-async": ("medium", "async関数内でブロッキング呼び出し（time.sleep等）をしている"),
    "broad-raise": ("low", "汎用の Exception を送出している（呼び出し側が区別できない）"),
    "todo-marker": ("low", "TODO/FIXME等の未対応を示すコメント"),
}
_MARKERS = ("TODO", "FIXME", "XXX", "HACK")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    path: str
    line: int
    symbol: str  # 該当箇所を含むシンボルの修飾名（無ければ空）
    detail: str

    @property
    def message(self) -> str:
        return RULES[self.rule][1]


class RiskService:
    """潜在的な問題の「手がかり」を、構文パターンから探す（Python）。

    バグであることを断定するものではない。意図的な実装の場合もあるため、各項目は
    根拠となる位置と規則名を示し、利用者が確認する前提とする。
    """

    def scan(self, project: Project, index: ProjectIndex, rules: set[str] | None = None) -> tuple[list[Finding], list[str]]:
        findings: list[Finding] = []
        result = ScanResult()
        for scanned in iter_python_files(project, index, result):
            findings.extend(self._scan_file(scanned.source_file.relative_path, scanned.text, scanned.tree, scanned.owner))
        if rules:
            findings = [f for f in findings if f.rule in rules]
        findings.sort(key=lambda f: (-SEVERITIES.index(f.severity), f.path, f.line))
        return findings, result.skipped

    def _scan_file(self, path: str, text: str, tree: ast.Module, owner: OwnerLookup) -> list[Finding]:
        found: list[Finding] = []

        def add(rule: str, line: int, detail: str = "") -> None:
            found.append(Finding(rule, RULES[rule][0], path, line, owner.name_at(line), detail))

        async_ranges = [(n.lineno, fa._end(n)) for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                types = fa._handler_types(node)
                if node.type is None:
                    add("bare-except", node.lineno)
                info = fa._handler_info(node, node, types)
                if info.swallowed:
                    add("swallowed-exception", node.lineno, f"except {', '.join(types)}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for default in [*node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]:
                    if isinstance(default, (ast.List, ast.Dict, ast.Set)) or (
                        isinstance(default, ast.Call) and fa.unparse(default.func, 20) in ("list", "dict", "set")
                    ):
                        add("mutable-default", default.lineno, fa.unparse(default, 40))
            elif isinstance(node, ast.Call):
                name = fa.unparse(node.func, 60)
                if name in ("eval", "exec"):
                    add("eval-exec", node.lineno, name)
                if name.startswith("subprocess.") and any(
                    k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords
                ):
                    add("shell-true", node.lineno, name)
                if name == "os.system":
                    add("shell-true", node.lineno, name)
                if name in ("pickle.load", "pickle.loads", "marshal.load", "marshal.loads") or (
                    name in ("yaml.load",) and not any(k.arg == "Loader" for k in node.keywords)
                ):
                    add("unsafe-deserialization", node.lineno, name)
                if name == "time.sleep" and any(a <= node.lineno <= b for a, b in async_ranges):
                    add("blocking-in-async", node.lineno, name)
            elif isinstance(node, ast.Raise) and node.exc is not None:
                target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                if fa.unparse(target, 30) == "Exception":
                    add("broad-raise", node.lineno)
        try:
            for token in tokenize.generate_tokens(io.StringIO(text).readline):
                if token.type == tokenize.COMMENT and any(m in token.string.upper() for m in _MARKERS):
                    add("todo-marker", token.start[0], token.string.strip("# ").strip()[:60])
        except (tokenize.TokenError, IndentationError):
            pass
        return found

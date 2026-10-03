from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import dataclass, field

from codeinsight.ai.context import Context
from codeinsight.ai.prompt import INFERENCE_MARK, SECTIONS
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import ExplanationStatus, Project

CITATION = re.compile(r"\[([^\[\]\s:]+):(\d+)(?:-(\d+))?\]")
_BACKTICK = re.compile(r"`([^`\n]{1,80})`")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.*)$")
_BUILTIN_WORDS = frozenset({
    "None", "True", "False", "self", "cls", "str", "int", "float", "bool", "list", "dict", "set", "tuple", "bytes",
    "print", "len", "range", "open", "type", "object", "Exception", "ValueError", "TypeError", "KeyError", "OSError",
    "isinstance", "super", "return", "raise", "yield", "async", "await", "def", "class", "if", "else", "for", "while",
    "try", "except", "finally", "with", "as", "in", "not", "and", "or", "is", "pass", "break", "continue", "lambda",
    "import", "from", "global", "json", "os", "sys", "Path", "main",
})
_FILE_EXTENSIONS = frozenset({
    "py", "pyi", "c", "h", "json", "md", "txt", "toml", "yaml", "yml", "ini", "cfg", "lock", "rst", "org", "html", "css",
    "js", "ts", "xml", "csv", "sql", "sh", "log", "zip", "jar", "epub", "pdf",
})
# 引用・根拠の付与を要求しない見出し（メタ情報）
_EXEMPT_SECTIONS = frozenset({"説明対象", "根拠", "解析上の制約", "推論を含む点"})


class CitationStatus(enum.Enum):
    VERIFIED = "verified"
    NOT_FOUND = "not_found"  # 解析対象にないファイル
    OUT_OF_RANGE = "out_of_range"  # ファイルの行数を超える・範囲が不正
    OUT_OF_CONTEXT = "out_of_context"  # 存在するが、AIに渡した根拠の範囲外（渡していない内容を根拠にしている）
    STALE = "stale"  # 解析後にファイルが変更されている


@dataclass(frozen=True)
class Citation:
    raw: str
    path: str
    start: int
    end: int
    status: CitationStatus
    line_no: int  # 回答中の行番号（1始まり）


@dataclass(frozen=True)
class LineVerdict:
    line_no: int
    text: str
    kind: str  # heading / evidenced / inference / unsupported / bad_citation / meta / other


@dataclass
class ValidationReport:
    citations: list[Citation] = field(default_factory=list)
    lines: list[LineVerdict] = field(default_factory=list)
    unknown_identifiers: list[tuple[int, str]] = field(default_factory=list)  # 実在を確認できない識別子（行, 名前）
    missing_sections: list[str] = field(default_factory=list)
    status: ExplanationStatus = ExplanationStatus.UNVERIFIED

    @property
    def bad_citations(self) -> list[Citation]:
        return [c for c in self.citations if c.status != CitationStatus.VERIFIED]

    def count(self, kind: str) -> int:
        return sum(1 for line in self.lines if line.kind == kind)

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "citations": [
                {"raw": c.raw, "status": c.status.value, "line": c.line_no} for c in self.citations
            ],
            "evidenced_lines": self.count("evidenced"),
            "inference_lines": self.count("inference"),
            "unsupported_lines": [(v.line_no, v.text[:80]) for v in self.lines if v.kind == "unsupported"],
            "bad_citation_lines": [v.line_no for v in self.lines if v.kind == "bad_citation"],
            "unknown_identifiers": [{"line": n, "name": name} for n, name in self.unknown_identifiers],
            "missing_sections": self.missing_sections,
        }


class CitationValidator:
    """AIの回答の根拠を、機械的に検証する（AGENTS.md §3.8・Phase 4の完了条件）。

    検証するもの:
    * 引用 `[path:行]` が、解析対象のファイルで、実在する行範囲で、AIに渡した根拠の範囲内か。
    * 引用先のファイルが、解析後に変更されていないか。
    * 根拠も推論の印も無い主張の行（未確認）。
    * 回答中の `識別子` が、渡した根拠または解析結果に実在するか（存在しない名前の創作の検出）。

    検証できるのは「示された根拠が存在し、渡したものの範囲内であること」までで、根拠が
    主張を実際に裏付けているかという意味までは判定しない。
    """

    def __init__(self, project: Project, index: ProjectIndex, context: Context) -> None:
        self._project = project
        self._index = index
        self._context = context
        self._line_counts: dict[str, int | None] = {}
        self._fresh: dict[str, bool] = {}
        symbol_names = {s.name for s in index.symbols.values()}
        qualified = {s.qualified_name for s in index.symbols.values()}
        self._known_names = symbol_names | qualified | {f.relative_path for f in index.files.values()}
        self._context_words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", context.text + " " + context.render()))

    def validate(self, text: str) -> ValidationReport:
        report = ValidationReport()
        section = ""
        in_code = False
        seen_sections: set[str] = set()
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_code = not in_code
                report.lines.append(LineVerdict(number, line, "other"))
                continue
            if in_code or not stripped:
                continue
            heading = _HEADING.match(line)
            if heading:
                section = heading.group(1).strip().strip("*").strip()
                for name in SECTIONS:
                    if name in section:
                        seen_sections.add(name)
                report.lines.append(LineVerdict(number, line, "heading"))
                continue

            citations = [self._citation(m, number) for m in CITATION.finditer(line)]
            report.citations.extend(citations)
            for token in _BACKTICK.findall(line):
                if self._unknown_identifier(token):
                    report.unknown_identifiers.append((number, token))

            exempt = any(name in section for name in _EXEMPT_SECTIONS)
            claim = len(CITATION.sub("", stripped).strip(" -*・>")) >= 8
            if citations and any(c.status != CitationStatus.VERIFIED for c in citations):
                kind = "bad_citation"
            elif INFERENCE_MARK in line:
                kind = "inference"
            elif citations:
                kind = "evidenced"
            elif exempt or not claim or "未確認" in line or "確認できない" in line:
                kind = "meta"
            else:
                kind = "unsupported"
            report.lines.append(LineVerdict(number, line, kind))

        report.missing_sections = [s for s in SECTIONS if s not in seen_sections]
        report.status = self._status(report)
        return report

    # --- 判定 ---

    def _status(self, report: ValidationReport) -> ExplanationStatus:
        if report.bad_citations or report.unknown_identifiers or not report.citations:
            return ExplanationStatus.UNVERIFIED
        if report.count("unsupported") > 0:
            return ExplanationStatus.PARTIAL
        return ExplanationStatus.VERIFIED

    def _citation(self, match: re.Match[str], line_no: int) -> Citation:
        path, start_text, end_text = match.group(1), match.group(2), match.group(3)
        start = int(start_text)
        end = int(end_text) if end_text else start
        status = self._check(path, start, end)
        return Citation(match.group(0), path, start, end, status, line_no)

    def _check(self, path: str, start: int, end: int) -> CitationStatus:
        source_file = self._index.file_by_path(path)
        if source_file is None:
            return CitationStatus.NOT_FOUND
        if not self._is_fresh(path):
            return CitationStatus.STALE
        count = self._line_count(path)
        if start < 1 or end < start or count is None or end > count:
            return CitationStatus.OUT_OF_RANGE
        if not self._context.covers(path, start, end):
            return CitationStatus.OUT_OF_CONTEXT
        return CitationStatus.VERIFIED

    def _is_fresh(self, path: str) -> bool:
        if path not in self._fresh:
            source_file = self._index.file_by_path(path)
            try:
                data = (self._project.root_path / path).read_bytes()
                self._fresh[path] = bool(source_file) and hashlib.sha256(data).hexdigest() == source_file.content_hash
                self._line_counts[path] = len(data.decode("utf-8", errors="replace").splitlines())
            except OSError:
                self._fresh[path] = False
                self._line_counts[path] = None
        return self._fresh[path]

    def _line_count(self, path: str) -> int | None:
        self._is_fresh(path)
        return self._line_counts.get(path)

    def _unknown_identifier(self, token: str) -> bool:
        if not _IDENTIFIER.match(token):
            return False  # 式・コード片・日本語などは対象外
        name = token.removesuffix("()")
        if name in _BUILTIN_WORDS or len(name) < 3:
            return False
        parts = name.split(".")
        if len(parts) > 1 and parts[-1].lower() in _FILE_EXTENSIONS:
            return False  # cache.py / library.json のようなファイル名は、識別子ではない
        if name in self._known_names or name in self._context_words:
            return False
        # a.b.c は、すべての要素が根拠にあるか、解析済みシンボルの末尾に一致すれば実在とみなす
        if all(p in self._context_words or p in self._known_names or p in _BUILTIN_WORDS for p in parts):
            return False
        return True

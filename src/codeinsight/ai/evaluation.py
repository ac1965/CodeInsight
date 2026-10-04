from __future__ import annotations

import re
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from codeinsight.ai.citations import CitationStatus
from codeinsight.ai.config import AIConfig
from codeinsight.ai.context import ContextError
from codeinsight.ai.provider import AIProvider, AIProviderError
from codeinsight.ai.service import ExplanationService
from codeinsight.application import AnalysisCoordinator, NavigationService, ProjectManager
from codeinsight.application.project_index import ProjectIndex
from codeinsight.bootstrap import build_symbol_extractor
from codeinsight.domain import Project
from codeinsight.infrastructure.analysis_repository import AnalysisRepository

KINDS = ("symbol", "file", "path", "question")
DEFAULT_MIN_TERM_RECALL = 0.6


class EvalError(Exception):
    """評価ケースの定義の誤り。"""


@dataclass(frozen=True)
class EvalCase:
    """AI解説の評価ケース。期待する根拠は行番号ではなくシンボル名で書き、実行時に位置へ解決する。"""

    case_id: str
    kind: str
    project: str  # 評価対象のプロジェクトのディレクトリ（ケースファイルからの相対）
    target: str = ""  # symbol: シンボル名 / file: 相対パス / path: 起点のシンボル名
    target2: str = ""  # path: 到達先のシンボル名
    question: str = ""
    expect_symbols: tuple[str, ...] = ()  # 引用されるべき根拠（シンボルの定義範囲）
    expect_terms: tuple[str, ...] = ()  # 回答に現れるべき語
    forbidden_terms: tuple[str, ...] = ()  # コードに存在しない事柄（作り話の罠）。回答に現れたら不合格
    min_term_recall: float = DEFAULT_MIN_TERM_RECALL
    note: str = ""


@dataclass
class EvalResult:
    case: EvalCase
    status: str  # pass / fail / error
    reasons: list[str] = field(default_factory=list)
    error: str = ""
    validation_status: str = ""
    citations_total: int = 0
    citations_valid: int = 0
    evidence_recall: float | None = None
    term_recall: float | None = None
    forbidden_hits: list[str] = field(default_factory=list)
    unsupported_lines: int = 0
    unknown_identifiers: int = 0
    unknown_names: list[str] = field(default_factory=list)
    seconds: float = 0.0
    answer: str = ""


@dataclass
class EvalSummary:
    results: list[EvalResult]

    @property
    def evaluated(self) -> list[EvalResult]:
        return [r for r in self.results if r.status != "error"]

    @property
    def pass_rate(self) -> float | None:
        return sum(r.status == "pass" for r in self.evaluated) / len(self.evaluated) if self.evaluated else None

    @property
    def citation_validity(self) -> float | None:
        total = sum(r.citations_total for r in self.evaluated)
        return sum(r.citations_valid for r in self.evaluated) / total if total else None

    @staticmethod
    def _mean(values: list[float | None]) -> float | None:
        present = [v for v in values if v is not None]
        return sum(present) / len(present) if present else None

    @property
    def mean_evidence_recall(self) -> float | None:
        return self._mean([r.evidence_recall for r in self.evaluated])

    @property
    def mean_term_recall(self) -> float | None:
        return self._mean([r.term_recall for r in self.evaluated])

    @property
    def forbidden_total(self) -> int:
        return sum(len(r.forbidden_hits) for r in self.evaluated)

    @property
    def unknown_identifier_total(self) -> int:
        return sum(r.unknown_identifiers for r in self.evaluated)

    @property
    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.evaluated:
            counts[r.validation_status] = counts.get(r.validation_status, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "cases": len(self.results), "errors": len(self.results) - len(self.evaluated), "pass_rate": self.pass_rate,
            "citation_validity": self.citation_validity, "mean_evidence_recall": self.mean_evidence_recall,
            "mean_term_recall": self.mean_term_recall, "forbidden_hits": self.forbidden_total,
            "unknown_identifiers": self.unknown_identifier_total, "validation_status": self.status_counts,
            "results": [
                {
                    "id": r.case.case_id, "status": r.status, "reasons": r.reasons, "error": r.error,
                    "validation_status": r.validation_status, "citations": [r.citations_valid, r.citations_total],
                    "evidence_recall": r.evidence_recall, "term_recall": r.term_recall,
                    "forbidden_hits": r.forbidden_hits, "unsupported_lines": r.unsupported_lines,
                    "unknown_identifiers": r.unknown_identifiers, "unknown_names": r.unknown_names,
                    "seconds": round(r.seconds, 1), "answer": r.answer,
                }
                for r in self.results
            ],
        }


def load_cases(path: Path) -> list[EvalCase]:
    """TOML（`[[case]]` の配列）から評価ケースを読む。定義の誤りは、その場で明示して止める。"""

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise EvalError(f"評価ケースを読めません: {path}: {exc}") from exc
    cases: list[EvalCase] = []
    seen: set[str] = set()
    for raw in data.get("case", []):
        case_id = str(raw.get("id", "")).strip()
        kind = raw.get("kind")
        if not case_id or case_id in seen:
            raise EvalError(f"ケースのidが無い、または重複しています: {case_id or '(空)'}")
        seen.add(case_id)
        if kind not in KINDS:
            raise EvalError(f"{case_id}: kind は {', '.join(KINDS)} のいずれかにしてください（{kind}）")
        if not raw.get("project"):
            raise EvalError(f"{case_id}: project（対象ディレクトリ）が必要です")
        needed = {"symbol": "target", "file": "target", "path": "target", "question": "question"}[kind]
        if not raw.get(needed) or (kind == "path" and not raw.get("target2")):
            raise EvalError(f"{case_id}: {kind} には {needed}{' と target2' if kind == 'path' else ''} が必要です")
        if not (raw.get("expect_symbols") or raw.get("expect_terms")):
            raise EvalError(f"{case_id}: expect_symbols か expect_terms のどちらかは必要です（期待が無いと評価できません）")
        cases.append(
            EvalCase(
                case_id=case_id, kind=kind, project=str((path.parent / raw["project"]).resolve()),
                target=raw.get("target", ""), target2=raw.get("target2", ""), question=raw.get("question", ""),
                expect_symbols=tuple(raw.get("expect_symbols", ())), expect_terms=tuple(raw.get("expect_terms", ())),
                forbidden_terms=tuple(raw.get("forbidden_terms", ())),
                min_term_recall=float(raw.get("min_term_recall", DEFAULT_MIN_TERM_RECALL)),
                note=raw.get("note", ""),
            )
        )
    if not cases:
        raise EvalError(f"評価ケースがありません: {path}")
    return cases


class _Workspace:
    """評価対象のプロジェクトを、一時DBに解析して保持する（利用者の解析結果DBや、対象のリポジトリは変更しない）。"""

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory(prefix="codeinsight-eval-")
        self._prepared: dict[str, tuple[AnalysisRepository, Project, ProjectIndex, NavigationService]] = {}

    def get(self, project_dir: str) -> tuple[AnalysisRepository, Project, ProjectIndex, NavigationService]:
        if project_dir not in self._prepared:
            root = Path(project_dir)
            if not root.is_dir():
                raise EvalError(f"対象ディレクトリがありません: {project_dir}")
            repository = AnalysisRepository(Path(self._dir.name) / f"db{len(self._prepared)}.sqlite")
            project = ProjectManager(repository).register(root)
            AnalysisCoordinator(repository, build_symbol_extractor()).analyze_project(project)
            navigation = NavigationService(repository)
            self._prepared[project_dir] = (repository, project, navigation.load_index(project).materialize(), navigation)
        return self._prepared[project_dir]

    def close(self) -> None:
        for repository, *_ in self._prepared.values():
            repository.close()
        self._dir.cleanup()


def evaluate(
    cases: list[EvalCase],
    config: AIConfig,
    provider: AIProvider | None = None,
    repeat: int = 1,
    on_result=None,
) -> EvalSummary:
    """評価ケースを実行し、機械的な指標で採点する。AIには、解析結果を根拠に解説を生成させる（保存はしない）。"""

    workspace = _Workspace()
    results: list[EvalResult] = []
    try:
        for case in cases:
            for _ in range(max(repeat, 1)):
                result = _run_case(case, config, provider, workspace)
                results.append(result)
                if on_result is not None:
                    on_result(result)
    finally:
        workspace.close()
    return EvalSummary(results)


def _run_case(case: EvalCase, config: AIConfig, provider: AIProvider | None, workspace: _Workspace) -> EvalResult:
    started = time.monotonic()
    try:
        repository, project, index, navigation = workspace.get(case.project)
        service = ExplanationService(repository, navigation, config, provider)
        if case.kind == "symbol":
            symbol = navigation.resolve_symbol(index, case.target).symbol
            result = service.explain_symbol(project, index, symbol, save=False)
        elif case.kind == "file":
            result = service.explain_file(project, index, case.target, save=False)
        elif case.kind == "path":
            source = navigation.resolve_symbol(index, case.target).symbol
            target = navigation.resolve_symbol(index, case.target2).symbol
            result = service.explain_path(project, index, source, target, save=False)
        else:
            result = service.ask(project, index, case.question, save=False)
        spans = [
            (index.path_of(s.file_id), s.start_line, s.end_line)
            for s in (navigation.resolve_symbol(index, q).symbol for q in case.expect_symbols)
        ]
    except (AIProviderError, ContextError) as exc:
        return EvalResult(case, "error", error=str(exc), seconds=time.monotonic() - started)
    except Exception as exc:  # 設定・ケースの誤り（シンボルが見つからない等）も、評価全体を止めずに記録する
        return EvalResult(case, "error", error=f"{type(exc).__name__}: {exc}", seconds=time.monotonic() - started)

    assert result.completion is not None and result.report is not None
    return score(case, result.completion.text, result.report, spans, time.monotonic() - started)


def _mentions(text: str, term: str) -> bool:
    """語が回答に現れるか。英数字・_ の語は単語境界で照合する（`ORM` が `transform` に一致しないように）。"""

    pattern = re.escape(term)
    if term[:1].isascii() and (term[:1].isalnum() or term[:1] == "_"):
        pattern = r"(?<![A-Za-z0-9_])" + pattern
    if term[-1:].isascii() and (term[-1:].isalnum() or term[-1:] == "_"):
        pattern += r"(?![A-Za-z0-9_])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def score(case: EvalCase, answer: str, report, expected_spans: list[tuple[str, int, int]], seconds: float = 0.0) -> EvalResult:
    """回答を、機械的な指標で採点する（引用の妥当性・期待する根拠の再現・語の再現・作り話の罠・名前の実在）。"""

    verified = [c for c in report.citations if c.status == CitationStatus.VERIFIED]
    result = EvalResult(
        case, "pass", validation_status=report.status.value, citations_total=len(report.citations),
        citations_valid=len(verified), unsupported_lines=report.count("unsupported"),
        unknown_identifiers=len(report.unknown_identifiers), unknown_names=[name for _, name in report.unknown_identifiers],
        seconds=seconds, answer=answer,
    )
    if expected_spans:
        covered = sum(
            1 for path, start, end in expected_spans
            if any(c.path == path and c.start <= end and c.end >= start for c in verified)
        )
        result.evidence_recall = covered / len(expected_spans)
    if case.expect_terms:
        result.term_recall = sum(1 for t in case.expect_terms if _mentions(answer, t)) / len(case.expect_terms)
    result.forbidden_hits = [t for t in case.forbidden_terms if _mentions(answer, t)]

    if report.bad_citations:
        result.reasons.append(f"検証できない引用が{len(report.bad_citations)}件ある")
    if not report.citations:
        result.reasons.append("引用が一つも無い")
    if result.unknown_identifiers:
        result.reasons.append(f"存在を確認できない名前が{result.unknown_identifiers}件ある")
    if result.evidence_recall is not None and result.evidence_recall == 0:
        result.reasons.append("期待する根拠（定義の範囲）を、どれも引用していない")
    if result.term_recall is not None and result.term_recall < case.min_term_recall:
        result.reasons.append(f"期待する語の再現率が低い（{result.term_recall:.0%} < {case.min_term_recall:.0%}）")
    if result.forbidden_hits:
        result.reasons.append(f"コードに無い事柄に言及している: {', '.join(result.forbidden_hits)}")
    if result.reasons:
        result.status = "fail"
    return result

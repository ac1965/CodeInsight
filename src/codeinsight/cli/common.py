"""CLIの共通部品（エラー・出力・プロジェクト選択・表示の整形）。"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

from codeinsight.analysis.call_graph import CallNode
from codeinsight.application import AmbiguousSymbolError, FreshnessService, NavigationService, ProjectIndex, SymbolNotFoundError
from codeinsight.application.navigation_service import DependencyHit, ReferenceHit
from codeinsight.application.search_service import SymbolHit
from codeinsight.domain import Confidence, FileFreshness, Project, ResolutionStatus, SymbolKind
from codeinsight.infrastructure import AnalysisRepository, default_db_path
from codeinsight.presentation.labels import STATIC_NOTE, STATUS_LABEL, status_text  # noqa: F401  (他のCLIモジュールが共通部品として参照する)

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")



class CliError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def safe(text: object) -> str:
    """解析対象（信頼できない入力）由来の文字列から、端末の制御文字を無害化する（4.4）。"""

    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(text))


def emit_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# --- 共通: DB・プロジェクトの選択 ---


def resolve_db_path(args: argparse.Namespace) -> Path:
    return Path(args.db) if args.db else default_db_path()


def _open_existing(args: argparse.Namespace) -> AnalysisRepository:
    db_path = resolve_db_path(args)
    if not db_path.is_file():
        raise CliError(f"データベースが見つかりません: {db_path}（先に analyze を実行してください）")
    return AnalysisRepository(db_path)


def _select_project(repository: AnalysisRepository, selector: str | None) -> Project:
    projects = repository.list_projects()
    if not projects:
        raise CliError("プロジェクトが登録されていません。先に analyze を実行してください。")
    if selector:
        resolved = Path(selector).expanduser().resolve()
        for project in projects:
            if selector in (project.project_id, project.name) or project.root_path == resolved:
                return project
        raise CliError(f"プロジェクトが見つかりません: {selector}")
    if len(projects) > 1:
        names = ", ".join(f"{p.name}({p.project_id})" for p in projects)
        raise CliError(f"複数のプロジェクトがあります。--project で指定してください: {names}")
    return projects[0]


OPENED: list[AnalysisRepository] = []  # コマンド終了時にまとめて閉じる（索引が遅延読み込みのため）


def open_project_context(args: argparse.Namespace):
    repository = _open_existing(args)
    OPENED.append(repository)
    try:
        project = _select_project(repository, args.project)
    except CliError:
        repository.close()
        raise
    return repository, project


def stale_file_paths(project: Project, index: ProjectIndex) -> list[str]:
    states = FreshnessService().check_project(project, list(index.files.values()))
    return sorted(
        index.files[fid].relative_path
        for fid, state in states.items()
        if state in (FileFreshness.STALE, FileFreshness.MISSING)
    )


def warn_if_stale(stale: list[str]) -> None:
    if stale:
        shown = ", ".join(stale[:5]) + (f" ほか{len(stale) - 5}件" if len(stale) > 5 else "")
        print(
            f"警告: {len(stale)} 個のファイルが解析後に変更されています。"
            f"表示される行番号などは古い可能性があります（再解析: codeinsight analyze）: {shown}",
            file=sys.stderr,
        )


def prepare_read(args: argparse.Namespace):
    """読み取り系コマンドの共通準備。(repository, project, index, stale) を返す。"""

    repository, project = open_project_context(args)
    index = NavigationService(repository).load_index(project)
    stale = stale_file_paths(project, index)
    index = index.excluding(getattr(args, "exclude", None) or [])
    return repository, project, index, stale


# --- 表示用の整形 ---


def symbol_dict(hit: SymbolHit) -> dict:
    s = hit.symbol
    return {
        "name": s.name,
        "qualified_name": s.qualified_name,
        "kind": s.kind.value,
        "path": hit.path,
        "start_line": s.start_line,
        "end_line": s.end_line,
    }


def reference_dict(hit: ReferenceHit) -> dict:
    r = hit.reference
    return {
        "kind": r.reference_kind.value,
        "source": hit.source.qualified_name,
        "target_name": r.target_name,
        "target": hit.target.qualified_name if hit.target else None,
        "status": r.resolution_status.value,
        "confidence": r.confidence.value,
        "path": hit.path,
        "line": r.source_location.start_line,
        "note": r.note,
    }


def dependency_dict(hit: DependencyHit) -> dict:
    d = hit.dependency
    return {
        "kind": d.dependency_kind.value,
        "source": hit.source_path,
        "target_name": d.target_name,
        "target": hit.target_path,
        "status": d.resolution_status.value,
        "confidence": d.confidence.value,
        "line": d.evidence_location.start_line,
        "note": d.note,
    }


class NoteTable:
    """同じ理由（注記）を各行に繰り返さず、脚注番号で示すための表。"""

    def __init__(self) -> None:
        self._numbers: dict[str, int] = {}

    def mark(self, note: str) -> str:
        if not note:
            return ""
        number = self._numbers.setdefault(note, len(self._numbers) + 1)
        return f"※{number}"

    def print_footer(self) -> None:
        if self._numbers:
            print()
            for note, number in self._numbers.items():
                print(f"※{number} {safe(note)}")


def format_reference(hit: ReferenceHit, notes: NoteTable | None = None) -> str:
    r = hit.reference
    status = status_text(r.resolution_status, r.confidence == Confidence.INFERRED)
    target = hit.target.qualified_name if hit.target else r.target_name
    mark = notes.mark(r.note) if notes is not None else ""
    note = f"  # {safe(r.note)}" if r.note and notes is None else ""
    return (
        f"{safe(hit.location)}\t{r.reference_kind.value}\t"
        f"{safe(hit.source.qualified_name)} -> {safe(target)}\t[{status}{mark}]{note}"
    )


def print_call_tree(
    node: CallNode, prefix: str = "", is_root: bool = True, show_external: bool = False
) -> None:
    """呼び出し階層を表示する。同じ呼び出しは1行に集約し、外部呼び出しは既定で省略する。"""

    if is_root:
        print(safe(node.label))
    hidden_external = 0
    groups: dict[tuple, list[CallNode]] = {}
    for child in node.children:
        status = child.reference.resolution_status if child.reference else None
        if status == ResolutionStatus.EXTERNAL and not show_external:
            hidden_external += 1
            continue
        key = (
            child.label,
            child.symbol.symbol_id if child.symbol else None,
            status,
            child.recursive,
            child.truncated,
            bool(child.reference and child.reference.confidence == Confidence.INFERRED),
        )
        groups.setdefault(key, []).append(child)

    entries = list(groups.values())
    for position, group in enumerate(entries):
        first = group[0]
        last = position == len(entries) - 1 and hidden_external == 0
        marks = []
        if first.symbol is None and first.reference is not None:
            marks.append(STATUS_LABEL[first.reference.resolution_status])
        if first.recursive:
            marks.append("再帰")
        if first.truncated:
            marks.append("深さ制限で省略")
        if first.reference is not None and first.reference.confidence == Confidence.INFERRED and first.symbol:
            marks.append("推定")
        if len(group) > 1:
            marks.append(f"×{len(group)}")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        lines = sorted({c.reference.source_location.start_line for c in group if c.reference})
        shown = ", ".join(f"L{n}" for n in lines[:4]) + (" …" if len(lines) > 4 else "")
        location = f"  ({shown})" if lines else ""
        print(f"{prefix}{'└── ' if last else '├── '}{safe(first.label)}{suffix}{location}")
        print_call_tree(first, prefix + ("    " if last else "│   "), is_root=False, show_external=show_external)
    if hidden_external:
        print(f"{prefix}└── … 外部の呼び出し {hidden_external}件を省略（--external で表示）")


def _suggest(index: ProjectIndex, query: str) -> str:
    """見つからない名前に近いシンボル名を「もしかして」として返す。"""

    names = sorted({s.name for s in index.symbols.values() if s.kind != SymbolKind.LOCAL_VARIABLE})
    close = difflib.get_close_matches(query.rsplit(".", 1)[-1], names, n=3, cutoff=0.6)
    return f"\nもしかして: {', '.join(close)}" if close else ""


def symbol_not_found(project: Project, index: ProjectIndex, query: str) -> CliError:
    return CliError(
        f"プロジェクト '{safe(project.name)}' にシンボルが見つかりません: {safe(query)}"
        f"{_suggest(index, query)}",
        2,
    )


def resolve_symbol_arg(
    args: argparse.Namespace,
    navigation: NavigationService,
    index: ProjectIndex,
    query: str,
    project: Project,
):
    try:
        return navigation.resolve_symbol(index, query, file=getattr(args, "file", None))
    except SymbolNotFoundError as exc:
        raise symbol_not_found(project, index, query) from exc
    except AmbiguousSymbolError as exc:
        listing = "\n".join(
            f"  {safe(h.location)}\t{h.symbol.kind.value}\t{safe(h.symbol.qualified_name)}"
            for h in exc.candidates
        )
        raise CliError(
            f"'{safe(query)}' は複数のシンボルに一致します。修飾名か --file で絞り込んでください:\n{listing}",
            2,
        ) from exc

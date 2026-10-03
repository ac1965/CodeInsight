from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from codeinsight.analysis.call_graph import CallNode, Direction
from codeinsight.analysis import flow_analysis as fa
from codeinsight.application.environment_service import EnvironmentService
from codeinsight.application.spec_check_service import SpecCheckService
from codeinsight.application.history_service import HistoryService
from codeinsight.application.impact_service import ImpactService
from codeinsight.application.test_map_service import TestMapService
from codeinsight.application.understand_service import UnderstandService
from codeinsight.application.unused_service import UnusedService
from codeinsight.application.boundary_service import KIND_LABELS as BOUNDARY_LABELS, BoundaryService
from codeinsight.application.config_service import KIND_LABELS as CONFIG_LABELS, ConfigService
from codeinsight.application.architecture_service import ROLE_LABELS, ArchitectureService
from codeinsight.application.external_service import CATEGORY_LABELS, ExternalService
from codeinsight.application import (
    AmbiguousSymbolError,
    AnalysisCoordinator,
    AnalysisProgress,
    CfgBuilder,
    DescribeService,
    FlowAnalysisError,
    FlowService,
    FreshnessService,
    MatchMode,
    NavigationService,
    OverviewService,
    ProjectIndex,
    ProjectManager,
    RiskService,
    SearchService,
    SymbolNotFoundError,
)
from codeinsight.application.risk_service import RULES as RISK_RULES
from codeinsight.application.graph_builder import GraphBuilder, GraphModel, Traversal
from codeinsight.application.navigation_service import DependencyHit, ReferenceHit
from codeinsight.application.search_service import SymbolHit
from codeinsight.bootstrap import build_symbol_extractor
from codeinsight.domain import (
    AnalysisStatus,
    Confidence,
    FileFreshness,
    Project,
    ResolutionStatus,
    SymbolKind,
)
from codeinsight.infrastructure import AnalysisRepository, default_db_path
from codeinsight.presentation import (
    build_structure_tree,
    render_html,
    render_tree_text,
    to_dot,
    to_json,
    to_mermaid,
    tree_to_dict,
)

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_STATUS_LABEL = {
    ResolutionStatus.RESOLVED: "解決",
    ResolutionStatus.UNRESOLVED: "未解決",
    ResolutionStatus.AMBIGUOUS: "曖昧",
    ResolutionStatus.EXTERNAL: "外部",
}
_STATIC_NOTE = "注意: 静的に確認できた関係であり、実行順序や実際に通る経路を示すものではありません。"


class CliError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def safe(text: object) -> str:
    """解析対象（信頼できない入力）由来の文字列から、端末の制御文字を無害化する（4.4）。"""

    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(text))


def _emit_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# --- 共通: DB・プロジェクトの選択 ---


def _db_path(args: argparse.Namespace) -> Path:
    return Path(args.db) if args.db else default_db_path()


def _open_existing(args: argparse.Namespace) -> AnalysisRepository:
    db_path = _db_path(args)
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


_OPENED: list[AnalysisRepository] = []  # コマンド終了時にまとめて閉じる（索引が遅延読み込みのため）


def _context(args: argparse.Namespace):
    repository = _open_existing(args)
    _OPENED.append(repository)
    try:
        project = _select_project(repository, args.project)
    except CliError:
        repository.close()
        raise
    return repository, project


def _stale_files(project: Project, index: ProjectIndex) -> list[str]:
    states = FreshnessService().check_project(project, list(index.files.values()))
    return sorted(
        index.files[fid].relative_path
        for fid, state in states.items()
        if state in (FileFreshness.STALE, FileFreshness.MISSING)
    )


def _warn_stale(stale: list[str]) -> None:
    if stale:
        shown = ", ".join(stale[:5]) + (f" ほか{len(stale) - 5}件" if len(stale) > 5 else "")
        print(
            f"警告: {len(stale)} 個のファイルが解析後に変更されています。"
            f"表示される行番号などは古い可能性があります（再解析: codeinsight analyze）: {shown}",
            file=sys.stderr,
        )


def _prepare(args: argparse.Namespace):
    """読み取り系コマンドの共通準備。(repository, project, index, stale) を返す。"""

    repository, project = _context(args)
    index = NavigationService(repository).load_index(project)
    stale = _stale_files(project, index)
    index = index.excluding(getattr(args, "exclude", None) or [])
    return repository, project, index, stale


# --- 表示用の整形 ---


def _symbol_dict(hit: SymbolHit) -> dict:
    s = hit.symbol
    return {
        "name": s.name,
        "qualified_name": s.qualified_name,
        "kind": s.kind.value,
        "path": hit.path,
        "start_line": s.start_line,
        "end_line": s.end_line,
    }


def _reference_dict(hit: ReferenceHit) -> dict:
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


def _dependency_dict(hit: DependencyHit) -> dict:
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


def _status_text(status: ResolutionStatus, confidence_inferred: bool) -> str:
    label = _STATUS_LABEL[status]
    return f"{label}(推定)" if status == ResolutionStatus.RESOLVED and confidence_inferred else label


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


def _format_reference(hit: ReferenceHit, notes: NoteTable | None = None) -> str:
    r = hit.reference
    status = _status_text(r.resolution_status, r.confidence == Confidence.INFERRED)
    target = hit.target.qualified_name if hit.target else r.target_name
    mark = notes.mark(r.note) if notes is not None else ""
    note = f"  # {safe(r.note)}" if r.note and notes is None else ""
    return (
        f"{safe(hit.location)}\t{r.reference_kind.value}\t"
        f"{safe(hit.source.qualified_name)} -> {safe(target)}\t[{status}{mark}]{note}"
    )


def _print_call_tree(
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
            marks.append(_STATUS_LABEL[first.reference.resolution_status])
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
        _print_call_tree(first, prefix + ("    " if last else "│   "), is_root=False, show_external=show_external)
    if hidden_external:
        print(f"{prefix}└── … 外部の呼び出し {hidden_external}件を省略（--external で表示）")


# --- コマンド ---


def _cmd_analyze(args: argparse.Namespace) -> int:
    root_path = Path(args.path)
    if not root_path.is_dir():
        raise CliError(f"ディレクトリが存在しません: {root_path}")

    db_path = _db_path(args)
    compile_commands_dir = Path(args.compile_commands) if args.compile_commands else root_path
    has_database = (compile_commands_dir / "compile_commands.json").is_file()

    def progress(event: AnalysisProgress) -> None:
        if sys.stderr.isatty():
            print(f"\r[{event.done}/{event.total}] {safe(event.relative_path)[:60]:<60}", end="", file=sys.stderr)
            if event.done == event.total:
                print(file=sys.stderr)

    with AnalysisRepository(db_path) as repository:
        project = ProjectManager(repository).get_or_register(root_path)
        coordinator = AnalysisCoordinator(
            repository, build_symbol_extractor(compile_commands_dir if has_database else None)
        )
        result = coordinator.analyze_project(project, progress=progress)
        source_files = repository.list_source_files(project.project_id)
        symbols = repository.list_symbols_for_project(project.project_id)
        references = repository.list_references_for_project(project.project_id)

    if args.format == "json":
        _emit_json(
            {
                "project": {"name": project.name, "id": project.project_id},
                "database": str(db_path),
                "files": len(source_files),
                "symbols": len(symbols),
                "references": len(references),
                "status": result.status.value,
                "warnings": list(result.warnings),
                "errors": list(result.errors),
            }
        )
    else:
        print(f"プロジェクト: {safe(project.name)} ({project.project_id})")
        print(f"データベース: {db_path}")
        print(f"解析対象ファイル数: {len(source_files)}")
        print(f"抽出シンボル数: {len(symbols)}")
        print(f"抽出した参照数: {len(references)}")
        print(f"解析結果: {result.status.value}")
        if result.warnings:
            print(f"警告 {len(result.warnings)}件:")
            for warning in result.warnings:
                print(f"  - {safe(warning)}")
        if result.errors:
            print(f"エラー {len(result.errors)}件:")
            for error in result.errors:
                print(f"  - {safe(error)}")
    return 0 if result.status != AnalysisStatus.FAILED else 1


def _cmd_status(args: argparse.Namespace) -> int:
    repository, project = _context(args)
    index = NavigationService(repository).load_index(project)
    freshness = FreshnessService().check_project(project, list(index.files.values()))
    results = repository.list_analysis_results(project.project_id)
    latest = results[0] if results else None
    counts = Counter(r.resolution_status for r in index.references)
    rows = [
        {"path": f.relative_path, "analysis": f.analysis_status.value, "freshness": freshness[f.file_id].value}
        for f in index.files.values()
    ]
    rows.sort(key=lambda r: r["path"])
    if args.format == "json":
        _emit_json(
            {
                "project": project.name,
                "revision": project.repository_revision,
                "latest_analysis": {
                    "status": latest.status.value,
                    "timestamp": latest.analysis_timestamp.isoformat(),
                    "analyzer_version": latest.analyzer_version,
                    "revision": latest.repository_revision,
                }
                if latest
                else None,
                "references": {k.value: v for k, v in counts.items()},
                "files": rows,
            }
        )
        return 0
    print(f"プロジェクト: {safe(project.name)} ({project.project_id})")
    print(f"ルート: {project.root_path}")
    if latest:
        print(
            f"最新の解析: {latest.status.value} / {latest.analysis_timestamp.isoformat()} / "
            f"解析器 {latest.analyzer_version} / リビジョン {latest.repository_revision or '-'}"
        )
    else:
        print("最新の解析: なし")
    print(
        "参照の解決状況: "
        + ", ".join(f"{_STATUS_LABEL[s]} {counts.get(s, 0)}" for s in ResolutionStatus)
    )
    labels = {"fresh": "最新", "stale": "古い(ソース変更あり)", "missing": "ファイルなし", "unanalyzed": "未解析/失敗"}
    for row in rows:
        print(f"{labels[row['freshness']]}\t{row['analysis']}\t{safe(row['path'])}")
    return 0


def _cmd_symbols(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    hits = [SymbolHit(s, index.path_of(s.file_id)) for s in index.symbols.values()]
    if args.file:
        hits = [h for h in hits if h.path == args.file]
    if args.kind:
        kinds = {SymbolKind(k) for k in args.kind}
        hits = [h for h in hits if h.symbol.kind in kinds]
    hits.sort(key=lambda h: (h.path, h.symbol.start_line))
    _warn_stale(stale)
    if args.format == "json":
        _emit_json({"stale_files": stale, "results": [_symbol_dict(h) for h in hits]})
        return 0
    print(f"プロジェクト: {safe(project.name)} ({project.project_id})")
    for hit in hits:
        print(f"{safe(hit.location)}\t{hit.symbol.kind.value}\t{safe(hit.symbol.qualified_name)}")
    return 0


def _cmd_tree(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    if args.path:
        index = index.under(args.path)
        if not index.files:
            raise CliError(f"パスに一致するファイルがありません: {safe(args.path)}", 2)
    tree = build_structure_tree(
        index,
        project.name,
        include_locals=args.locals,
        include_variables=not args.no_variables,
        max_symbol_depth=args.depth,
    )
    _warn_stale(stale)
    if args.format == "json":
        _emit_json(tree_to_dict(tree))
    else:
        print(safe(render_tree_text(tree)), end="")
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    repository, project = _context(args)
    search = SearchService(repository)
    if args.files:
        files = search.search_files(project, args.query)
        payload = [{"kind": "file", "path": f.relative_path, "language": f.language.value} for f in files]
        lines = [f"{safe(f.relative_path)}\t{f.language.value}" for f in files]
        stale: list[str] = []
    elif args.text:
        try:
            hits = search.search_text(
                project, args.query, ignore_case=args.ignore_case, regex=args.regex, limit=args.limit
            )
        except re.error as exc:
            raise CliError(f"正規表現が不正です: {exc}") from exc
        payload = [
            {"kind": "text", "path": h.path, "line": h.line, "text": h.text, "freshness": h.freshness.value}
            for h in hits
        ]
        lines = [
            f"{safe(h.path)}:{h.line}\t{safe(h.text.strip())}"
            + ("\t[解析後に変更あり]" if h.freshness == FileFreshness.STALE else "")
            for h in hits
        ]
        stale = []
    else:
        kinds = [SymbolKind(k) for k in args.kind] if args.kind else None
        symbol_hits = search.search_symbols(
            project,
            args.query,
            kinds=kinds,
            match=MatchMode(args.match),
            file=args.file,
            limit=args.limit,
        )
        payload = [{"search": "symbol", **_symbol_dict(h)} for h in symbol_hits]
        lines = [
            f"{safe(h.location)}\t{h.symbol.kind.value}\t{safe(h.symbol.qualified_name)}"
            for h in symbol_hits
        ]
        index = NavigationService(repository).load_index(project)
        stale = _stale_files(project, index)
    _warn_stale(stale)
    if args.format == "json":
        _emit_json({"stale_files": stale, "results": payload})
    else:
        for line in lines:
            print(line)
        if not lines:
            print("一致するものはありませんでした。", file=sys.stderr)
    return 0


def _suggest(index: ProjectIndex, query: str) -> str:
    """見つからない名前に近いシンボル名を「もしかして」として返す。"""

    names = sorted({s.name for s in index.symbols.values() if s.kind != SymbolKind.LOCAL_VARIABLE})
    close = difflib.get_close_matches(query.rsplit(".", 1)[-1], names, n=3, cutoff=0.6)
    return f"\nもしかして: {', '.join(close)}" if close else ""


def _not_found(project: Project, index: ProjectIndex, query: str) -> CliError:
    return CliError(
        f"プロジェクト '{safe(project.name)}' にシンボルが見つかりません: {safe(query)}"
        f"{_suggest(index, query)}",
        2,
    )


def _resolve(
    args: argparse.Namespace,
    navigation: NavigationService,
    index: ProjectIndex,
    query: str,
    project: Project,
):
    try:
        return navigation.resolve_symbol(index, query, file=getattr(args, "file", None))
    except SymbolNotFoundError as exc:
        raise _not_found(project, index, query) from exc
    except AmbiguousSymbolError as exc:
        listing = "\n".join(
            f"  {safe(h.location)}\t{h.symbol.kind.value}\t{safe(h.symbol.qualified_name)}"
            for h in exc.candidates
        )
        raise CliError(
            f"'{safe(query)}' は複数のシンボルに一致します。修飾名か --file で絞り込んでください:\n{listing}",
            2,
        ) from exc


def _cmd_def(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    hits = NavigationService(repository).lookup(index, args.name, file=args.file)
    if not hits:
        raise _not_found(project, index, args.name)
    _warn_stale(stale)
    if args.format == "json":
        _emit_json({"stale_files": stale, "results": [_symbol_dict(h) for h in hits]})
    else:
        for hit in hits:
            print(f"{safe(hit.location)}\t{hit.symbol.kind.value}\t{safe(hit.symbol.qualified_name)}")
    return 0


def _reference_command(args: argparse.Namespace, mode: str) -> int:
    repository, project, index, stale = _prepare(args)
    navigation = NavigationService(repository)
    symbol = _resolve(args, navigation, index, args.name, project).symbol
    if mode == "refs":
        hits = navigation.references_to(index, symbol)
    elif mode == "callers":
        hits = navigation.callers(index, symbol)
    else:
        hits = navigation.callees(index, symbol)
        if args.hide_external:
            hits = [h for h in hits if h.reference.resolution_status != ResolutionStatus.EXTERNAL]
    _warn_stale(stale)
    if args.format == "json":
        _emit_json(
            {
                "symbol": symbol.qualified_name,
                "stale_files": stale,
                "results": [_reference_dict(h) for h in hits],
            }
        )
        return 0
    notes = NoteTable()
    for hit in hits:
        print(_format_reference(hit, notes))
    notes.print_footer()
    if not hits:
        print("該当する関係は確認できませんでした（未解決の関係は unresolved コマンドで確認できます）。", file=sys.stderr)
    if mode in ("callers", "callees"):
        print(_STATIC_NOTE, file=sys.stderr)
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    navigation = NavigationService(repository)
    symbol = _resolve(args, navigation, index, args.name, project).symbol
    tree = navigation.call_hierarchy(index, symbol, Direction(args.direction), args.depth)
    _warn_stale(stale)
    print(f"{'呼び出し先' if args.direction == 'callees' else '呼び出し元'}の階層 (深さ {args.depth}):")
    _print_call_tree(tree, show_external=args.external)
    print(_STATIC_NOTE, file=sys.stderr)
    return 0


def _cmd_path(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    navigation = NavigationService(repository)
    source = _resolve(args, navigation, index, args.source, project).symbol
    target = _resolve(args, navigation, index, args.target, project).symbol
    paths = navigation.call_paths(index, source, target, args.max_depth, args.limit)
    _warn_stale(stale)
    if args.format == "json":
        _emit_json(
            {
                "source": source.qualified_name,
                "target": target.qualified_name,
                "paths": [[_reference_dict(h) for h in path] for path in paths],
            }
        )
        return 0
    if not paths:
        print("静的に確認できる呼び出し経路は見つかりませんでした（関数ポインタ等の未解決の呼び出しは含みません）。")
        return 0
    for number, path in enumerate(paths, start=1):
        chain = " -> ".join([safe(source.qualified_name), *[safe(h.target.qualified_name) for h in path if h.target]])
        print(f"経路{number}: {chain}")
        for hit in path:
            print(f"    {safe(hit.location)}")
    print(_STATIC_NOTE, file=sys.stderr)
    return 0


def _cmd_deps(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    navigation = NavigationService(repository)
    if args.cycles:
        cycles = navigation.dependency_cycles(index)
        if args.format == "json":
            _emit_json({"cycles": cycles})
        else:
            for cycle in cycles:
                print(" <-> ".join(safe(p) for p in cycle))
            if not cycles:
                print("循環する依存関係は確認できませんでした。")
        return 0
    try:
        hits = navigation.dependencies(index, args.file, dependents=args.dependents)
    except SymbolNotFoundError as exc:
        raise CliError(f"ファイルが見つかりません: {safe(args.file)}", 2) from exc
    if not args.external:
        hits = [h for h in hits if h.dependency.resolution_status != ResolutionStatus.EXTERNAL]
    _warn_stale(stale)
    if args.format == "json":
        _emit_json({"stale_files": stale, "results": [_dependency_dict(h) for h in hits]})
        return 0
    notes = NoteTable()
    for hit in hits:
        d = hit.dependency
        status = _status_text(d.resolution_status, d.confidence == Confidence.INFERRED)
        target = hit.target_path or d.target_name
        print(
            f"{safe(hit.location)}\t{d.dependency_kind.value}\t"
            f"{safe(hit.source_path)} -> {safe(target)}\t[{status}{notes.mark(d.note)}]"
        )
    notes.print_footer()
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    match = re.fullmatch(r"(?P<path>.+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?", args.location)
    if match is None:
        raise CliError("位置は FILE[:LINE[-END]] またはシンボル名で指定してください。")
    repository, project, index, stale = _prepare(args)
    navigation = NavigationService(repository)
    path, start, end = match["path"], match["start"], match["end"]
    context = args.context
    if index.file_by_path(path) is None:
        # ファイルでなければ、シンボル名として解釈し、その定義全体を表示する。
        symbol = _resolve(args, navigation, index, args.location, project).symbol
        path, start, end = index.path_of(symbol.file_id), str(symbol.start_line), str(symbol.end_line)
        context = 0
        print(f"{safe(symbol.qualified_name)}  ({symbol.kind.value}, {safe(path)}:{start}-{end})")
    start_line = int(start) if start else None
    end_line = int(end) if end else None
    try:
        view = navigation.show_source(project, index, path, start_line, end_line, context)
    except SymbolNotFoundError as exc:
        raise CliError(f"解析対象のファイルが見つかりません: {safe(path)}", 2) from exc
    except OSError as exc:
        raise CliError(f"ファイルを読み込めません: {exc}") from exc
    if view.freshness != FileFreshness.FRESH:
        label = {"stale": "解析後に変更されています", "missing": "ファイルが存在しません", "unanalyzed": "解析に成功していません"}
        print(f"警告: {safe(view.path)} は{label[view.freshness.value]}。解析結果と一致しない可能性があります。", file=sys.stderr)
    width = len(str(view.lines[-1][0])) if view.lines else 1
    for number, text in view.lines:
        mark = ">" if view.highlight_start <= number <= view.highlight_end else " "
        print(f"{mark} {number:>{width}} | {safe(text)}")
    return 0


def _cmd_describe(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    navigation = NavigationService(repository)
    symbol = _resolve(args, navigation, index, args.name, project).symbol
    description = DescribeService(navigation).describe(project, index, symbol)
    _warn_stale(stale)
    if args.format == "json":
        _emit_json(
            {
                "symbol": _symbol_dict(description.hit),
                "summary": symbol.summary,
                "declaration": [{"line": n, "text": t} for n, t in description.declaration],
                "decorators": list(symbol.decorators),
                "base_classes": list(symbol.base_classes),
                "members": [{"name": m.name, "kind": m.kind.value, "line": m.start_line} for m in description.members],
                "derived_classes": [c.qualified_name for c in description.derived_classes],
                "callers": description.callers,
                "callees": {k.value: v for k, v in description.callees.items()},
                "callees_include_members": description.callees_include_members,
                "references": {k.value: v for k, v in description.references.items()},
                "referencing_files": description.referencing_files,
                "freshness": description.freshness.value,
            }
        )
        return 0
    print(f"{safe(symbol.qualified_name)}  ({symbol.kind.value})")
    print(f"  場所: {safe(description.hit.location)}")
    if symbol.summary:
        print(f"  要約: {safe(symbol.summary)}   （ソースのdocstring/コメントの先頭行）")
    if description.declaration:
        print("  宣言:")
        for number, text in description.declaration:
            print(f"    {number:>5} | {safe(text)}")
    if symbol.decorators:
        print(f"  デコレータ: {', '.join(safe(d) for d in symbol.decorators)}")
    if symbol.base_classes:
        print(f"  基底クラス: {', '.join(safe(b) for b in symbol.base_classes)}")
    if description.derived_classes:
        print(f"  派生クラス: {', '.join(safe(c.qualified_name) for c in description.derived_classes)}")
    if description.members:
        print(f"  メンバ ({len(description.members)}):")
        for member in description.members[: args.members]:
            hint = f"  — {safe(member.summary)}" if member.summary else ""
            print(f"    L{member.start_line:<5} {member.kind.value:<16} {safe(member.name)}{hint}")
        if len(description.members) > args.members:
            print(f"    … ほか {len(description.members) - args.members}件（--members で表示数を変更）")
    callee_counts = description.callees
    print(
        f"  呼び出し元: {description.callers}件 / 呼び出し先"
        + ("（メンバの合計）" if description.callees_include_members else "")
        + ": "
        + (
            "・".join(f"{_STATUS_LABEL[s]} {callee_counts.get(s, 0)}" for s in ResolutionStatus)
            if callee_counts
            else "なし"
        )
    )
    if description.references:
        kinds = ", ".join(f"{k.value} {v}" for k, v in sorted(description.references.items(), key=lambda kv: kv[0].value))
        print(f"  参照: {kinds}（{description.referencing_files}ファイル）")
    else:
        print("  参照: 確認できた参照なし")
    print(
        "  ※ 件数は静的解析で確認できた関係のみで、動的な呼び出し等は含まれません。"
        "詳細は callers / callees / refs / trace を参照。"
    )
    return 0


def _cmd_overview(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    overview = OverviewService(NavigationService(repository)).build(index, args.top)
    _warn_stale(stale)
    if args.format == "json":
        _emit_json(
            {
                "project": project.name,
                "revision": project.repository_revision,
                "languages": {k.value: v for k, v in overview.languages.items()},
                "symbols": {k.value: v for k, v in overview.symbol_counts.items()},
                "modules": [vars(m) for m in overview.modules],
                "entry_points": [_symbol_dict(h) for h in overview.entry_points],
                "most_called": [{**_symbol_dict(r.hit), "callers": r.value} for r in overview.most_called],
                "most_calling": [{**_symbol_dict(r.hit), "callees": r.value} for r in overview.most_calling],
                "largest": [{**_symbol_dict(r.hit), "lines": r.value} for r in overview.largest],
                "dependency_cycles": overview.cycles,
                "reference_status": {k.value: v for k, v in overview.reference_status.items()},
                "failed_files": overview.failed_files,
                "stale_files": stale,
            }
        )
        return 0
    print(f"プロジェクト: {safe(project.name)}  ルート: {project.root_path}")
    print(f"リビジョン: {project.repository_revision or '-'}")
    langs = ", ".join(f"{k.value} {v}ファイル" for k, v in sorted(overview.languages.items(), key=lambda kv: -kv[1]))
    print(f"言語: {langs}")
    counts = overview.symbol_counts
    labels = (
        ("クラス", SymbolKind.CLASS),
        ("関数", SymbolKind.FUNCTION),
        ("メソッド", SymbolKind.METHOD),
        ("構造体", SymbolKind.STRUCT),
        ("関数宣言", SymbolKind.FUNCTION_DECLARATION),
    )
    print("定義: " + ", ".join(f"{label} {counts[kind]}" for label, kind in labels if counts.get(kind)))

    print("\n■ エントリポイントの候補（main関数・__main__.py）")
    for hit in overview.entry_points or []:
        print(f"  {safe(hit.location)}  {safe(hit.symbol.qualified_name)}")
    if not overview.entry_points:
        print("  確認できませんでした")

    print(f"\n■ 主要なモジュール（他から依存される順、上位{len(overview.modules)}件）")
    for m in overview.modules:
        summary = f"  — {safe(m.summary)}" if m.summary else ""
        print(f"  依存される{m.fan_in:>3} / 依存する{m.fan_out:>3}  {m.lines:>5}行  {safe(m.path)}{summary}")

    for title, rows, unit in (
        ("よく呼ばれる関数（呼び出し元の数）", overview.most_called, "元"),
        ("多くを呼び出す関数（呼び出し先の数）", overview.most_calling, "先"),
        ("大きい定義（行数）", overview.largest, "行"),
    ):
        print(f"\n■ {title}")
        for r in rows:
            print(f"  {r.value:>5}{unit}  {safe(r.hit.symbol.qualified_name)}  ({safe(r.hit.location)})")

    print("\n■ 循環する依存関係")
    for cycle in overview.cycles:
        print("  " + " <-> ".join(safe(p) for p in cycle))
    if not overview.cycles:
        print("  確認できませんでした")

    status = overview.reference_status
    total = sum(status.values()) or 1
    print(
        "\n■ 解析の信頼性: 参照 "
        + ", ".join(f"{_STATUS_LABEL[s]} {status.get(s, 0)}" for s in ResolutionStatus)
        + f"（未解決 {status.get(ResolutionStatus.UNRESOLVED, 0) * 100 // total}%）"
    )
    if overview.failed_files:
        print(f"  解析に失敗したファイル: {', '.join(safe(p) for p in overview.failed_files)}")
    if stale:
        print(f"  解析後に変更されたファイル: {len(stale)}件（再解析を推奨）")
    print("\n※ 静的解析で確認できた事実のみです。「何をするか」の説明は、docstringの転記を除き、含みません。")
    return 0


def _flow_service(args: argparse.Namespace):
    repository, project, index, stale = _prepare(args)
    navigation = NavigationService(repository)
    return project, index, navigation, FlowService(navigation), stale


def _flow_symbol(args, navigation, index, project):
    return _resolve(args, navigation, index, args.name, project).symbol


def _cmd_flow(args: argparse.Namespace) -> int:
    project, index, navigation, service, stale = _flow_service(args)
    symbol = _flow_symbol(args, navigation, index, project)
    try:
        summary = service.control_flow(project, index, symbol)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    path = index.path_of(symbol.file_id)
    if args.format == "json":
        _emit_json(
            {
                "symbol": symbol.qualified_name,
                "path": path,
                "metrics": summary.metrics,
                "items": [vars(i) for i in summary.items],
                "handlers": [vars(h) for h in summary.handlers],
                "raises": [{"line": r.line, "exception": r.exception} for r in summary.raises],
                "hints": [vars(h) for h in summary.hints],
            }
        )
        return 0
    m = summary.metrics
    print(f"{safe(symbol.qualified_name)}  ({symbol.kind.value}, {safe(path)}:{symbol.start_line}-{symbol.end_line}, {m['lines']}行)")
    print("制御構造（行番号付き。ネストはインデント）:")
    for item in summary.items:
        detail = f"  {safe(item.detail)}" if item.detail else ""
        print(f"  L{item.line:<5}{'  ' * item.depth}{item.kind}{detail}")
    if not summary.items:
        print("  （分岐・ループ・例外処理・return等はありません）")
    print(
        f"指標: 分岐 {m['branches']}, ループ {m['loops']}, try {m['try_blocks']}/except {m['handlers']}, "
        f"raise {m['raises']}, return {m['returns']}, await {m['awaits']}, with {m['with_blocks']}, "
        f"循環的複雑度 {m['cyclomatic']}, 最大ネスト {m['max_depth']}"
    )
    if summary.handlers:
        print("例外処理:")
        for h in summary.handlers:
            traits = []
            if h.swallowed:
                traits.append("握りつぶし（何もしない）")
            if h.reraises:
                traits.append("再送出あり")
            if h.logs:
                traits.append("ログ/表示あり")
            if "<bare>" in h.types:
                traits.append("型の指定なし")
            print(f"  L{h.line:<5} except {safe(', '.join(h.types))}  {' / '.join(traits) or '処理あり'}")
    if summary.hints:
        print("リトライ・タイムアウト・待機の手がかり（名前・構文からの推定）:")
        labels = {"retry": "リトライ", "timeout": "タイムアウト指定", "sleep": "待機"}
        for hint in summary.hints:
            print(f"  L{hint.line:<5} {labels[hint.kind]}: {safe(hint.detail)}")
    _warn_stale(stale)
    print("※ 構文から確認できた構造です。実行時にどの経路を通るかは示しません。", file=sys.stderr)
    return 0


def _print_trace(trace, prefix: str = "", top: bool = True) -> None:
    if top:
        role = "引数" if trace.is_param else "変数"
        print(f"{role} {safe(trace.name)}  ({safe(trace.symbol.qualified_name)}, {safe(trace.path)})")
        defs = "; ".join(
            f"L{d.line} " + ("引数" if d.how == "param" else f"{d.how}" + (f" ← {safe(d.origin)}" if d.origin else ""))
            for d in trace.definitions
        )
        print(f"{prefix}  定義: {defs or '（なし）'}")
        print(f"{prefix}  使用: {', '.join('L' + str(n) for n in trace.uses) or '（なし）'}")
    for position, flow in enumerate(trace.flows):
        last = position == len(trace.flows) - 1
        status = f"  [{safe(flow.status)}]" if flow.status else ""
        print(f"{prefix}{'└── ' if last else '├── '}L{flow.line} {safe(flow.description)}{status}")
        if flow.child is not None:
            child = flow.child
            print(f"{prefix}{'    ' if last else '│   '}   ▸ {safe(child.symbol.qualified_name)} の {safe(child.name)}"
                  f"（定義 {len(child.definitions)}、使用 {len(child.uses)}）")
            _print_trace(child, prefix + ("    " if last else "│   ") + "   ", top=False)


def _print_upstream(nodes, prefix: str = "") -> None:
    for position, node in enumerate(nodes):
        last = position == len(nodes) - 1
        print(f"{prefix}{'└── ' if last else '├── '}{safe(node.path)}:{node.line}  {safe(node.caller.qualified_name)}  ← {safe(node.argument)}")
        _print_upstream(node.children, prefix + ("    " if last else "│   "))


def _cmd_dataflow(args: argparse.Namespace) -> int:
    project, index, navigation, service, stale = _flow_service(args)
    symbol = _flow_symbol(args, navigation, index, project)
    try:
        if args.variable is None:
            variables = service.variables(project, index, symbol)
            print(f"{safe(symbol.qualified_name)} の変数（定義数 / 使用数 / 伝播先の数）:")
            for name, info in sorted(variables.items(), key=lambda kv: (not kv[1].is_param, min((d.line for d in kv[1].definitions), default=0), kv[0])):
                role = "引数" if info.is_param else "変数"
                print(f"  {role} {safe(name):<20} 定義 {len(info.definitions):>2} / 使用 {len(set(info.uses)):>2} / 伝播 {len(info.flows):>2}")
            print("変数名を指定すると、値の行き先をたどります（例: dataflow <シンボル> <変数名>）。")
            _warn_stale(stale)
            return 0
        trace = service.trace_variable(project, index, symbol, args.variable, args.depth)
        _print_trace(trace)
        if args.upstream:
            if not trace.is_param:
                raise CliError(f"--upstream は引数にのみ使えます: {safe(args.variable)}")
            print("\n呼び出し元から渡される実引数（解決済みの呼び出しのみ）:")
            upstream = service.upstream(project, index, symbol, args.variable, args.depth)
            _print_upstream(upstream)
            if not upstream:
                print("  確認できる呼び出し元がありません")
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    _warn_stale(stale)
    print("※ 実行順序・条件を考慮しない近似です。値そのものや、動的な呼び出しの先は追えません。", file=sys.stderr)
    return 0


def _cmd_state(args: argparse.Namespace) -> int:
    project, index, navigation, service, stale = _flow_service(args)
    symbol = _flow_symbol(args, navigation, index, project)
    try:
        if symbol.kind == SymbolKind.CLASS:
            accesses = service.class_state(project, index, symbol)
        elif symbol.kind == SymbolKind.MODULE:
            writes = service.module_state(project, index, symbol)
            print(f"{safe(symbol.qualified_name)}: 関数内から書き換えられるモジュール変数")
            for w in writes:
                how = "global宣言で再代入" if w.mode == "global_assign" else "破壊的メソッドで変更"
                print(f"  L{w.line:<5} {safe(w.name)}  {safe(w.function)}() が{how}")
            if not writes:
                print("  確認できませんでした")
            _warn_stale(stale)
            return 0
        else:
            raise CliError(f"state はクラスまたはモジュールに使います（{symbol.kind.value}）。")
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    by_attribute: dict[str, list[fa.StateAccess]] = defaultdict(list)
    for access in accesses:
        by_attribute[access.attribute].append(access)
    if args.format == "json":
        _emit_json({a: [vars(x) for x in items] for a, items in by_attribute.items()})
        return 0
    print(f"{safe(symbol.qualified_name)} の状態（self.<属性> の書き込み・変更・読み取り）")
    for attribute, items in sorted(by_attribute.items(), key=lambda kv: min(a.line for a in kv[1])):
        writers = [a for a in items if a.mode in ("write", "mutate")]
        outside_init = {a.method for a in writers if a.method != "__init__"}
        traits = "状態が変化する" if outside_init else ("初期化のみ" if writers else "読み取りのみ（外部から設定される可能性）")
        print(f"\n  {safe(attribute)}  — {traits}")
        for mode, label in (("write", "書き込み"), ("mutate", "変更(破壊的メソッド/添字代入)"), ("read", "読み取り")):
            group = [a for a in items if a.mode == mode]
            if group:
                methods = sorted({(a.method, a.line) for a in group}, key=lambda x: x[1])
                print(f"    {label}: " + ", ".join(f"{safe(m)}:L{n}" for m, n in methods[:8]) + (" …" if len(methods) > 8 else ""))
    _warn_stale(stale)
    return 0


def _cmd_exceptions(args: argparse.Namespace) -> int:
    project, index, navigation, service, stale = _flow_service(args)
    symbol = _flow_symbol(args, navigation, index, project)
    try:
        report = service.exceptions(project, index, symbol, args.depth)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    if args.format == "json":
        _emit_json(
            {
                "symbol": symbol.qualified_name,
                "propagated": [
                    {
                        "exception": e.exception,
                        "raised_in": e.raised_in.qualified_name,
                        "line": e.raised_line,
                        "chain": [{"symbol": s.qualified_name, "line": n} for s, n in e.chain],
                    }
                    for e in report.propagated
                ],
                "unresolved_calls": report.unresolved_calls,
            }
        )
        return 0
    print(f"{safe(symbol.qualified_name)} から呼び出し元へ出うる例外（明示的な raise のみ、深さ {args.depth}）:")
    for exc in sorted(report.propagated, key=lambda e: (e.exception, e.raised_line)):
        origin = index.path_of(exc.raised_in.file_id)
        print(f"  {safe(exc.exception)}   raise: {safe(origin)}:{exc.raised_line}  {safe(exc.raised_in.qualified_name)}")
        if exc.chain:
            route = safe(symbol.qualified_name) + "".join(
                f" →(L{line}) {safe(callee.qualified_name)}" for callee, line in exc.chain
            )
            print(f"      経路: {route}")
    if not report.propagated:
        print("  確認できませんでした")
    if report.caught_inside:
        print("関数内で捕捉される raise:")
        for line, name, types in report.caught_inside:
            print(f"  L{line:<5} {safe(name)}  → except {safe(', '.join(types))}")
    swallowed = [h for h in report.handlers if h.swallowed]
    if swallowed:
        print("握りつぶしている例外処理:")
        for h in swallowed:
            print(f"  L{h.line:<5} except {safe(', '.join(h.types))}")
    if report.unresolved_calls:
        print(f"※ 呼び出し先を特定できない呼び出しが {report.unresolved_calls}件あり、そこから出る例外は追えていません。", file=sys.stderr)
    print("※ 組み込み・外部ライブラリが送出する例外は含みません。", file=sys.stderr)
    _warn_stale(stale)
    return 0


def _cmd_risks(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    rules = set(args.rule) if args.rule else None
    findings, skipped = RiskService().scan(project, index, rules)
    order = ("low", "medium", "high")
    findings = [f for f in findings if order.index(f.severity) >= order.index(args.min_severity)]
    if args.format == "json":
        _emit_json({"skipped": skipped, "findings": [{**vars(f), "message": f.message} for f in findings]})
        return 0
    counts = Counter(f.rule for f in findings)
    print(f"潜在的な問題の手がかり {len(findings)}件（バグの断定ではありません。意図的な実装の場合があります）")
    grouped: dict[str, list] = defaultdict(list)
    for f in findings:
        grouped[f.rule].append(f)
    for rule, items in sorted(grouped.items(), key=lambda kv: (-order.index(kv[1][0].severity), -len(kv[1]))):
        print(f"\n■ [{items[0].severity}] {rule}（{counts[rule]}件） — {items[0].message}")
        for f in items[: args.limit]:
            where = f"  in {safe(f.symbol)}" if f.symbol else ""
            detail = f"  {safe(f.detail)}" if f.detail else ""
            print(f"  {safe(f.path)}:{f.line}{where}{detail}")
        if len(items) > args.limit:
            print(f"  … ほか {len(items) - args.limit}件（--limit / --all）")
    if skipped:
        print(f"\n解析後に変更された等で対象外にしたファイル: {', '.join(safe(p) for p in skipped[:5])}", file=sys.stderr)
    return 0


def _cmd_externals(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    report = ExternalService().report(index)
    grouped = report.by_category()
    if args.format == "json":
        _emit_json(
            {
                cat: [
                    {"library": u.library, "owner": u.owner, "path": u.path, "line": u.line, "kind": u.kind, "confidence": u.confidence}
                    for u in uses
                ]
                for cat, uses in grouped.items()
            }
        )
        return 0
    print("外部システム・外部ライブラリとの接続（importと、名前解決できた呼び出しから分類）")
    for category in CATEGORY_LABELS:
        uses = grouped.get(category)
        if not uses or (args.category and category not in args.category):
            continue
        libraries = Counter(u.library.split(".")[0] if u.kind != "call" else u.library for u in uses)
        files = {u.path for u in uses}
        print(f"\n■ {CATEGORY_LABELS[category]}（{len(uses)}箇所、{len(files)}ファイル）")
        print("  主な外部名: " + ", ".join(f"{safe(name)}×{count}" for name, count in libraries.most_common(8)))
        for use in uses[: args.limit]:
            mark = "" if use.confidence == "confirmed" else "  [推定: メソッド名による]"
            print(f"  {safe(use.path)}:{use.line}  {safe(use.owner)}  {use.kind} {safe(use.library)}{mark}")
        if len(uses) > args.limit:
            print(f"  … ほか {len(uses) - args.limit}件（--limit / --category）")
    if not grouped:
        print("  確認できませんでした")
    _warn_stale(stale)
    print("※ 外部名に解決できたimport・呼び出しのみです。変数の型が不明なメソッド呼び出しは、名前からの推定のみ示します。", file=sys.stderr)
    return 0


def _group_uses(uses) -> list[tuple]:
    """同じ外部名の使用を1行にまとめる（外部名, カテゴリ, 操作, 推定か, 行のリスト）。"""

    grouped: dict[tuple, list[int]] = {}
    for use in uses:
        grouped.setdefault((use.library, use.category, use.operation, use.confidence != "confirmed"), []).append(use.line)
    return [(*key, sorted(set(lines))) for key, lines in grouped.items()]


def _lines_text(lines: list[int]) -> str:
    shown = ", ".join(f"L{n}" for n in lines[:4])
    return shown + (" …" if len(lines) > 4 else "")


_OPERATION_LABELS = {"read": "読み取り", "write": "書き込み", "effect": "外部呼び出し", "output": "出力", "io": "入出力", "call": "呼び出し"}


def _cmd_effects(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    navigation = NavigationService(repository)
    symbol = _resolve(args, navigation, index, args.name, project).symbol
    service = ExternalService()
    summary = service.effects(index, service.report(index), symbol, args.depth)
    print(f"{safe(symbol.qualified_name)} の副作用の候補（外部への入出力。読み取りだけの場合もあります）")
    print("\n■ この関数が直接行うもの")
    for library, category, operation, inferred, lines in _group_uses(summary.direct):
        mark = "  [推定]" if inferred else ""
        count = f" ×{len(lines)}" if len(lines) > 1 else ""
        print(f"  [{CATEGORY_LABELS[category]}・{_OPERATION_LABELS[operation]}] {safe(library)}{count}  {_lines_text(lines)}{mark}")
    if not summary.direct:
        print("  確認できませんでした")
    print(f"\n■ 呼び出し先（解決済み、深さ{args.depth}）を介して行うもの")
    seen_reach: set[tuple] = set()
    for use, route in summary.reachable:
        key = (use.library, route[-1])
        if key in seen_reach:
            continue
        seen_reach.add(key)
        print(f"  [{CATEGORY_LABELS[use.category]}・{_OPERATION_LABELS[use.operation]}] {safe(use.library)}  {safe(use.path)}:{use.line}")
        print(f"      経路: {' → '.join(safe(r) for r in route)}")
    if not summary.reachable:
        print("  確認できませんでした")
    if summary.unresolved_calls:
        print(f"※ 呼び出し先を特定できない呼び出しが {summary.unresolved_calls}件あり、そこから先の副作用は追えていません。", file=sys.stderr)
    print("※ 副作用の種類は、外部ライブラリ・システム関数の名前による分類です。", file=sys.stderr)
    _warn_stale(stale)
    return 0


def _cmd_architecture(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    externals = ExternalService().report(index)
    architecture = ArchitectureService().build(index, externals, args.depth)
    if args.format == "json":
        _emit_json(
            {
                "depth": architecture.depth,
                "layers": architecture.layers,
                "cycles": architecture.cycles,
                "components": {
                    n: {
                        "files": c.files, "definitions": c.definitions, "lines": c.lines, "role_hint": c.role,
                        "imports": dict(c.imports), "calls": dict(c.calls), "externals": dict(c.externals),
                    }
                    for n, c in architecture.components.items()
                },
                "layer_violations": [vars(v) for v in architecture.violations],
            }
        )
        return 0
    print(f"■ コンポーネント（ディレクトリ単位、深さ{architecture.depth}）  ※役割は名前からの推定")
    for name, c in sorted(architecture.components.items(), key=lambda kv: (-kv[1].level, kv[0])):
        role = f"  [{ROLE_LABELS[c.role]}・推定]" if c.role else ""
        print(f"  {safe(name)}  {c.files}ファイル / 定義{c.definitions} / {c.lines}行{role}")
        if c.depends_on:
            targets = ", ".join(
                f"{safe(t)}(import {c.imports[t]}, call {c.calls[t]})" for t in sorted(c.depends_on)
            )
            print(f"      依存先: {targets}")
        if c.externals:
            ext = ", ".join(f"{CATEGORY_LABELS[k]}×{v}" for k, v in c.externals.most_common(5))
            print(f"      外部連携: {ext}")
    print("\n■ 層構造（依存の向きから機械的に求めた段。上位ほど他に依存する側、下位は依存される側）")
    for number, group in enumerate(architecture.layers):
        level = len(architecture.layers) - 1 - number
        print(f"  段{level}: " + ", ".join(safe(g) for g in group))
    print("\n■ 循環する依存（コンポーネント間）")
    for cycle in architecture.cycles:
        print("  " + " <-> ".join(safe(c) for c in cycle))
    if not architecture.cycles:
        print("  確認できませんでした")
    if architecture.violations:
        print("\n■ 層の順に反する可能性のある依存（候補。役割が名前からの推定のため）")
        for v in architecture.violations:
            print(
                f"  {safe(v.source)}[{ROLE_LABELS[v.source_role]}] → {safe(v.target)}[{ROLE_LABELS[v.target_role]}]"
                f"  (import {v.imports}, call {v.calls})"
            )
    _warn_stale(stale)
    print("※ 層の段数は依存の向きから求めた事実に基づきます。役割名（プレゼンテーション層等）は名前による推定です。", file=sys.stderr)
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    items, skipped = ConfigService().scan(project, index)
    if args.kind:
        items = [i for i in items if i.kind in args.kind]
    if args.name:
        items = [i for i in items if args.name.lower() in i.name.lower()]
    if args.format == "json":
        _emit_json(
            [
                {
                    "kind": i.kind, "name": i.name, "path": i.path, "line": i.line, "owner": i.owner,
                    "default": i.default, "help": i.help, "detail": i.detail, "confidence": i.confidence,
                    "uses": [vars(u) for u in i.uses],
                }
                for i in items
            ]
        )
        return 0
    print("設定値の定義箇所と、使われる箇所（文字列リテラルで書かれた名前のみ。使用箇所は静的な近似）")
    for kind, label in CONFIG_LABELS.items():
        group = [i for i in items if i.kind == kind]
        if not group:
            continue
        print(f"\n■ {label}（{len(group)}件）")
        for item in group:
            extras = []
            if item.default:
                extras.append(f"既定値: {item.default}")
            if item.detail:
                extras.append(item.detail)
            if item.help:
                extras.append(f"説明: {item.help}")
            if item.confidence != "confirmed":
                extras.append("推定")
            print(f"  {safe(item.name)}  {safe(item.path)}:{item.line}  in {safe(item.owner)}" + (f"  [{' / '.join(safe(e) for e in extras)}]" if extras else ""))
            for use in item.uses[: args.limit]:
                print(f"      使われる箇所: {safe(use.path)}:{use.line}  {safe(use.owner)}  ({safe(use.how)})")
            if len(item.uses) > args.limit:
                print(f"      … ほか {len(item.uses) - args.limit}件（--limit）")
            if not item.uses and kind in ("env", "cli_option", "constant"):
                print("      使用箇所を確認できません（静的に追えない使い方か、未使用の可能性）")
    if not items:
        print("  確認できませんでした")
    if skipped:
        print(f"解析後に変更された等で対象外にしたファイル: {', '.join(safe(p) for p in skipped[:5])}", file=sys.stderr)
    _warn_stale(stale)
    return 0


def _cmd_boundaries(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    items, skipped = BoundaryService().scan(project, index)
    if args.kind:
        items = [i for i in items if i.kind in args.kind]
    if args.format == "json":
        _emit_json(
            [
                {
                    "kind": i.kind, "label": i.label, "path": i.path, "line": i.line, "owner": i.owner,
                    "target": i.target.qualified_name if i.target else None, "detail": i.detail, "confidence": i.confidence,
                }
                for i in items
            ]
        )
        return 0
    print("プログラムの入口と境界（フレームワークの規約・構文パターンから検出）")
    for kind, label in BOUNDARY_LABELS.items():
        group = [i for i in items if i.kind == kind]
        if not group:
            continue
        print(f"\n■ {label}（{len(group)}件）")
        for item in group[: args.limit]:
            target = f"  → {safe(item.target.qualified_name)}" if item.target else ""
            mark = "  [推定]" if item.confidence != "confirmed" else ""
            detail = f"  ({safe(item.detail)})" if item.detail else ""
            print(f"  {safe(item.label)}  {safe(item.path)}:{item.line}  in {safe(item.owner)}{target}{detail}{mark}")
        if len(group) > args.limit:
            print(f"  … ほか {len(group) - args.limit}件（--limit / --kind）")
    if not items:
        print("  確認できませんでした")
    if skipped:
        print(f"解析後に変更された等で対象外にしたファイル: {', '.join(safe(p) for p in skipped[:5])}", file=sys.stderr)
    _warn_stale(stale)
    print("※ フレームワークの規約に基づく検出です。独自の登録方法・動的な登録は検出できません。", file=sys.stderr)
    return 0


def _entry_ids(project, index) -> set[str]:
    items, _ = BoundaryService().scan(project, index)
    return {i.target.symbol_id for i in items if i.target and i.kind in ("entry", "cli", "http", "event", "thread")}


def _cmd_history(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    service = HistoryService()
    if args.name:
        navigation = NavigationService(repository)
        symbol = _resolve(args, navigation, index, args.name, project).symbol
        history = service.symbol_history(project, index, symbol, args.limit)
        if not history.available:
            raise CliError("Gitの履歴を取得できませんでした（Gitリポジトリではない、または未コミットのファイル）。")
        if args.format == "json":
            _emit_json({"symbol": symbol.qualified_name, "modified_in_working_tree": history.modified_in_working_tree,
                        "commits": [vars(c) for c in history.commits]})
            return 0
        print(f"{safe(symbol.qualified_name)} の変更履歴（{safe(history.path)}:{symbol.start_line}-{symbol.end_line} に触れたコミット、新しい順）")
        for commit in history.commits:
            print(f"  {commit.date}  {commit.hash[:8]}  {safe(commit.author)}  {safe(commit.subject)}")
        if not history.commits:
            print("  該当するコミットがありません（未コミット、または履歴を追えませんでした）")
        if history.modified_in_working_tree:
            print("警告: このファイルには未コミットの変更があり、行範囲と履歴の対応がずれている可能性があります。", file=sys.stderr)
        print("※ 履歴は「なぜその実装か」の手がかりです。理由そのものはコードからは確認できません。", file=sys.stderr)
        return 0
    report = service.report(project, index, args.max_commits)
    if not report.available:
        raise CliError("Gitの履歴を取得できませんでした（Gitリポジトリではありません）。")
    if args.format == "json":
        _emit_json({"commits_examined": report.commits_examined,
                    "churn": [vars(c) for c in report.churn[: args.limit]],
                    "coupling": [{"a": a, "b": b, "commits": n} for a, b, n in report.coupling[: args.limit]]})
        return 0
    print(f"変更頻度の高いファイル（直近{report.commits_examined}コミット。コミット数×行数が大きい順）")
    for churn in report.churn[: args.limit]:
        print(f"  {churn.commits:>4}回 / {churn.authors}人 / {churn.lines:>5}行  {churn.first_date}〜{churn.last_date}  {safe(churn.path)}")
    print("\n同時に変更されることが多いファイルの組（3コミット以上）")
    for a, b, n in report.coupling[: args.limit]:
        print(f"  {n:>3}回  {safe(a)}  ⇄  {safe(b)}")
    if not report.coupling:
        print("  確認できませんでした")
    return 0


def _cmd_tests(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    service = TestMapService()
    if args.untested:
        symbols = service.untested(index, args.depth, args.min_lines)
        print(f"どのテストからも（深さ{args.depth}以内で）静的に届かない関数・クラス: {len(symbols)}件")
        for symbol in symbols[: args.limit]:
            print(f"  {safe(index.path_of(symbol.file_id))}:{symbol.start_line}-{symbol.end_line}\t{symbol.kind.value}\t{safe(symbol.qualified_name)}")
        if len(symbols) > args.limit:
            print(f"  … ほか {len(symbols) - args.limit}件（--limit）")
        print("※ 静的に届くかどうかです。実際の実行・検証（カバレッジ）は確認していません。", file=sys.stderr)
        return 0
    if not args.name:
        raise CliError("シンボル名か --untested を指定してください。")
    navigation = NavigationService(repository)
    symbol = _resolve(args, navigation, index, args.name, project).symbol
    mapping = service.tests_for(index, symbol, args.depth)
    if args.format == "json":
        _emit_json({"symbol": symbol.qualified_name,
                    "tests": [{"test": r.test.qualified_name, "path": r.path, "line": r.line, "route": r.route, "direct": r.direct} for r in mapping.reaches],
                    "test_files_importing": mapping.test_files_importing})
        return 0
    print(f"{safe(symbol.qualified_name)} に届くテスト（静的な呼び出し・参照の連鎖。深さ{args.depth}以内）")
    direct = [r for r in mapping.reaches if r.direct]
    indirect = [r for r in mapping.reaches if not r.direct]
    print(f"\n■ 直接参照するテスト（{len(direct)}件）")
    for r in direct:
        print(f"  {safe(r.path)}:{r.line}  {safe(r.test.qualified_name)}")
    print(f"\n■ 他の関数を介して届くテスト（{len(indirect)}件）")
    for r in indirect[: args.limit]:
        print(f"  {safe(r.path)}:{r.line}  {safe(r.test.qualified_name)}  経路: {' → '.join(safe(x) for x in r.route)}")
    if mapping.test_files_importing:
        print(f"\n■ このファイルをimportしているテストファイル: {', '.join(safe(f) for f in mapping.test_files_importing)}")
    if not mapping.reaches:
        print("\n  静的に届くテストは確認できませんでした（動的な呼び出しや、公開APIの外部テストは含みません）")
    print("※ 静的に届くことを示すだけで、実際に検証されている（カバレッジ）ことは意味しません。", file=sys.stderr)
    return 0


def _cmd_impact(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    navigation = NavigationService(repository)
    symbol = _resolve(args, navigation, index, args.name, project).symbol
    report = ImpactService().impact(index, symbol, args.depth, _entry_ids(project, index))
    if args.format == "json":
        _emit_json({"symbol": symbol.qualified_name, "affected": [{"symbol": a.symbol.qualified_name, "path": a.path, "distance": a.distance,
                                                                    "route": a.route, "test": a.is_test} for a in report.affected],
                    "files": sorted(report.files), "components": sorted(report.components),
                    "entry_points": [a.symbol.qualified_name for a in report.entry_points]})
        return 0
    production = [a for a in report.affected if not a.is_test]
    tests = [a for a in report.affected if a.is_test]
    print(f"{safe(symbol.qualified_name)} を変更したときに影響を受ける範囲（呼び出し・参照・継承を逆にたどる。深さ{args.depth}）")
    print(f"  影響を受ける関数・クラス: {len(production)}件 / ファイル {len({a.path for a in production})} / コンポーネント {len(report.components)} / 関連するテスト {len(tests)}件")
    for a in production[: args.limit]:
        print(f"  距離{a.distance}  {safe(a.path)}  {safe(a.symbol.qualified_name)}")
        if a.distance > 1:
            print(f"        経路: {' → '.join(safe(x) for x in a.route)}")
    if len(production) > args.limit:
        print(f"  … ほか {len(production) - args.limit}件（--limit / --depth）")
    print("\n■ 入口（エントリポイント・CLI・HTTP・イベント等）に届くか")
    for a in report.entry_points:
        print(f"  {safe(a.path)}  {safe(a.symbol.qualified_name)}  経路: {' → '.join(safe(x) for x in a.route)}")
    if not report.entry_points:
        print("  静的に確認できる入口には届きません（動的な呼び出し・公開APIとしての利用は含みません）")
    if report.unresolved_callers:
        print(f"\n※ 同じ名前を呼ぶが解決できていない呼び出しが {report.unresolved_callers}件あり、影響を追えていません。", file=sys.stderr)
    return 0


def _cmd_unused(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    candidates = UnusedService().candidates(index, _entry_ids(project, index))
    order = ("high", "medium", "low")
    candidates = [c for c in candidates if order.index(c.confidence) <= order.index(args.min_confidence)]
    if args.format == "json":
        _emit_json([{"symbol": c.symbol.qualified_name, "kind": c.symbol.kind.value, "path": c.path, "line": c.symbol.start_line,
                     "confidence": c.confidence, "reason": c.reason} for c in candidates])
        return 0
    labels = {"high": "確度 高", "medium": "確度 中", "low": "確度 低"}
    print(f"どこからも参照されていないシンボルの候補 {len(candidates)}件（削除してよいという意味ではありません）")
    for confidence in order:
        group = [c for c in candidates if c.confidence == confidence]
        if not group:
            continue
        print(f"\n■ {labels[confidence]}（{len(group)}件）")
        for c in group[: args.limit]:
            print(f"  {safe(c.path)}:{c.symbol.start_line}\t{c.symbol.kind.value}\t{safe(c.symbol.qualified_name)}\t# {safe(c.reason)}")
        if len(group) > args.limit:
            print(f"  … ほか {len(group) - args.limit}件（--limit）")
    _warn_stale(stale)
    print("※ 動的な呼び出し・リフレクション・外部から利用される公開API・フレームワークによる登録は静的には見えません。", file=sys.stderr)
    return 0


def _print_section(title: str) -> None:
    print(f"\n━━ {title}")


def _cmd_understand(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    navigation = NavigationService(repository)
    symbol = _resolve(args, navigation, index, args.name, project).symbol
    try:
        u = UnderstandService(navigation).understand(project, index, symbol, args.depth)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    limit = args.limit
    print(f"{safe(symbol.qualified_name)}  ({symbol.kind.value}, {safe(u.path)}:{symbol.start_line}-{symbol.end_line})")
    for number, text in u.declaration[:6]:
        print(f"  {number:>5} | {safe(text)}")

    _print_section("1. なぜ存在するのか（確認できる手がかり。理由そのものはコードからは確認できません）")
    print(f"  docstring: {safe(u.summary) if u.summary else '（なし）'}")
    if u.module_summary:
        print(f"  モジュールの説明: {safe(u.module_summary)}")
    components = len(u.impact.components) if u.impact else 0
    print(f"  使われ方: 呼び出し元 {u.caller_total}件、影響範囲は {components} コンポーネント")
    if u.test_names:
        print(f"  テストでの扱い（テスト名は意図の手がかり）: {', '.join(safe(n.rsplit('.', 1)[-1]) for n in u.test_names)}")
    for path, number, text in u.doc_mentions:
        print(f"  文書での言及: {safe(path)}:{number}  {safe(text)}")
    for number, text in u.todo_comments:
        print(f"  未対応のコメント: L{number} {safe(text)}")
    if u.history and u.history.introduced:
        c = u.history.introduced
        print(f"  最初に導入されたコミット: {c.date}  {c.hash[:8]}  {safe(c.subject)}")

    _print_section("2. 誰が呼ぶのか")
    print(f"  呼び出し元 {u.caller_total}件（解決済みの呼び出しのみ）")
    for hit in u.callers[:limit]:
        print(f"    {safe(hit.location)}  {safe(hit.source.qualified_name)}")
    if u.caller_total > limit:
        print(f"    … ほか {u.caller_total - limit}件（callers コマンド）")
    for item in u.registrations:
        print(f"  登録元: {safe(item.label)}  {safe(item.path)}:{item.line}")
    if u.impact and u.impact.entry_points:
        for a in u.impact.entry_points[:3]:
            print(f"  入口からの到達: {' → '.join(safe(x) for x in a.route)}")

    _print_section("3. 何を入力するのか")
    if u.parameters:
        for p in u.parameters:
            ann = f": {safe(p.annotation)}" if p.annotation else ""
            default = f" = {safe(p.default)}" if p.default else ""
            kind = "" if p.kind == "positional" else f"  [{p.kind}]"
            passed = u.caller_arguments.get(p.name)
            actual = f"   ← 呼び出し元が渡す値: {', '.join(safe(a) for a in passed[:4])}" if passed else ""
            print(f"  引数 {safe(p.name)}{ann}{default}{kind}{actual}")
    elif u.language == Language.PYTHON and symbol.kind.value in ("function", "method"):
        print("  引数なし")
    for item in u.config_reads:
        default = f" 既定値 {safe(item.default)}" if item.default else ""
        print(f"  設定値: {CONFIG_LABELS[item.kind]} {safe(item.name)}{default}  ({safe(item.path)}:{item.line})")
    for library, category, operation, inferred, lines in _group_uses(u.external_inputs)[:limit]:
        print(f"  外部からの入力の可能性（直接）: [{CATEGORY_LABELS[category]}・{_OPERATION_LABELS[operation]}] {safe(library)}  {_lines_text(lines)}")
    if u.effects:
        seen_reads: set[tuple] = set()
        for use, route in u.effects.reads_reachable:
            key = (use.library, route[-1])
            if key in seen_reads:
                continue
            seen_reads.add(key)
            print(f"  外部からの入力の可能性（呼び出し先経由）: [{CATEGORY_LABELS[use.category]}・{_OPERATION_LABELS[use.operation]}] {safe(use.library)}  経路: {' → '.join(safe(r) for r in route)}")

    _print_section("4. 何を変更するのか")
    for text in u.state_changes:
        print(f"  状態: {safe(text)}")
    for text in u.parameter_mutations:
        print(f"  引数: {safe(text)}")
    if u.effects:
        for library, category, operation, inferred, lines in _group_uses(u.effects.direct):
            count = f" ×{len(lines)}" if len(lines) > 1 else ""
            print(f"  外部への副作用の候補（直接）: [{CATEGORY_LABELS[category]}・{_OPERATION_LABELS[operation]}] {safe(library)}{count}  {_lines_text(lines)}")
        seen = set()
        for use, route in u.effects.reachable:
            key = (use.category, use.library, route[-1])
            if key in seen:
                continue
            seen.add(key)
            print(f"  外部への副作用の候補（呼び出し先経由）: [{CATEGORY_LABELS[use.category]}・{_OPERATION_LABELS[use.operation]}] {safe(use.library)}  経路: {' → '.join(safe(r) for r in route)}")
    if not (u.state_changes or u.parameter_mutations or (u.effects and (u.effects.direct or u.effects.reachable))):
        print("  確認できる状態変更・引数の変更・外部への副作用はありません（静的に追える範囲）")

    _print_section("5. 何を返すのか")
    if u.return_annotation:
        print(f"  戻り値の型注釈: {safe(u.return_annotation)}")
    for number, text in u.returns[:limit]:
        print(f"  L{number} return {safe(text)}")
    if u.is_generator:
        print("  ジェネレータ（yield で値を順次返す）")
    if u.is_async:
        print("  async 関数（awaitable を返す）")
    if not u.returns and not u.is_generator and u.language == Language.PYTHON and symbol.kind.value in ("function", "method"):
        print("  明示的な return はありません（None を返す）")

    _print_section("6. 誰に影響するのか")
    if u.impact:
        production = [a for a in u.impact.affected if not a.is_test]
        print(f"  影響を受ける関数・クラス {len(production)}件 / ファイル {len({a.path for a in production})} / コンポーネント {len(u.impact.components)}")
        for a in production[: min(limit, 5)]:
            print(f"    距離{a.distance}  {safe(a.symbol.qualified_name)}")
    if u.tests:
        direct = [r for r in u.tests.reaches if r.direct]
        print(f"  届くテスト: 直接 {len(direct)}件、間接 {len(u.tests.reaches) - len(direct)}件（静的な呼び出しの連鎖）")

    _print_section("7. 失敗するとどうなるのか")
    if u.exceptions:
        for e in sorted(u.exceptions.propagated, key=lambda x: (x.exception, x.raised_line)):
            caught, total = u.caught_by_callers.get(e.exception, (0, 0))
            where = f"{safe(e.raised_in.qualified_name)}:L{e.raised_line}"
            via = f"（呼び出し先 {safe(e.chain[0][0].qualified_name)} 経由）" if e.chain else ""
            print(f"  外へ出うる例外: {safe(e.exception)}  raise {where}{via}  — 呼び出し箇所 {total} 件中 {caught} 件が捕捉")
        if not u.exceptions.propagated:
            print("  外へ出うる明示的な例外は確認できません（組み込み・外部ライブラリの例外は含まない）")
        for h in u.exceptions.handlers:
            traits = "握りつぶし" if h.swallowed else ("再送出あり" if h.reraises else "処理あり")
            print(f"  関数内の例外処理: L{h.line} except {safe(', '.join(h.types))} — {traits}")
        if u.exceptions.unresolved_calls:
            print(f"  ※ 呼び出し先を特定できない呼び出しが {u.exceptions.unresolved_calls}件あり、そこからの例外は追えていません")
    labels = {"retry": "リトライ", "timeout": "タイムアウト指定", "sleep": "待機"}
    for hint in u.resilience:
        print(f"  {labels[hint.kind]}の手がかり: L{hint.line} {safe(hint.detail)}")
    for finding in u.risks:
        print(f"  リスクの手がかり: L{finding.line} [{finding.severity}] {finding.rule} — {safe(finding.message)}")

    _print_section("8. なぜ現在の実装になっているのか（履歴の手がかり）")
    if u.history and u.history.available:
        for commit in u.history.commits[: min(limit, 6)]:
            print(f"  {commit.date}  {commit.hash[:8]}  {safe(commit.author)}  {safe(commit.subject)}")
        if not u.history.commits:
            print("  この範囲に触れたコミットは確認できません（未コミットの可能性）")
        if u.history.modified_in_working_tree:
            print("  ※ 未コミットの変更があり、履歴と行範囲の対応がずれている可能性があります")
    else:
        print("  Gitの履歴を取得できません")
    print("  ※ 設計判断の理由は、コミットメッセージ・Issue・設計資料にあります。コードと履歴からは推測しません。")

    if u.limitations:
        _print_section("確認できなかったこと・対象外")
        for text in u.limitations:
            print(f"  ・{safe(text)}")
    _warn_stale(stale)
    return 0


def _cmd_environment(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    report = EnvironmentService().report(project, index)
    if args.format == "json":
        _emit_json({
            "python_requirement": report.python_requirement, "declared": report.declared,
            "used_external": dict(report.used_external), "undeclared": report.undeclared,
            "unused_declared": report.unused_declared, "frameworks": report.frameworks,
            "platform_checks": [vars(x) for x in report.platform_checks],
            "executables": [vars(x) for x in report.executables], "c_system_headers": report.c_system_headers,
        })
        return 0
    print("実行環境の前提（pyproject/requirements・import・OS判定・外部コマンドから）")
    print(f"\n■ 言語のバージョン\n  Python: {safe(report.python_requirement) or '宣言なし（pyproject.tomlのrequires-python）'}")
    print(f"\n■ 宣言されている依存（{len(report.declared)}件）")
    for name, origin in report.declared.items():
        print(f"  {safe(name)}  ({safe(origin)})")
    print(f"\n■ 使われている外部ライブラリ（importされる外部のトップレベル名、{len(report.used_external)}件）")
    print("  " + (", ".join(f"{safe(n)}×{c}" for n, c in report.used_external.most_common()) or "なし"))
    if report.frameworks:
        print("\n■ フレームワーク・ライブラリの種類")
        for label, names in report.frameworks.items():
            print(f"  {label}: {', '.join(safe(n) for n in names)}")
    if report.undeclared:
        print(f"\n■ 使われているが、宣言に見つからない（候補）: {', '.join(safe(n) for n in report.undeclared)}")
        print("  ※ パッケージ名とimport名が異なる場合（例: beautifulsoup4 と bs4）も、ここに出ます。")
    if report.unused_declared:
        print(f"\n■ 宣言されているが、importが見つからない（候補）: {', '.join(safe(n) for n in report.unused_declared)}")
    print(f"\n■ OS・実行環境による分岐（{len(report.platform_checks)}件）")
    for site in report.platform_checks[: args.limit]:
        print(f"  {safe(site.path)}:{site.line}  {safe(site.detail)}")
    print(f"\n■ 起動する外部コマンド（{len(report.executables)}件）")
    for site in report.executables[: args.limit]:
        print(f"  {safe(site.path)}:{site.line}  {safe(site.detail)}")
    if report.c_system_headers:
        print(f"\n■ Cのシステムヘッダー: {', '.join(safe(h) for h in report.c_system_headers)}")
    _warn_stale(stale)
    print("※ 実行はしていません。静的に確認できる前提のみです（ライブラリの実際のバージョンやOSの設定は確認できません）。", file=sys.stderr)
    return 0


def _cmd_docs_check(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    report = SpecCheckService().check(project, index)
    if args.format == "json":
        _emit_json({"documents": report.documents, "checked": report.checked,
                    "missing_in_code": [vars(m) for m in report.missing_in_code],
                    "undocumented_options": report.undocumented_options, "undocumented_env": report.undocumented_env})
        return 0
    print(f"文書 {report.documents}件のバッククォート内の識別子・オプション・環境変数 {report.checked}件を、実装と照合（文字列の一致のみ）")
    labels = {"identifier": "識別子", "option": "オプション", "env": "環境変数"}
    print(f"\n■ 文書に書かれているが、実装で見つからない（{len(report.missing_in_code)}件。文書が古い、または概念的な用語の可能性）")
    for m in report.missing_in_code[: args.limit]:
        print(f"  {safe(m.path)}:{m.line}  [{labels[m.kind]}] {safe(m.token)}")
    print(f"\n■ 実装にあるが、文書に書かれていないオプション（{len(report.undocumented_options)}件）")
    for name, path, line in report.undocumented_options[: args.limit]:
        print(f"  {safe(path)}:{line}  {safe(name)}")
    print(f"\n■ 実装にあるが、文書に書かれていない環境変数（{len(report.undocumented_env)}件）")
    for name, path, line in report.undocumented_env[: args.limit]:
        print(f"  {safe(path)}:{line}  {safe(name)}")
    _warn_stale(stale)
    print("※ 仕様の意味は理解せず、名前の一致だけを見ています。結果は、確認すべき箇所の手がかりです。", file=sys.stderr)
    return 0


def _cmd_unresolved(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    navigation = NavigationService(repository)
    references = navigation.unresolved_references(index)
    dependencies = [
        h
        for h in navigation.dependencies(index)
        if h.dependency.resolution_status in (ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS)
    ]
    _warn_stale(stale)
    if args.format == "json":
        _emit_json(
            {
                "references": [_reference_dict(h) for h in references],
                "dependencies": [_dependency_dict(h) for h in dependencies],
            }
        )
        return 0
    print(f"未解決の参照 {len(references)}件 / 未解決の依存関係 {len(dependencies)}件（理由別）")
    groups: dict[str, list[str]] = defaultdict(list)
    for hit in references:
        groups[hit.reference.note or "（理由なし）"].append(_format_reference(hit, None).split("\t[", 1)[0])
    for dep in dependencies:
        d = dep.dependency
        groups[d.note or "（理由なし）"].append(
            f"{safe(dep.location)}\t{d.dependency_kind.value}\t{safe(dep.source_path)} -> {safe(d.target_name)}"
        )
    for note, lines in sorted(groups.items(), key=lambda item: -len(item[1])):
        print(f"\n■ {safe(note)}（{len(lines)}件）")
        shown = lines if args.all else lines[:args.limit]
        for line in shown:
            print(f"  {line}")
        if len(lines) > len(shown):
            print(f"  … ほか {len(lines) - len(shown)}件（--all で全て表示）")
    return 0


def _build_graph(
    args: argparse.Namespace, index: ProjectIndex, navigation: NavigationService, project: Project
) -> GraphModel:
    builder = GraphBuilder(index)
    traversal = Traversal(args.direction)
    if args.kind == "call":
        root = _resolve(args, navigation, index, args.root, project).symbol if args.root else None
        return builder.call_graph(
            root,
            args.depth,
            traversal,
            include_unresolved=not args.no_unresolved,
            include_external=args.external,
        )
    if args.kind == "inherit":
        root = _resolve(args, navigation, index, args.root, project).symbol if args.root else None
        return builder.inheritance_graph(root, args.depth, traversal)
    if args.kind == "arch":
        architecture = ArchitectureService().build(index, ExternalService().report(index), args.depth)
        return builder.architecture_graph(architecture)
    if args.kind == "flow":
        if not args.root:
            raise CliError("graph flow には --root で関数・メソッド名を指定してください。")
        symbol = _resolve(args, navigation, index, args.root, project).symbol
        try:
            function = FlowService(navigation).function_ast(project, index, symbol)
        except FlowAnalysisError as exc:
            raise CliError(str(exc)) from exc
        return CfgBuilder().build(function, index.path_of(symbol.file_id), symbol.qualified_name)
    try:
        return builder.file_dependency_graph(
            args.root,
            args.depth,
            traversal,
            include_external=args.external,
            include_unresolved=not args.no_unresolved,
        )
    except LookupError as exc:
        raise CliError(f"ファイルが見つかりません: {safe(args.root)}", 2) from exc


def _cmd_graph(args: argparse.Namespace) -> int:
    repository, project, index, stale = _prepare(args)
    model = _build_graph(args, index, NavigationService(repository), project)
    model.meta.update(
        {
            "project": project.name,
            "repository_revision": project.repository_revision,
            "stale_files": stale,
        }
    )
    if stale:
        model.notes.append(f"解析後に変更されたファイルがあります（{len(stale)}件）。内容が古い可能性があります。")
    _warn_stale(stale)
    if len(model.nodes) > 200 and not args.root:
        print(
            f"ヒント: ノードが{len(model.nodes)}個あり、読み取りにくい可能性があります。"
            "--root と --depth、または --exclude で絞り込めます。",
            file=sys.stderr,
        )
    renderers = {"mermaid": to_mermaid, "dot": to_dot, "json": to_json, "html": render_html}
    output = renderers[args.format](model)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"出力しました: {args.output}（ノード {len(model.nodes)} / 辺 {len(model.edges)}）", file=sys.stderr)
    else:
        print(output, end="")
    return 0


# --- パーサー ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codeinsight",
        description="C言語・Pythonを中心としたコードリーディング支援ソフトウェア",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser, formats: tuple[str, ...] = ("text", "json")) -> None:
        sub.add_argument("--db", help="解析結果DBのパス（既定: ~/.codeinsight/codeinsight.db）")
        sub.add_argument("--project", help="プロジェクトのID・名前・ルートパス（登録が1件なら省略可）")
        sub.add_argument("--format", choices=formats, default=formats[0], help="出力形式")

    def add(
        name: str,
        help_text: str,
        func,
        formats: tuple[str, ...] = ("text", "json"),
        exclude: bool = True,
    ):
        sub = subparsers.add_parser(name, help=help_text)
        common(sub, formats)
        if exclude:
            sub.add_argument(
                "--exclude",
                action="append",
                metavar="GLOB",
                help="結果から除くファイルのパターン（例: 'tests/*'。複数指定可）",
            )
        sub.set_defaults(func=func)
        return sub

    analyze = subparsers.add_parser("analyze", help="プロジェクトを走査・解析し、結果を保存する")
    analyze.add_argument("path", help="解析対象ディレクトリ")
    analyze.add_argument("--db", help="解析結果DBのパス（既定: ~/.codeinsight/codeinsight.db）")
    analyze.add_argument("--compile-commands", help="compile_commands.jsonを含むディレクトリ（既定: 解析対象ルート）")
    analyze.add_argument("--format", choices=("text", "json"), default="text")
    analyze.set_defaults(func=_cmd_analyze)

    add("status", "解析状況と、ソース変更による古さを表示する", _cmd_status, exclude=False)

    symbols = add("symbols", "保存済みのシンボルを一覧表示する", _cmd_symbols)
    symbols.add_argument("--file", help="相対パスで絞り込む")
    symbols.add_argument("--kind", action="append", choices=[k.value for k in SymbolKind], help="種類で絞り込む")

    tree = add("tree", "ディレクトリ・ファイル・シンボルの階層を表示する", _cmd_tree)
    tree.add_argument("path", nargs="?", help="表示を絞るディレクトリまたはファイルの相対パス（前方一致）")
    tree.add_argument("--locals", action="store_true", help="ローカル変数も表示する")
    tree.add_argument("--no-variables", action="store_true", help="変数（グローバル/クラス/static）を表示しない")
    tree.add_argument("--depth", type=int, help="ファイルの下に表示するシンボルの階層数（1ならトップレベルのみ）")

    search = add("search", "シンボル・ファイル名・テキストを検索する", _cmd_search)
    search.add_argument("query")
    search.add_argument("--files", action="store_true", help="ファイル名を検索する")
    search.add_argument("--text", action="store_true", help="テキスト全文検索（構文解析ではなく文字列一致）")
    search.add_argument("--regex", action="store_true", help="--text で正規表現を使う")
    search.add_argument("-i", "--ignore-case", action="store_true", help="--text で大文字小文字を区別しない")
    search.add_argument("--match", choices=[m.value for m in MatchMode], default="substring")
    search.add_argument("--kind", action="append", choices=[k.value for k in SymbolKind])
    search.add_argument("--file", help="シンボル検索を指定ファイルに限定する")
    search.add_argument("--limit", type=int)

    definition = add("def", "シンボルの定義箇所を表示する", _cmd_def)
    definition.add_argument("name", help="名前または修飾名")
    definition.add_argument("--file")

    for name, help_text, mode in (
        ("refs", "シンボルを参照している箇所を表示する", "refs"),
        ("callers", "呼び出し元の一覧を表示する", "callers"),
        ("callees", "呼び出し先の一覧を表示する（未解決・外部を含む）", "callees"),
    ):
        sub = add(name, help_text, lambda a, m=mode: _reference_command(a, m))
        sub.add_argument("name", help="名前または修飾名")
        sub.add_argument("--file")
        if mode == "callees":
            sub.add_argument("--hide-external", action="store_true", help="外部（標準/外部ライブラリ等）の呼び出しを除く")

    trace = add("trace", "呼び出し階層を表示する（再帰・未解決を明示）", _cmd_trace, ("text",))
    trace.add_argument("name")
    trace.add_argument("--direction", choices=("callees", "callers"), default="callees")
    trace.add_argument("--depth", type=int, default=3)
    trace.add_argument("--external", action="store_true", help="外部（標準/外部ライブラリ等）の呼び出しも表示する")
    trace.add_argument("--file")

    path = add("path", "2つの関数の間の呼び出し経路を検索する", _cmd_path)
    path.add_argument("source")
    path.add_argument("target")
    path.add_argument("--max-depth", type=int, default=8)
    path.add_argument("--limit", type=int, default=20)
    path.add_argument("--file")

    deps = add("deps", "ファイル間の依存関係(include/import)を表示する", _cmd_deps)
    deps.add_argument("file", nargs="?", help="相対パス（省略時は全体）")
    deps.add_argument("--dependents", action="store_true", help="指定ファイルに依存している側を表示する")
    deps.add_argument("--cycles", action="store_true", help="循環する依存関係を表示する")
    deps.add_argument("--external", action="store_true", help="プロジェクト外への依存も表示する")

    show = add("show", "ソースコードを行番号付きで表示する（ファイル位置またはシンボル名）", _cmd_show, ("text",))
    show.add_argument("location", help="FILE[:LINE[-END]] またはシンボル名（定義全体を表示）")
    show.add_argument("--context", type=int, default=3)
    show.add_argument("--file", help="シンボル名が複数に一致する場合の絞り込み")

    describe = add("describe", "シンボルの詳細（宣言・要約・メンバ・呼び出し/参照の件数）を表示する", _cmd_describe)
    describe.add_argument("name", help="名前または修飾名")
    describe.add_argument("--file")
    describe.add_argument("--members", type=int, default=30, help="表示するメンバ数の上限")

    overview = add("overview", "リポジトリの全体像（言語・主要モジュール・エントリポイント・中心となる関数）", _cmd_overview)
    overview.add_argument("--top", type=int, default=10, help="各ランキングの表示件数")

    flow = add("flow", "関数の制御構造（分岐・ループ・例外処理・return）と、リトライ/タイムアウトの手がかりを表示する", _cmd_flow)
    flow.add_argument("name", help="関数・メソッドの名前または修飾名")
    flow.add_argument("--file")

    dataflow = add("dataflow", "変数の定義・使用と、値の行き先（代入・呼び出し引数・戻り値・状態）をたどる", _cmd_dataflow, ("text",))
    dataflow.add_argument("name", help="関数・メソッドの名前または修飾名")
    dataflow.add_argument("variable", nargs="?", help="変数名（省略時は変数の一覧）")
    dataflow.add_argument("--depth", type=int, default=2, help="呼び出し先・代入先をたどる深さ")
    dataflow.add_argument("--upstream", action="store_true", help="引数に渡される実引数を、呼び出し元から調べる")
    dataflow.add_argument("--file")

    state = add("state", "クラスの属性（self.<属性>）・モジュール変数の書き込み/変更/読み取りを表示する", _cmd_state)
    state.add_argument("name", help="クラスまたはモジュールの名前または修飾名")
    state.add_argument("--file")

    exceptions = add("exceptions", "関数から出うる例外（明示的なraiseと解決済みの呼び出しをたどる）と握りつぶしを表示する", _cmd_exceptions)
    exceptions.add_argument("name", help="関数・メソッドの名前または修飾名")
    exceptions.add_argument("--depth", type=int, default=4)
    exceptions.add_argument("--file")

    risks = add("risks", "潜在的な問題の手がかり（例外の握りつぶし・eval・shell=True等）を探す", _cmd_risks)
    risks.add_argument("--rule", action="append", choices=sorted(RISK_RULES), help="規則で絞り込む")
    risks.add_argument("--min-severity", choices=("low", "medium", "high"), default="low")
    risks.add_argument("--limit", type=int, default=10, help="規則ごとの表示件数")

    externals = add("externals", "外部システム・外部ライブラリとの接続（ネットワーク・DB・ファイル・プロセス等）を分類して表示する", _cmd_externals)
    externals.add_argument("--category", action="append", choices=list(CATEGORY_LABELS), help="カテゴリで絞り込む")
    externals.add_argument("--limit", type=int, default=8, help="カテゴリごとの表示件数")

    effects = add("effects", "関数の副作用の候補（外部への入出力）を、直接と呼び出し先を介したものに分けて表示する", _cmd_effects, ("text",))
    effects.add_argument("name", help="関数・メソッドの名前または修飾名")
    effects.add_argument("--depth", type=int, default=3)
    effects.add_argument("--file")

    architecture = add("architecture", "コンポーネント構成・層構造・循環・外部連携を表示する（役割は名前による推定）", _cmd_architecture)
    architecture.add_argument("--depth", type=int, help="コンポーネントとするディレクトリの深さ（省略時は自動）")

    config = add("config", "設定値（環境変数・コマンドライン引数・設定ファイル・定数）の定義箇所と使われる箇所を表示する", _cmd_config)
    config.add_argument("--kind", action="append", choices=list(CONFIG_LABELS), help="種類で絞り込む")
    config.add_argument("--name", help="名前の部分一致で絞り込む")
    config.add_argument("--limit", type=int, default=5, help="1件あたりの使用箇所の表示数")

    boundaries = add("boundaries", "入口と境界（エントリポイント・CLI・HTTP・イベント・スレッド・非同期・キャッシュ）を表示する", _cmd_boundaries)
    boundaries.add_argument("--kind", action="append", choices=list(BOUNDARY_LABELS), help="種類で絞り込む")
    boundaries.add_argument("--limit", type=int, default=20, help="種類ごとの表示件数")

    understand = add("understand", "関数の読解カード: 8つの問い（なぜ存在する/誰が呼ぶ/入力/変更/戻り値/影響/失敗時/履歴）に事実で答える", _cmd_understand, ("text",))
    understand.add_argument("name", help="関数・メソッドの名前または修飾名")
    understand.add_argument("--depth", type=int, default=3, help="呼び出しをたどる深さ")
    understand.add_argument("--limit", type=int, default=8, help="各項目の表示件数")
    understand.add_argument("--file")

    history = add("history", "変更履歴（シンボル指定）、または変更頻度・同時変更の多いファイル（指定なし）を表示する（Git・読み取り専用）", _cmd_history)
    history.add_argument("name", nargs="?", help="シンボル名（省略時はリポジトリ全体の傾向）")
    history.add_argument("--limit", type=int, default=10)
    history.add_argument("--max-commits", type=int, default=2000, help="全体の傾向で調べるコミット数")
    history.add_argument("--file")

    tests = add("tests", "シンボルに届くテスト、または届くテストがないコード(--untested)を表示する", _cmd_tests)
    tests.add_argument("name", nargs="?", help="シンボル名")
    tests.add_argument("--untested", action="store_true", help="どのテストからも静的に届かない関数・クラスを一覧する")
    tests.add_argument("--depth", type=int, default=3)
    tests.add_argument("--min-lines", type=int, default=1, help="--untested で対象にする最小の行数")
    tests.add_argument("--limit", type=int, default=30)
    tests.add_argument("--file")

    impact = add("impact", "シンボルを変更したときの影響範囲（利用者側への波及・入口・テスト）を表示する", _cmd_impact)
    impact.add_argument("name", help="名前または修飾名")
    impact.add_argument("--depth", type=int, default=4)
    impact.add_argument("--limit", type=int, default=20)
    impact.add_argument("--file")

    unused = add("unused", "どこからも参照されていないシンボルの候補を、確度つきで表示する", _cmd_unused)
    unused.add_argument("--min-confidence", choices=("high", "medium", "low"), default="medium", help="表示する最低の確度")
    unused.add_argument("--limit", type=int, default=20)

    environment = add("environment", "実行環境の前提（Pythonのバージョン・依存・OS分岐・外部コマンド）を表示する", _cmd_environment)
    environment.add_argument("--limit", type=int, default=15)

    docs_check = add("docs-check", "文書の識別子・オプション・環境変数と、実装の差を探す（仕様と実装のずれの手がかり）", _cmd_docs_check)
    docs_check.add_argument("--limit", type=int, default=20)

    from codeinsight import cli_ai

    cli_ai.register(add)

    unresolved = add("unresolved", "静的に確定できなかった参照・依存関係を理由別に表示する", _cmd_unresolved)
    unresolved.add_argument("--limit", type=int, default=10, help="理由ごとに表示する件数（既定: 10）")
    unresolved.add_argument("--all", action="store_true", help="全件を表示する")

    graph = add("graph", "グラフを出力する（Mermaid / DOT / JSON / 自己完結HTML）", _cmd_graph, ("mermaid", "dot", "json", "html"))
    graph.add_argument("kind", choices=("call", "deps", "inherit", "flow", "arch"), help="呼び出し / ファイル依存 / 継承 / 関数の制御フロー(--rootが必須) / コンポーネント間のアーキテクチャ")
    graph.add_argument("--root", help="起点のシンボル名（deps は相対パス）。指定すると部分グラフを出力")
    graph.add_argument("--depth", type=int, help="起点からの深さ（archではコンポーネントとするディレクトリの深さ）")
    graph.add_argument("--direction", choices=[t.value for t in Traversal], default="both")
    graph.add_argument("--external", action="store_true", help="プロジェクト外への関係も含める")
    graph.add_argument("--no-unresolved", action="store_true", help="未解決の関係を含めない")
    graph.add_argument("--file")
    graph.add_argument("-o", "--output", help="出力先ファイル（省略時は標準出力）")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return exc.code
    except BrokenPipeError:
        # `codeinsight ... | head` のように出力先が先に閉じた場合は、静かに終了する。
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
    finally:
        while _OPENED:
            _OPENED.pop().close()


if __name__ == "__main__":
    raise SystemExit(main())

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
from codeinsight.application import (
    AmbiguousSymbolError,
    AnalysisCoordinator,
    AnalysisProgress,
    FreshnessService,
    MatchMode,
    NavigationService,
    ProjectIndex,
    ProjectManager,
    SearchService,
    SymbolNotFoundError,
)
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
        raise CliError("位置は FILE[:LINE[-END]] の形式で指定してください。")
    repository, project, index, stale = _prepare(args)
    start = int(match["start"]) if match["start"] else None
    end = int(match["end"]) if match["end"] else None
    try:
        view = NavigationService(repository).show_source(
            project, index, match["path"], start, end, args.context
        )
    except SymbolNotFoundError as exc:
        raise CliError(f"解析対象のファイルが見つかりません: {safe(match['path'])}", 2) from exc
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

    show = add("show", "ソースコードを行番号付きで表示する", _cmd_show, ("text",))
    show.add_argument("location", help="FILE[:LINE[-END]]")
    show.add_argument("--context", type=int, default=3)

    unresolved = add("unresolved", "静的に確定できなかった参照・依存関係を理由別に表示する", _cmd_unresolved)
    unresolved.add_argument("--limit", type=int, default=10, help="理由ごとに表示する件数（既定: 10）")
    unresolved.add_argument("--all", action="store_true", help="全件を表示する")

    graph = add("graph", "グラフを出力する（Mermaid / DOT / JSON / 自己完結HTML）", _cmd_graph, ("mermaid", "dot", "json", "html"))
    graph.add_argument("kind", choices=("call", "deps", "inherit"), help="呼び出し / ファイル依存 / 継承")
    graph.add_argument("--root", help="起点のシンボル名（deps は相対パス）。指定すると部分グラフを出力")
    graph.add_argument("--depth", type=int, help="起点からの深さ")
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

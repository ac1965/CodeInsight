"""探索・参照のコマンド（analyze / status / symbols / tree / search / def / refs / callers / trace / path / deps / show / describe / overview / unresolved）。"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from codeinsight.analysis.call_graph import Direction
from codeinsight.application import AnalysisCoordinator, AnalysisProgress, DescribeService, FreshnessService, MatchMode, NavigationService, OverviewService, ProjectManager, SearchService, SymbolNotFoundError
from codeinsight.application.search_service import SymbolHit
from codeinsight.bootstrap import build_symbol_extractor
from codeinsight.domain import AnalysisStatus, Confidence, FileFreshness, ResolutionStatus, SymbolKind
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.presentation import build_structure_tree, render_tree_text, tree_to_dict
from codeinsight.cli.common import CliError, NoteTable, STATIC_NOTE, STATUS_LABEL, open_project_context, resolve_db_path, dependency_dict, emit_json, format_reference, symbol_not_found, prepare_read, print_call_tree, reference_dict, resolve_symbol_arg, stale_file_paths, status_text, symbol_dict, warn_if_stale, safe


# --- コマンド ---


def cmd_analyze(args: argparse.Namespace) -> int:
    root_path = Path(args.path)
    if not root_path.is_dir():
        raise CliError(f"ディレクトリが存在しません: {root_path}")

    db_path = resolve_db_path(args)
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
        emit_json(
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


def cmd_status(args: argparse.Namespace) -> int:
    repository, project = open_project_context(args)
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
        emit_json(
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
        + ", ".join(f"{STATUS_LABEL[s]} {counts.get(s, 0)}" for s in ResolutionStatus)
    )
    labels = {"fresh": "最新", "stale": "古い(ソース変更あり)", "missing": "ファイルなし", "unanalyzed": "未解析/失敗"}
    for row in rows:
        print(f"{labels[row['freshness']]}\t{row['analysis']}\t{safe(row['path'])}")
    return 0


def cmd_symbols(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    hits = [SymbolHit(s, index.path_of(s.file_id)) for s in index.symbols.values()]
    if args.file:
        hits = [h for h in hits if h.path == args.file]
    if args.kind:
        kinds = {SymbolKind(k) for k in args.kind}
        hits = [h for h in hits if h.symbol.kind in kinds]
    hits.sort(key=lambda h: (h.path, h.symbol.start_line))
    warn_if_stale(stale)
    if args.format == "json":
        emit_json({"stale_files": stale, "results": [symbol_dict(h) for h in hits]})
        return 0
    print(f"プロジェクト: {safe(project.name)} ({project.project_id})")
    for hit in hits:
        print(f"{safe(hit.location)}\t{hit.symbol.kind.value}\t{safe(hit.symbol.qualified_name)}")
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
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
    warn_if_stale(stale)
    if args.format == "json":
        emit_json(tree_to_dict(tree))
    else:
        print(safe(render_tree_text(tree)), end="")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    repository, project = open_project_context(args)
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
        payload = [{"search": "symbol", **symbol_dict(h)} for h in symbol_hits]
        lines = [
            f"{safe(h.location)}\t{h.symbol.kind.value}\t{safe(h.symbol.qualified_name)}"
            for h in symbol_hits
        ]
        index = NavigationService(repository).load_index(project)
        stale = stale_file_paths(project, index)
    warn_if_stale(stale)
    if args.format == "json":
        emit_json({"stale_files": stale, "results": payload})
    else:
        for line in lines:
            print(line)
        if not lines:
            print("一致するものはありませんでした。", file=sys.stderr)
    return 0


def cmd_def(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    hits = NavigationService(repository).lookup(index, args.name, file=args.file)
    if not hits:
        raise symbol_not_found(project, index, args.name)
    warn_if_stale(stale)
    if args.format == "json":
        emit_json({"stale_files": stale, "results": [symbol_dict(h) for h in hits]})
    else:
        for hit in hits:
            print(f"{safe(hit.location)}\t{hit.symbol.kind.value}\t{safe(hit.symbol.qualified_name)}")
    return 0


def reference_command(args: argparse.Namespace, mode: str) -> int:
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    symbol = resolve_symbol_arg(args, navigation, index, args.name, project).symbol
    if mode == "refs":
        hits = navigation.references_to(index, symbol)
    elif mode == "callers":
        hits = navigation.callers(index, symbol)
    else:
        hits = navigation.callees(index, symbol)
        if args.hide_external:
            hits = [h for h in hits if h.reference.resolution_status != ResolutionStatus.EXTERNAL]
    warn_if_stale(stale)
    if args.format == "json":
        emit_json(
            {
                "symbol": symbol.qualified_name,
                "stale_files": stale,
                "results": [reference_dict(h) for h in hits],
            }
        )
        return 0
    notes = NoteTable()
    for hit in hits:
        print(format_reference(hit, notes))
    notes.print_footer()
    if not hits:
        print("該当する関係は確認できませんでした（未解決の関係は unresolved コマンドで確認できます）。", file=sys.stderr)
    if mode in ("callers", "callees"):
        print(STATIC_NOTE, file=sys.stderr)
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    symbol = resolve_symbol_arg(args, navigation, index, args.name, project).symbol
    tree = navigation.call_hierarchy(index, symbol, Direction(args.direction), args.depth)
    warn_if_stale(stale)
    print(f"{'呼び出し先' if args.direction == 'callees' else '呼び出し元'}の階層 (深さ {args.depth}):")
    print_call_tree(tree, show_external=args.external)
    print(STATIC_NOTE, file=sys.stderr)
    return 0


def cmd_path(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    source = resolve_symbol_arg(args, navigation, index, args.source, project).symbol
    target = resolve_symbol_arg(args, navigation, index, args.target, project).symbol
    paths = navigation.call_paths(index, source, target, args.max_depth, args.limit)
    warn_if_stale(stale)
    if args.format == "json":
        emit_json(
            {
                "source": source.qualified_name,
                "target": target.qualified_name,
                "paths": [[reference_dict(h) for h in path] for path in paths],
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
    print(STATIC_NOTE, file=sys.stderr)
    return 0


def cmd_deps(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    if args.cycles:
        cycles = navigation.dependency_cycles(index)
        if args.format == "json":
            emit_json({"cycles": cycles})
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
    warn_if_stale(stale)
    if args.format == "json":
        emit_json({"stale_files": stale, "results": [dependency_dict(h) for h in hits]})
        return 0
    notes = NoteTable()
    for hit in hits:
        d = hit.dependency
        status = status_text(d.resolution_status, d.confidence == Confidence.INFERRED)
        target = hit.target_path or d.target_name
        print(
            f"{safe(hit.location)}\t{d.dependency_kind.value}\t"
            f"{safe(hit.source_path)} -> {safe(target)}\t[{status}{notes.mark(d.note)}]"
        )
    notes.print_footer()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    match = re.fullmatch(r"(?P<path>.+?)(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?", args.location)
    if match is None:
        raise CliError("位置は FILE[:LINE[-END]] またはシンボル名で指定してください。")
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    path, start, end = match["path"], match["start"], match["end"]
    context = args.context
    if index.file_by_path(path) is None:
        # ファイルでなければ、シンボル名として解釈し、その定義全体を表示する。
        symbol = resolve_symbol_arg(args, navigation, index, args.location, project).symbol
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


def cmd_describe(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    symbol = resolve_symbol_arg(args, navigation, index, args.name, project).symbol
    description = DescribeService(navigation).describe(project, index, symbol)
    warn_if_stale(stale)
    if args.format == "json":
        emit_json(
            {
                "symbol": symbol_dict(description.hit),
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
            "・".join(f"{STATUS_LABEL[s]} {callee_counts.get(s, 0)}" for s in ResolutionStatus)
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


def cmd_overview(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    overview = OverviewService(NavigationService(repository)).build(index, args.top)
    warn_if_stale(stale)
    if args.format == "json":
        emit_json(
            {
                "project": project.name,
                "revision": project.repository_revision,
                "languages": {k.value: v for k, v in overview.languages.items()},
                "symbols": {k.value: v for k, v in overview.symbol_counts.items()},
                "modules": [vars(m) for m in overview.modules],
                "entry_points": [symbol_dict(h) for h in overview.entry_points],
                "most_called": [{**symbol_dict(r.hit), "callers": r.value} for r in overview.most_called],
                "most_calling": [{**symbol_dict(r.hit), "callees": r.value} for r in overview.most_calling],
                "largest": [{**symbol_dict(r.hit), "lines": r.value} for r in overview.largest],
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
        + ", ".join(f"{STATUS_LABEL[s]} {status.get(s, 0)}" for s in ResolutionStatus)
        + f"（未解決 {status.get(ResolutionStatus.UNRESOLVED, 0) * 100 // total}%）"
    )
    if overview.failed_files:
        print(f"  解析に失敗したファイル: {', '.join(safe(p) for p in overview.failed_files)}")
    if stale:
        print(f"  解析後に変更されたファイル: {len(stale)}件（再解析を推奨）")
    print("\n※ 静的解析で確認できた事実のみです。「何をするか」の説明は、docstringの転記を除き、含みません。")
    return 0


def cmd_unresolved(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    references = navigation.unresolved_references(index)
    dependencies = [
        h
        for h in navigation.dependencies(index)
        if h.dependency.resolution_status in (ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS)
    ]
    warn_if_stale(stale)
    if args.format == "json":
        emit_json(
            {
                "references": [reference_dict(h) for h in references],
                "dependencies": [dependency_dict(h) for h in dependencies],
            }
        )
        return 0
    print(f"未解決の参照 {len(references)}件 / 未解決の依存関係 {len(dependencies)}件（理由別）")
    groups: dict[str, list[str]] = defaultdict(list)
    for hit in references:
        groups[hit.reference.note or "（理由なし）"].append(format_reference(hit, None).split("\t[", 1)[0])
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

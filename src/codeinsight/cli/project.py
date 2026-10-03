"""プロジェクト全体の洞察のコマンド（externals / effects / architecture / config / boundaries / environment / docs-check / history / tests / impact / unused）。"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from codeinsight.application import NavigationService
from codeinsight.application.architecture_service import ROLE_LABELS, ArchitectureService
from codeinsight.application.boundary_service import KIND_LABELS as BOUNDARY_LABELS
from codeinsight.application.boundary_service import BoundaryService
from codeinsight.application.config_service import KIND_LABELS as CONFIG_LABELS
from codeinsight.application.config_service import ConfigService
from codeinsight.application.environment_service import EnvironmentService
from codeinsight.application.external_service import CATEGORY_LABELS, ExternalService
from codeinsight.application.history_service import HistoryService
from codeinsight.application.impact_service import ImpactService
from codeinsight.application.spec_check_service import SpecCheckService
from codeinsight.application.test_map_service import TestMapService
from codeinsight.application.unused_service import UnusedService
from codeinsight.cli.common import CliError, emit_json, prepare_read, resolve_symbol_arg, safe, warn_if_stale


def cmd_externals(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    report = ExternalService().report(index)
    grouped = report.by_category()
    if args.format == "json":
        emit_json(
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
    warn_if_stale(stale)
    print("※ 外部名に解決できたimport・呼び出しのみです。変数の型が不明なメソッド呼び出しは、名前からの推定のみ示します。", file=sys.stderr)
    return 0


def group_uses(uses) -> list[tuple]:
    """同じ外部名の使用を1行にまとめる（外部名, カテゴリ, 操作, 推定か, 行のリスト）。"""

    grouped: dict[tuple, list[int]] = {}
    for use in uses:
        grouped.setdefault((use.library, use.category, use.operation, use.confidence != "confirmed"), []).append(use.line)
    return [(*key, sorted(set(lines))) for key, lines in grouped.items()]


def lines_text(lines: list[int]) -> str:
    shown = ", ".join(f"L{n}" for n in lines[:4])
    return shown + (" …" if len(lines) > 4 else "")


OPERATION_LABELS = {"read": "読み取り", "write": "書き込み", "effect": "外部呼び出し", "output": "出力", "io": "入出力", "call": "呼び出し"}


def cmd_effects(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    symbol = resolve_symbol_arg(args, navigation, index, args.name, project).symbol
    service = ExternalService()
    summary = service.effects(index, service.report(index), symbol, args.depth)
    print(f"{safe(symbol.qualified_name)} の副作用の候補（外部への入出力。読み取りだけの場合もあります）")
    print("\n■ この関数が直接行うもの")
    for library, category, operation, inferred, lines in group_uses(summary.direct):
        mark = "  [推定]" if inferred else ""
        count = f" ×{len(lines)}" if len(lines) > 1 else ""
        print(f"  [{CATEGORY_LABELS[category]}・{OPERATION_LABELS[operation]}] {safe(library)}{count}  {lines_text(lines)}{mark}")
    if not summary.direct:
        print("  確認できませんでした")
    print(f"\n■ 呼び出し先（解決済み、深さ{args.depth}）を介して行うもの")
    seen_reach: set[tuple] = set()
    for use, route in summary.reachable:
        key = (use.library, route[-1])
        if key in seen_reach:
            continue
        seen_reach.add(key)
        print(f"  [{CATEGORY_LABELS[use.category]}・{OPERATION_LABELS[use.operation]}] {safe(use.library)}  {safe(use.path)}:{use.line}")
        print(f"      経路: {' → '.join(safe(r) for r in route)}")
    if not summary.reachable:
        print("  確認できませんでした")
    if summary.unresolved_calls:
        print(f"※ 呼び出し先を特定できない呼び出しが {summary.unresolved_calls}件あり、そこから先の副作用は追えていません。", file=sys.stderr)
    print("※ 副作用の種類は、外部ライブラリ・システム関数の名前による分類です。", file=sys.stderr)
    warn_if_stale(stale)
    return 0


def cmd_architecture(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    externals = ExternalService().report(index)
    architecture = ArchitectureService().build(index, externals, args.depth)
    if args.format == "json":
        emit_json(
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
    warn_if_stale(stale)
    print("※ 層の段数は依存の向きから求めた事実に基づきます。役割名（プレゼンテーション層等）は名前による推定です。", file=sys.stderr)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    items, skipped = ConfigService().scan(project, index)
    if args.kind:
        items = [i for i in items if i.kind in args.kind]
    if args.name:
        items = [i for i in items if args.name.lower() in i.name.lower()]
    if args.format == "json":
        emit_json(
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
    warn_if_stale(stale)
    return 0


def cmd_boundaries(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    items, skipped = BoundaryService().scan(project, index)
    if args.kind:
        items = [i for i in items if i.kind in args.kind]
    if args.format == "json":
        emit_json(
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
    warn_if_stale(stale)
    print("※ フレームワークの規約に基づく検出です。独自の登録方法・動的な登録は検出できません。", file=sys.stderr)
    return 0


def _entry_ids(project, index) -> set[str]:
    items, _ = BoundaryService().scan(project, index)
    return {i.target.symbol_id for i in items if i.target and i.kind in ("entry", "cli", "http", "event", "thread")}


def cmd_history(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    service = HistoryService()
    if args.name:
        navigation = NavigationService(repository)
        symbol = resolve_symbol_arg(args, navigation, index, args.name, project).symbol
        history = service.symbol_history(project, index, symbol, args.limit)
        if not history.available:
            raise CliError("Gitの履歴を取得できませんでした（Gitリポジトリではない、または未コミットのファイル）。")
        if args.format == "json":
            emit_json({"symbol": symbol.qualified_name, "modified_in_working_tree": history.modified_in_working_tree,
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
        emit_json({"commits_examined": report.commits_examined,
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


def cmd_tests(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
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
    symbol = resolve_symbol_arg(args, navigation, index, args.name, project).symbol
    mapping = service.tests_for(index, symbol, args.depth)
    if args.format == "json":
        emit_json({"symbol": symbol.qualified_name,
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


def cmd_impact(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    symbol = resolve_symbol_arg(args, navigation, index, args.name, project).symbol
    report = ImpactService().impact(index, symbol, args.depth, _entry_ids(project, index))
    if args.format == "json":
        emit_json({"symbol": symbol.qualified_name, "affected": [{"symbol": a.symbol.qualified_name, "path": a.path, "distance": a.distance,
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


def cmd_unused(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    candidates = UnusedService().candidates(index, _entry_ids(project, index))
    order = ("high", "medium", "low")
    candidates = [c for c in candidates if order.index(c.confidence) <= order.index(args.min_confidence)]
    if args.format == "json":
        emit_json([{"symbol": c.symbol.qualified_name, "kind": c.symbol.kind.value, "path": c.path, "line": c.symbol.start_line,
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
    warn_if_stale(stale)
    print("※ 動的な呼び出し・リフレクション・外部から利用される公開API・フレームワークによる登録は静的には見えません。", file=sys.stderr)
    return 0


def cmd_environment(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    report = EnvironmentService().report(project, index)
    if args.format == "json":
        emit_json({
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
    warn_if_stale(stale)
    print("※ 実行はしていません。静的に確認できる前提のみです（ライブラリの実際のバージョンやOSの設定は確認できません）。", file=sys.stderr)
    return 0


def cmd_docs_check(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    report = SpecCheckService().check(project, index)
    if args.format == "json":
        emit_json({"documents": report.documents, "checked": report.checked,
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
    warn_if_stale(stale)
    print("※ 仕様の意味は理解せず、名前の一致だけを見ています。結果は、確認すべき箇所の手がかりです。", file=sys.stderr)
    return 0

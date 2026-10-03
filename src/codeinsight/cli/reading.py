"""関数の読解のコマンド（flow / dataflow / state / exceptions / risks / understand）。"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict

from codeinsight.analysis import flow_analysis as fa
from codeinsight.application import FlowAnalysisError, FlowService, NavigationService, RiskService
from codeinsight.application.config_service import KIND_LABELS as CONFIG_LABELS
from codeinsight.application.external_service import CATEGORY_LABELS
from codeinsight.application.understand_service import UnderstandService
from codeinsight.cli.common import CliError, emit_json, prepare_read, resolve_symbol_arg, safe, warn_if_stale
from codeinsight.cli.project import OPERATION_LABELS, group_uses, lines_text
from codeinsight.domain import Language, SymbolKind


def _flow_service(args: argparse.Namespace):
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    return project, index, navigation, FlowService(navigation), stale


def _flow_symbol(args, navigation, index, project):
    return resolve_symbol_arg(args, navigation, index, args.name, project).symbol


def cmd_flow(args: argparse.Namespace) -> int:
    project, index, navigation, service, stale = _flow_service(args)
    symbol = _flow_symbol(args, navigation, index, project)
    try:
        summary = service.control_flow(project, index, symbol)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    path = index.path_of(symbol.file_id)
    if args.format == "json":
        emit_json(
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
    warn_if_stale(stale)
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


def cmd_dataflow(args: argparse.Namespace) -> int:
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
            warn_if_stale(stale)
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
    warn_if_stale(stale)
    print("※ 実行順序・条件を考慮しない近似です。値そのものや、動的な呼び出しの先は追えません。", file=sys.stderr)
    return 0


def cmd_state(args: argparse.Namespace) -> int:
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
            warn_if_stale(stale)
            return 0
        else:
            raise CliError(f"state はクラスまたはモジュールに使います（{symbol.kind.value}）。")
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    by_attribute: dict[str, list[fa.StateAccess]] = defaultdict(list)
    for access in accesses:
        by_attribute[access.attribute].append(access)
    if args.format == "json":
        emit_json({a: [vars(x) for x in items] for a, items in by_attribute.items()})
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
    warn_if_stale(stale)
    return 0


def cmd_exceptions(args: argparse.Namespace) -> int:
    project, index, navigation, service, stale = _flow_service(args)
    symbol = _flow_symbol(args, navigation, index, project)
    try:
        report = service.exceptions(project, index, symbol, args.depth)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    if args.format == "json":
        emit_json(
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
    for item in sorted(report.propagated, key=lambda e: (e.exception, e.raised_line)):
        origin = index.path_of(item.raised_in.file_id)
        print(f"  {safe(item.exception)}   raise: {safe(origin)}:{item.raised_line}  {safe(item.raised_in.qualified_name)}")
        if item.chain:
            route = safe(symbol.qualified_name) + "".join(
                f" →(L{line}) {safe(callee.qualified_name)}" for callee, line in item.chain
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
    warn_if_stale(stale)
    return 0


def cmd_risks(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    rules = set(args.rule) if args.rule else None
    findings, skipped = RiskService().scan(project, index, rules)
    order = ("low", "medium", "high")
    findings = [f for f in findings if order.index(f.severity) >= order.index(args.min_severity)]
    if args.format == "json":
        emit_json({"skipped": skipped, "findings": [{**vars(f), "message": f.message} for f in findings]})
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


def _print_section(title: str) -> None:
    print(f"\n━━ {title}")


def cmd_understand(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    symbol = resolve_symbol_arg(args, navigation, index, args.name, project).symbol
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
    for config in u.config_reads:
        default = f" 既定値 {safe(config.default)}" if config.default else ""
        print(f"  設定値: {CONFIG_LABELS[config.kind]} {safe(config.name)}{default}  ({safe(config.path)}:{config.line})")
    for library, category, operation, _inferred, lines in group_uses(u.external_inputs)[:limit]:
        print(f"  外部からの入力の可能性（直接）: [{CATEGORY_LABELS[category]}・{OPERATION_LABELS[operation]}] {safe(library)}  {lines_text(lines)}")
    if u.effects:
        seen_reads: set[tuple] = set()
        for use, route in u.effects.reads_reachable:
            key = (use.library, route[-1])
            if key in seen_reads:
                continue
            seen_reads.add(key)
            print(f"  外部からの入力の可能性（呼び出し先経由）: [{CATEGORY_LABELS[use.category]}・{OPERATION_LABELS[use.operation]}] {safe(use.library)}  経路: {' → '.join(safe(r) for r in route)}")

    _print_section("4. 何を変更するのか")
    for text in u.state_changes:
        print(f"  状態: {safe(text)}")
    for text in u.parameter_mutations:
        print(f"  引数: {safe(text)}")
    if u.effects:
        for library, category, operation, _inferred, lines in group_uses(u.effects.direct):
            count = f" ×{len(lines)}" if len(lines) > 1 else ""
            print(f"  外部への副作用の候補（直接）: [{CATEGORY_LABELS[category]}・{OPERATION_LABELS[operation]}] {safe(library)}{count}  {lines_text(lines)}")
        seen = set()
        for use, route in u.effects.reachable:
            effect_key = (use.category, use.library, route[-1])
            if effect_key in seen:
                continue
            seen.add(effect_key)
            print(f"  外部への副作用の候補（呼び出し先経由）: [{CATEGORY_LABELS[use.category]}・{OPERATION_LABELS[use.operation]}] {safe(use.library)}  経路: {' → '.join(safe(r) for r in route)}")
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
    warn_if_stale(stale)
    return 0

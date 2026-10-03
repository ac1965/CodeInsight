"""Cの関数の読解コマンド（flow / dataflow / exceptions / state）の表示。解析は application/c_flow_service。"""

from __future__ import annotations

import argparse
import sys

from codeinsight.application.c_flow_service import CFlowService
from codeinsight.application.flow_service import FlowAnalysisError
from codeinsight.application.navigation_service import NavigationService
from codeinsight.cli.common import CliError, emit_json, safe, warn_if_stale

_HINT_LABELS = {"fallthrough": "case の落ち込み", "error-return": "エラー値の戻り"}
_EXIT_LABELS = {
    "terminate": "プロセスを終了する呼び出し",
    "longjmp": "非局所ジャンプ（longjmp）",
    "assert": "assert（条件が偽なら異常終了）",
    "error_return": "エラー値の候補を返す",
    "errno": "errno を設定する",
}
_SCOPE_LABELS = {"param": "引数", "local": "ローカル変数", "global": "グローバル変数", "static": "静的変数"}


def flow(args: argparse.Namespace, project, index, navigation: NavigationService, symbol, stale) -> int:
    service = CFlowService(navigation)
    try:
        summary = service.control_flow(project, index, symbol)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    path = index.path_of(symbol.file_id)
    if args.format == "json":
        emit_json({
            "symbol": symbol.qualified_name, "path": path, "language": "c", "metrics": summary.metrics,
            "items": [vars(i) for i in summary.items], "hints": [vars(h) for h in summary.hints],
        })
        return 0
    m = summary.metrics
    print(f"{safe(symbol.qualified_name)}  ({symbol.kind.value}, {safe(path)}:{symbol.start_line}-{symbol.end_line}, {m['lines']}行)")
    print("制御構造（行番号付き。ネストはインデント）:")
    for item in summary.items:
        detail = f"  {safe(item.detail)}" if item.detail else ""
        print(f"  L{item.line:<5}{'  ' * item.depth}{item.kind}{detail}")
    if not summary.items:
        print("  （分岐・ループ・return等はありません）")
    print(
        f"指標: 分岐 {m['branches']}, ループ {m['loops']}, switch {m['switches']}（case {m['cases']}）, goto {m['gotos']}, "
        f"return {m['returns']}, 終了呼び出し {m['exits']}, 循環的複雑度 {m['cyclomatic']}, 最大ネスト {m['max_depth']}"
    )
    if summary.hints:
        print("注意して読む箇所の手がかり（構文からの推定）:")
        for hint in summary.hints:
            print(f"  L{hint.line:<5} {_HINT_LABELS.get(hint.kind, hint.kind)}: {safe(hint.detail)}")
    warn_if_stale(stale)
    print("※ 構文から確認できた構造です。実行時にどの経路を通るかは示しません。マクロ展開の内部と、条件付きコンパイルで除外された箇所は含みません。", file=sys.stderr)
    return 0


def _print_trace(trace, prefix: str = "", top: bool = True) -> None:
    if top:
        role = "引数" if trace.is_param else "変数"
        print(f"{role} {safe(trace.name)}  ({safe(trace.symbol.qualified_name)}, {safe(trace.path)})")
        defs = "; ".join(
            f"L{d.line} " + ("引数" if d.how == "param" else d.how + (f" ← {safe(d.origin)}" if d.origin else ""))
            for d in trace.definitions
        )
        print(f"{prefix}  定義: {defs or '（なし）'}")
        print(f"{prefix}  使用: {', '.join('L' + str(n) for n in trace.uses) or '（なし）'}")
    for position, item in enumerate(trace.flows):
        last = position == len(trace.flows) - 1
        status = f"  [{safe(item.status)}]" if item.status else ""
        print(f"{prefix}{'└── ' if last else '├── '}L{item.line} {safe(item.description)}{status}")
        if item.child is not None:
            child = item.child
            print(f"{prefix}{'    ' if last else '│   '}   ▸ {safe(child.symbol.qualified_name)} の {safe(child.name)}（定義 {len(child.definitions)}、使用 {len(child.uses)}）")
            _print_trace(child, prefix + ("    " if last else "│   ") + "   ", top=False)


def dataflow(args: argparse.Namespace, project, index, navigation: NavigationService, symbol, stale) -> int:
    service = CFlowService(navigation)
    try:
        if args.variable is None:
            variables = service.variables(project, index, symbol)
            print(f"{safe(symbol.qualified_name)} の変数（定義数 / 使用数 / 伝播先の数）:")
            for name, info in sorted(variables.items(), key=lambda kv: (kv[1].scope != "param", min((d.line for d in kv[1].definitions), default=0), kv[0])):
                print(f"  {_SCOPE_LABELS[info.scope]:<8} {safe(name):<20} 定義 {len(info.definitions):>2} / 使用 {len(set(info.uses)):>2} / 伝播 {len(info.flows):>2}")
            print("変数名を指定すると、値の行き先をたどります（例: dataflow <シンボル> <変数名>）。")
            warn_if_stale(stale)
            return 0
        if args.upstream:
            raise CliError("--upstream（呼び出し元の実引数の追跡）は、Cでは未対応です。")
        trace = service.trace_variable(project, index, symbol, args.variable, args.depth)
        _print_trace(trace)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    warn_if_stale(stale)
    print("※ 実行順序・条件を考慮しない近似です。値そのもの、ポインタを介した書き込み先の実体（エイリアス）、関数ポインタ・マクロの内部は追えません。", file=sys.stderr)
    return 0


def exceptions(args: argparse.Namespace, project, index, navigation: NavigationService, symbol, stale) -> int:
    service = CFlowService(navigation)
    try:
        report = service.exit_report(project, index, symbol, args.depth)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    if args.format == "json":
        emit_json({
            "symbol": symbol.qualified_name, "language": "c", "depth": args.depth, "exits": [vars(e) for e in report.direct],
            "propagated": [propagated_dict(p) for p in report.propagated],
            "unresolved_calls": report.unresolved_calls, "skipped": report.skipped, "truncated": report.truncated,
        })
        return 0
    print(f"{safe(symbol.qualified_name)} の終了・失敗の経路（Cには例外が無いため、終了呼び出し・エラー値の戻り・errno の手がかり）:")
    for item in report.direct:
        print(f"  L{item.line:<5} {_EXIT_LABELS[item.kind]}: {safe(item.detail)}")
    if not report.direct:
        print("  この関数の中には確認できませんでした")
    print(f"\n呼び出し先を経由して終了しうる経路（解決済みの呼び出しを深さ {args.depth} まで。静的な連鎖で、条件によっては実際には通りません）:")
    for item in report.propagated:
        print(f"  {_route(symbol, item)}")
        print(f"      → {safe(item.origin.qualified_name)}:L{item.line} で {safe(item.detail)}（{_EXIT_LABELS[item.kind]}）")
    if not report.propagated:
        print("  確認できませんでした")
    if report.unresolved_calls:
        print(f"  ※ 関数ポインタ・曖昧などで呼び出し先を特定できない呼び出しが {report.unresolved_calls}件あり、そこからの終了は追えていません。")
    if report.truncated:
        print(f"  ※ 深さ {args.depth} の先にまだ調べていない呼び出し先があります（--depth で広げられます）。")
    if report.skipped:
        print(f"  ※ 構文解析できず、終了の有無を確認できなかった呼び出し先: {', '.join(safe(s) for s in report.skipped[:5])}")
    warn_if_stale(stale)
    print("※ 呼び出し側がエラー値を確認しているかは追っていません。ライブラリ関数の終了は、名前（exit/abort/err 系）で判定します。", file=sys.stderr)
    return 0


def _route(symbol, item) -> str:
    """呼び出しの連鎖: 起点 →(呼び出し行) 呼び出し先 →(…) …"""

    text = safe(symbol.qualified_name)
    for callee, line in item.chain:
        text += f" →(L{line}) {safe(callee.qualified_name)}"
    return text


def propagated_dict(item) -> dict:
    return {
        "kind": item.kind, "detail": item.detail, "origin": item.origin.qualified_name, "line": item.line,
        "chain": [{"symbol": s.qualified_name, "call_line": line} for s, line in item.chain],
    }


def state(args: argparse.Namespace, project, index, navigation: NavigationService, symbol, stale) -> int:
    service = CFlowService(navigation)
    try:
        result = service.state(project, index, symbol)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    if args.format == "json":
        emit_json({"symbol": symbol.qualified_name, "language": "c", "accesses": [vars(a) for a in result.accesses],
                   "parameter_mutations": [vars(m) for m in result.parameter_mutations]})
        return 0
    print(f"{safe(symbol.qualified_name)} の状態の変化（グローバル・静的変数と、引数のポインタを通じた書き込み）")
    modes = {"write": "書き込み", "mutate": "書き換えの可能性", "read": "読み取り"}
    scopes = {"global": "グローバル変数", "static": "静的変数"}
    names = sorted({a.name for a in result.accesses})
    for name in names:
        group = [a for a in result.accesses if a.name == name]
        print(f"\n  {safe(name)}  — {scopes[group[0].scope]}")
        for mode in ("write", "mutate", "read"):
            lines = [a for a in group if a.mode == mode]
            if lines:
                print(f"    {modes[mode]}: " + ", ".join(f"L{a.line}" for a in lines[:10]) + (" …" if len(lines) > 10 else ""))
    if result.parameter_mutations:
        print("\n  引数を通じた呼び出し元のデータの変更:")
        for m in result.parameter_mutations:
            print(f"    L{m.line} 引数 {safe(m.parameter)}  {safe(m.detail)}")
    if not names and not result.parameter_mutations:
        print("  確認できませんでした")
    warn_if_stale(stale)
    print("※ ポインタを介した書き込み先の実体は追えません（引数のポインタ以外のエイリアスは含みません）。", file=sys.stderr)
    return 0

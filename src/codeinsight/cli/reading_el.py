"""Emacs Lispの関数の読解コマンド（flow / exceptions / state）の表示。解析は application/elisp_flow_service。"""

from __future__ import annotations

import argparse
import sys
from dataclasses import asdict

from codeinsight.application.elisp_flow_service import ElispFlowService
from codeinsight.application.flow_service import FlowAnalysisError
from codeinsight.application.navigation_service import NavigationService
from codeinsight.cli.common import CliError, emit_json, safe, warn_if_stale

_KIND_LABELS = {"signal": "エラーのシグナル", "terminate": "Emacsを終了する呼び出し", "throw": "throw（対応する catch へ）"}
_MODE_LABELS = {"write": "書き込み", "mutate": "書き換え", "read": "読み取り"}
_SCOPE_LABELS = {"global": "グローバル・動的変数", "buffer-local": "バッファローカル変数", "hook": "フック"}


def flow(args: argparse.Namespace, project, index, navigation: NavigationService, symbol, stale) -> int:
    service = ElispFlowService(navigation)
    try:
        summary = service.control_flow(project, index, symbol)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    path = index.path_of(symbol.file_id)
    if args.format == "json":
        emit_json({"symbol": symbol.qualified_name, "path": path, "language": "elisp", "metrics": summary.metrics, "items": [vars(i) for i in summary.items]})
        return 0
    m = summary.metrics
    print(f"{safe(symbol.qualified_name)}  ({symbol.kind.value}, {safe(path)}:{symbol.start_line}-{symbol.end_line}, {m['lines']}行)")
    print("制御構造（行番号付き。ネストはインデント）:")
    for item in summary.items:
        detail = f"  {safe(item.detail)}" if item.detail else ""
        print(f"  L{item.line:<5}{'  ' * item.depth}{item.kind}{detail}")
    if not summary.items:
        print("  （分岐・ループ・シグナル等はありません）")
    print(
        f"指標: 分岐 {m['branches']}, ループ {m['loops']}, 例外ハンドラ {m['handlers']}, シグナル {m['signals']}, "
        f"cl-return {m['returns']}, 終了呼び出し {m['exits']}, 循環的複雑度 {m['cyclomatic']}, 最大ネスト {m['max_depth']}"
    )
    warn_if_stale(stale)
    print("※ 構文から確認できた構造です。実行時にどの経路を通るかは示しません。マクロ（独自マクロ・use-package など）の展開後の構造と、バッククォートのテンプレートの内部は含みません。", file=sys.stderr)
    return 0


def _route(symbol, hop) -> str:
    text = safe(symbol.qualified_name)
    for callee, line in hop.chain:
        text += f" →(L{line}) {safe(callee.qualified_name)}"
    return text


def exceptions(args: argparse.Namespace, project, index, navigation: NavigationService, symbol, stale) -> int:
    service = ElispFlowService(navigation)
    try:
        report = service.exit_report(project, index, symbol, args.depth)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    direct = report.direct
    if args.format == "json":
        emit_json({
            "symbol": symbol.qualified_name, "language": "elisp", "depth": args.depth,
            "signals": [asdict(s) for s in direct.signals], "handlers": [asdict(h) for h in direct.handlers],
            "catches": [{"line": line, "tag": tag} for line, tag in direct.catches], "unwind_protect": direct.unwind,
            "propagated": [
                {"kind": p.signal.kind, "detail": p.signal.detail, "origin": p.origin.qualified_name, "line": p.signal.line,
                 "chain": [{"symbol": s.qualified_name, "call_line": line} for s, line in p.chain], "guards": list(p.guards)}
                for p in report.propagated
            ],
            "unresolved_calls": report.unresolved_calls, "skipped": report.skipped, "truncated": report.truncated,
        })
        return 0
    print(f"{safe(symbol.qualified_name)} のエラー・終了の経路（シグナルの送出・捕捉・後始末。組み込み関数が送出するエラーは含みません）:")
    for item in direct.signals:
        guard = f"  [囲んでいる: {'; '.join(item.guarded_by)}]" if item.guarded_by else ""
        print(f"  L{item.line:<5} {_KIND_LABELS[item.kind]}: {safe(item.detail)}{guard}")
    if not direct.signals:
        print("  この関数の中には確認できませんでした")
    if direct.handlers or direct.catches or direct.unwind:
        print("\n捕捉・後始末:")
        for h in direct.handlers:
            marks = ("握りつぶし" if h.swallowed else "") + (" 再送出あり" if h.reraises else "")
            print(f"  L{h.line:<5} {h.form}  捕捉: {safe(', '.join(h.conditions) or '（なし）')}  {marks.strip()}")
        for line, tag in direct.catches:
            print(f"  L{line:<5} catch  タグ: {safe(tag)}")
        for line in direct.unwind:
            print(f"  L{line:<5} unwind-protect（後始末）")
    print(f"\n呼び出し先を経由して送出されうる経路（解決済みの呼び出しを深さ {args.depth} まで。名前の一致による推定の連鎖で、条件によっては実際には通りません）:")
    for hop in report.propagated:
        print(f"  {_route(symbol, hop)}")
        print(f"      → {safe(hop.origin.qualified_name)}:L{hop.signal.line} で {safe(hop.signal.detail)}（{_KIND_LABELS[hop.signal.kind]}）")
        if hop.guards:
            print(f"      ※ 連鎖の途中に保護あり（捕捉されるかは、条件の一致によります）: {'; '.join(safe(g) for g in hop.guards)}")
    if not report.propagated:
        print("  確認できませんでした")
    if report.unresolved_calls:
        print(f"  ※ 呼び出し先を特定できない呼び出しが {report.unresolved_calls}件あり（funcall など）、そこからの送出は追えていません。")
    if report.truncated:
        print(f"  ※ 深さ {args.depth} の先にまだ調べていない呼び出し先があります（--depth で広げられます）。")
    if report.skipped:
        print(f"  ※ 構文解析できず確認できなかった呼び出し先: {', '.join(safe(s) for s in report.skipped[:5])}")
    warn_if_stale(stale)
    print("※ 保護の範囲は行単位の近似です。エラーの型の継承関係（error と user-error など）は考慮していません。", file=sys.stderr)
    return 0


def state(args: argparse.Namespace, project, index, navigation: NavigationService, symbol, stale) -> int:
    service = ElispFlowService(navigation)
    try:
        result = service.state(project, index, symbol)
    except FlowAnalysisError as exc:
        raise CliError(str(exc)) from exc
    if args.format == "json":
        emit_json({"symbol": symbol.qualified_name, "language": "elisp", "accesses": [vars(a) for a in result.accesses], "shadowed": result.shadowed})
        return 0
    print(f"{safe(symbol.qualified_name)} の状態の変化（局所束縛されていない変数への書き込み・書き換えと、プロジェクト内で定義された変数の読み取り）")
    for name in sorted({a.name for a in result.accesses}):
        group = [a for a in result.accesses if a.name == name]
        print(f"\n  {safe(name)}  — {_SCOPE_LABELS[group[0].scope]}")
        for mode in ("write", "mutate", "read"):
            hits = [a for a in group if a.mode == mode]
            if hits:
                print(f"    {_MODE_LABELS[mode]}: " + ", ".join(f"L{a.line}({safe(a.via)})" if mode != "read" else f"L{a.line}" for a in hits[:10]) + (" …" if len(hits) > 10 else ""))
    if not result.accesses:
        print("  確認できませんでした")
    if result.shadowed:
        print(f"\n  ※ 局所にも束縛される名前のため、グローバルへのアクセスかを判定していません: {', '.join(safe(n) for n in result.shadowed)}")
    warn_if_stale(stale)
    print("※ 変数の読み取りは、プロジェクト内で defvar 等により定義された変数に限ります。マクロの内部・funcall 経由・間接的な書き換え（変数を別の名前で渡した場合）は追えません。", file=sys.stderr)
    return 0

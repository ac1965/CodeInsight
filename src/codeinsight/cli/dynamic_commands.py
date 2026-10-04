"""動的解析のコマンド。`dynamic-plan` は何も実行しない。`dynamic-run` は、許可（--allow-run・コマンド）が揃った場合に、
コンテナの中で実行して観測を保存する。`dynamic-runs` は実行の履歴、`observed` はシンボルの観測を表示する。

このモジュールは、対象のプログラムを実行するコードを持たない（実行は dynamic/executor.py だけ。DYNAMIC_ANALYSIS.md）。
"""

from __future__ import annotations

import argparse
import os

from codeinsight.application import NavigationService
from codeinsight.cli.common import CliError, emit_json, prepare_read, resolve_symbol_arg, safe, warn_if_stale
from codeinsight.domain.observation import ObservationKind, RunStatus
from codeinsight.dynamic.executor import DEFAULT_IMAGE, ExecutorError
from codeinsight.dynamic.permission import ENV_ALLOW_RUN, DynamicPermission, DynamicPermissionError
from codeinsight.dynamic.service import DynamicAnalysisService, hash_files


def _permission(args: argparse.Namespace) -> DynamicPermission:
    command = list(args.command or [])
    if command[:1] == ["--"]:
        command = command[1:]
    return DynamicPermission(
        allow_run=bool(args.allow_run) or os.environ.get(ENV_ALLOW_RUN) == "1",
        command=tuple(command),
        allow_network=args.allow_network,
        env_allowlist=tuple(args.env or ()),
        sandbox_backend=args.sandbox,
        allow_unsandboxed=args.allow_unsandboxed,
        timeout_seconds=args.timeout,
        keep_workdir=args.keep_workdir,
    )


def cmd_dynamic_plan(args: argparse.Namespace) -> int:
    repository, project, index, _stale = prepare_read(args)
    languages = {f.language.value for f in index.files.values()}
    plan = DynamicAnalysisService().plan(project, _permission(args), languages)
    if args.format == "json":
        emit_json({
            "project": plan.project, "permitted": plan.permitted, "denied_reasons": plan.denied_reasons, "permission": plan.permission,
            "sandbox": plan.sandbox, "sandbox_available": plan.sandbox_available,
            "collectors": [{"name": c.name, "language": c.language, "kinds": [k.value for k in c.kinds], "mechanism": c.mechanism,
                            "needs_rebuild": c.needs_rebuild, "implemented": c.implemented} for c in plan.collectors],
            "notes": plan.notes, "executed": False,
        })
        return 0
    print("動的解析の計画（表示のみ。何も実行していません）")
    print(f"  プロジェクト: {safe(plan.project)}")
    command = " ".join(safe(c) for c in plan.permission["command"]) or "（未指定）"
    print(f"  実行するコマンド: {command}")
    if plan.permitted:
        print("  許可: 揃っています（この計画は表示のみ。実行は dynamic-run）")
    else:
        print("  許可: 揃っていません。実行は拒否されます:")
        for reason in plan.denied_reasons:
            print(f"    - {reason}")
    sandbox = plan.sandbox
    available = {True: "利用できる", False: "この計算機では利用できない", None: "—"}[plan.sandbox_available]
    print(f"  隔離: {sandbox['backend']}（{available}） / ネットワーク: {'許可' if sandbox['network'] == 'allowed' else '遮断'}"
          f" / 対象: {sandbox['target_mount']} / 作業領域: {sandbox['workdir']} / 時間: {sandbox['timeout_seconds']:.0f}秒"
          f" / メモリ: {sandbox['memory_mb']}MB / 環境変数: {', '.join(sandbox['env']) or '（なし）'}")
    print("  収集器（実行できるのは Python のみ）:")
    for collector in plan.collectors:
        rebuild = " [再コンパイルを伴う]" if collector.needs_rebuild else ""
        print(f"    {collector.name}: {', '.join(k.value for k in collector.kinds)} — {collector.mechanism}{rebuild}")
    if not plan.collectors:
        print("    （このプロジェクトの言語に対応する収集器はありません）")
    for note in plan.notes:
        print(f"  ※ {note}")
    return 0


def cmd_dynamic_run(args: argparse.Namespace) -> int:
    repository, project, index, _stale = prepare_read(args)
    permission = _permission(args)
    try:
        outcome = DynamicAnalysisService().run(project, permission, index, repository, image=args.image)
    except DynamicPermissionError as exc:
        raise CliError("動的解析を実行できません（許可が揃っていません）:\n  - " + "\n  - ".join(exc.reasons) + "\n  計画は dynamic-plan で確認できます。", 2) from exc
    except ExecutorError as exc:
        raise CliError(f"{exc}\n対象は実行されていません。", 3) from exc
    run = outcome.run
    labels = {RunStatus.COMPLETED: "完了", RunStatus.FAILED: "異常終了", RunStatus.TIMEOUT: "時間切れ", RunStatus.TARGET_MODIFIED: "対象が変更された"}
    print(f"動的解析を実行しました（実行ID: {run.run_id}）— 観測は、この1回の実行で起きたことの記録です。")
    print(f"  結果: {labels.get(run.status, run.status.value)} / 終了コード: {run.exit_code} / {run.duration_seconds}秒 / 収集器: {run.collector} {run.collector_version}")
    kinds = {k: sum(1 for o in outcome.observations if o.kind == k) for k in ObservationKind}
    matched = sum(1 for o in outcome.observations if o.symbol_id)
    print(f"  観測: 呼び出し {kinds[ObservationKind.CALL]}件 / 実行された関数 {sum(1 for o in outcome.observations if o.detail.get('what') == 'function')}件"
          f" / 例外 {kinds[ObservationKind.FAILURE]}件（解析結果のシンボルに対応づけられたもの {matched}件）")
    for note in run.notes:
        print(f"  ※ {note}")
    if outcome.stderr_tail.strip():
        print("  対象の標準エラー（末尾）:")
        for line in outcome.stderr_tail.strip().splitlines()[-8:]:
            print(f"    | {safe(line)}")
    return 0 if run.status == RunStatus.COMPLETED else 1


def cmd_dynamic_runs(args: argparse.Namespace) -> int:
    repository, project, index, _stale = prepare_read(args)
    runs = repository.list_dynamic_runs(project.project_id)
    current = hash_files(project.root_path.resolve(), sorted({p for r in runs for p in r.source_hashes}))
    rows = [
        {"run_id": r.run_id, "started_at": r.started_at.isoformat(timespec="seconds"), "status": r.status.value, "exit_code": r.exit_code,
         "command": list(r.command), "collector": r.collector, "stale_files": sorted(p for p, h in r.source_hashes.items() if current.get(p) != h)}
        for r in runs
    ]
    if args.format == "json":
        emit_json({"runs": rows})
        return 0
    if not rows:
        print("動的解析の実行履歴はありません（dynamic-run で実行できます）。")
        return 0
    for row in rows:
        stale = f" ※実行後にソースが変わっています（{len(row['stale_files'])}ファイル。位置が対応しない可能性）" if row["stale_files"] else ""
        print(f"{row['run_id']}  {row['started_at']}  {row['status']}  終了コード {row['exit_code']}  {safe(' '.join(row['command']))}{stale}")
    return 0


def cmd_observed(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    symbol = resolve_symbol_arg(args, navigation, index, args.symbol, project).symbol
    runs = repository.list_dynamic_runs(project.project_id)
    if not runs:
        print("動的解析の実行履歴がありません。観測はありません（観測が無いことは、実行されないことを意味しません）。")
        return 0
    static_callees = {r.reference.target_symbol_id for r in navigation.callees(index, symbol)}
    current = hash_files(project.root_path.resolve(), sorted({p for r in runs for p in r.source_hashes}))
    path = index.path_of(symbol.file_id)
    report = []
    for run in runs:
        observations = repository.list_dynamic_observations(run.run_id)
        executed = [o for o in observations if o.symbol_id == symbol.symbol_id and o.detail.get("what") == "function"]
        callees = [o for o in observations if o.kind == ObservationKind.CALL and o.symbol_id == symbol.symbol_id]
        callers = [o for o in observations if o.kind == ObservationKind.CALL and o.target_symbol_id == symbol.symbol_id]
        failures = [o for o in observations if o.kind == ObservationKind.FAILURE and o.path == path and symbol.start_line <= (o.start_line or 0) <= symbol.end_line]
        report.append((run, executed, callers, callees, failures, run.source_hashes.get(path) not in (None, current.get(path))))
    if args.format == "json":
        emit_json({"symbol": symbol.qualified_name, "path": path, "runs": [
            {"run_id": r.run_id, "status": r.status.value, "stale": st, "executed_count": sum(o.count for o in ex),
             "callers": [{"name": o.name, "path": o.path, "count": o.count} for o in cr],
             "callees": [{"name": o.target_name, "path": o.target_path, "count": o.count, "in_static": o.target_symbol_id in static_callees} for o in ce],
             "raised": [{"type": o.detail.get("type"), "line": o.start_line, "count": o.count} for o in fa]}
            for r, ex, cr, ce, fa, st in report]})
        return 0
    warn_if_stale(stale)
    print(f"観測: {symbol.qualified_name}（{path}:{symbol.start_line}-{symbol.end_line}）— 実行して観測した結果。静的解析の事実とは別です。")
    for run, executed, callers, callees, failures, is_stale in report:
        print(f"\n実行 {run.run_id}（{run.started_at:%Y-%m-%d %H:%M}、{run.status.value}、{safe(' '.join(run.command))}）")
        if is_stale:
            print("  ※ 実行後にこのファイルが変わっているため、位置が対応しない可能性があります（古い観測）。")
        if not executed:
            print("  この実行では、実行されたことを観測できませんでした（実行されない、とは限りません）。")
            continue
        print(f"  実行された回数: {sum(o.count for o in executed)}")
        for o in callers:
            print(f"  呼び出し元: {safe(o.name)} ({o.path}) ×{o.count}")
        for o in callees:
            mark = "静的にも確認" if o.target_symbol_id in static_callees else "静的解析では確認できていない呼び出し"
            print(f"  呼び出し先: {safe(o.target_name)} ({o.target_path}) ×{o.count}  [{mark}]")
        for o in failures:
            print(f"  例外: {o.detail.get('type')} L{o.start_line} ×{o.count}")
    return 0


def register(add) -> None:
    for name, help_text, func in (
        ("dynamic-plan", "動的解析の計画を表示する（実行しない。許可・隔離・収集器の確認）", cmd_dynamic_plan),
        ("dynamic-run", "動的解析を実行する（許可が揃った場合のみ。コンテナの中で実行し、観測を保存する）", cmd_dynamic_run),
    ):
        sub = add(name, help_text, func, ("text", "json") if name == "dynamic-plan" else ("text",), exclude=False)
        sub.add_argument("--allow-run", action="store_true", help=f"対象の実行を許可する（または環境変数 {ENV_ALLOW_RUN}=1）。既定は拒否")
        sub.add_argument("--allow-network", action="store_true", help="ネットワークを許可する（既定は遮断）")
        sub.add_argument("--env", action="append", metavar="NAME", help="実行に渡す環境変数の名前（複数指定可。既定は何も渡さない）")
        sub.add_argument("--sandbox", choices=("container", "bwrap", "none"), default="container", help="隔離の種類（none は --allow-unsandboxed が必要）")
        sub.add_argument("--allow-unsandboxed", action="store_true", help="隔離なしの実行を許可する（非推奨）")
        sub.add_argument("--timeout", type=float, default=60.0, help="時間の上限（秒）")
        if name == "dynamic-run":
            sub.add_argument("--image", default=DEFAULT_IMAGE, help=f"実行に使うコンテナのイメージ（既定: {DEFAULT_IMAGE}。取得は自動では行わない）")
        sub.add_argument("--keep-workdir", action="store_true", help="実行後も作業領域を残す")
        sub.add_argument("command", nargs=argparse.REMAINDER, help="-- の後に、実行するコマンド（シェルを介さない argv）")
    runs = add("dynamic-runs", "動的解析の実行履歴を表示する（実行後にソースが変わった実行は、古いものとして示す）", cmd_dynamic_runs, exclude=False)
    observed = add("observed", "シンボルについて、動的解析で観測されたこと（実行回数・呼び出し元/先・例外）を表示する", cmd_observed)
    observed.add_argument("symbol", help="関数・メソッド名")
    observed.add_argument("--file")
    del runs

"""動的解析のコマンド（スタブ）。`dynamic-plan` は実行しない計画を表示する。`dynamic-run` は許可を確認し、実行は未実装。

このモジュールは、対象のプログラムを実行するコードを持たない（DYNAMIC_ANALYSIS.md）。
"""

from __future__ import annotations

import argparse
import os

from codeinsight.cli.common import CliError, emit_json, prepare_read, safe
from codeinsight.dynamic.permission import ENV_ALLOW_RUN, DynamicAnalysisNotImplemented, DynamicPermission, DynamicPermissionError
from codeinsight.dynamic.service import DynamicAnalysisService


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
        print("  許可: 揃っています（ただし、実行は未実装です）")
    else:
        print("  許可: 揃っていません。実行は拒否されます:")
        for reason in plan.denied_reasons:
            print(f"    - {reason}")
    sandbox = plan.sandbox
    available = {True: "利用できる", False: "この計算機では利用できない", None: "—"}[plan.sandbox_available]
    print(f"  隔離: {sandbox['backend']}（{available}） / ネットワーク: {'許可' if sandbox['network'] == 'allowed' else '遮断'}"
          f" / 対象: {sandbox['target_mount']} / 作業領域: {sandbox['workdir']} / 時間: {sandbox['timeout_seconds']:.0f}秒"
          f" / メモリ: {sandbox['memory_mb']}MB / 環境変数: {', '.join(sandbox['env']) or '（なし）'}")
    print("  収集器（予定。すべて未実装）:")
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
        DynamicAnalysisService().run(project, permission)
    except DynamicPermissionError as exc:
        raise CliError("動的解析を実行できません（許可が揃っていません）:\n  - " + "\n  - ".join(exc.reasons) + "\n  計画は dynamic-plan で確認できます。", 2) from exc
    except DynamicAnalysisNotImplemented as exc:
        raise CliError(f"{exc}\n許可は確認できましたが、実行機能がないため、何も実行していません（設計: DYNAMIC_ANALYSIS.md）。", 3) from exc
    return 0


def register(add) -> None:
    for name, help_text, func in (
        ("dynamic-plan", "動的解析の計画を表示する（スタブ。実行しない。許可・隔離・収集器の確認）", cmd_dynamic_plan),
        ("dynamic-run", "動的解析を実行する（スタブ。許可を確認するだけで、実行は未実装）", cmd_dynamic_run),
    ):
        sub = add(name, help_text, func, ("text", "json") if name == "dynamic-plan" else ("text",), exclude=False)
        sub.add_argument("--allow-run", action="store_true", help=f"対象の実行を許可する（または環境変数 {ENV_ALLOW_RUN}=1）。既定は拒否")
        sub.add_argument("--allow-network", action="store_true", help="ネットワークを許可する（既定は遮断）")
        sub.add_argument("--env", action="append", metavar="NAME", help="実行に渡す環境変数の名前（複数指定可。既定は何も渡さない）")
        sub.add_argument("--sandbox", choices=("container", "bwrap", "none"), default="container", help="隔離の種類（none は --allow-unsandboxed が必要）")
        sub.add_argument("--allow-unsandboxed", action="store_true", help="隔離なしの実行を許可する（非推奨）")
        sub.add_argument("--timeout", type=float, default=60.0, help="時間の上限（秒）")
        sub.add_argument("--keep-workdir", action="store_true", help="実行後も作業領域を残す")
        sub.add_argument("command", nargs=argparse.REMAINDER, help="-- の後に、実行するコマンド（シェルを介さない argv）")

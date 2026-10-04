"""外部のコード索引（SCIP）のコマンド（import-scip / compare-scip）。解析は application/external_index_service。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codeinsight.application.external_index_service import Comparison, ExternalIndexService
from codeinsight.cli.common import CliError, emit_json, open_project_context, prepare_read, safe, warn_if_stale
from codeinsight.infrastructure.scip import ScipError


def cmd_import_scip(args: argparse.Namespace) -> int:
    repository, project = open_project_context(args)
    service = ExternalIndexService(repository)
    if args.clear:
        count = service.clear(project, args.tool)
        print(f"外部のコード索引を {count}件 削除しました" + (f"（{safe(args.tool)}）" if args.tool else ""))
        return 0
    if not args.file:
        raise CliError("SCIPの索引ファイル（index.scip）を指定してください（または --clear）。")
    try:
        report = service.import_scip(project, Path(args.file).expanduser())
    except ScipError as exc:
        raise CliError(f"SCIPの索引を取り込めません: {exc}") from exc
    if args.format == "json":
        emit_json(vars(report))
        return 0
    print(f"外部のコード索引を取り込みました（ツール: {safe(report.tool or '不明')} {safe(report.tool_version)}）")
    print(f"  文書 {report.documents}件、出現箇所 {report.occurrences}件")
    if report.skipped_outside:
        print(f"  取り込まなかった文書: {report.skipped_outside}件（プロジェクトのルートの外、または解釈できないパス）")
    if report.skipped_missing:
        print(f"  取り込まなかった文書: {report.skipped_missing}件（ルートの中に存在しないファイル）")
    if report.root_note:
        print(f"  ※ {report.root_note}")
    print("※ 索引は外部ツールの結果で、CodeInsight自身の解析結果ではありません。compare-scip で、自身の参照解決と比較できます。ツールは実行していません。", file=sys.stderr)
    return 0


def _row(item: Comparison) -> str:
    ours = safe(item.ours) if item.ours else "（解決できず）"
    index = ", ".join(f"{safe(p)}:{ln}" for p, ln in item.index[:3]) or "（プロジェクトの外のシンボル）"
    return f"  {safe(item.path)}:{item.line}  {safe(item.symbol)}  CodeInsight: {ours}  /  索引: {index}"


def cmd_compare_scip(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    service = ExternalIndexService(repository)
    if not service.summaries(project):
        raise CliError("取り込み済みの索引がありません。import-scip で取り込んでください。")
    report = service.compare(project, index)
    if args.format == "json":
        emit_json({
            "indexes": report.indexes, "compared": report.compared, "confirmed": report.confirmed, "confirmed_inferred": report.confirmed_inferred,
            "consistent_external": report.consistent_external, "no_occurrence": report.no_occurrence, "stale_files": report.stale_files, "by_state": report.by_state,
            "conflicts": [_json(c) for c in report.conflicts], "index_resolves": [_json(c) for c in report.index_resolves],
        })
        return 0
    print(f"外部のコード索引との比較（{safe(', '.join(report.indexes))}）。索引はCodeInsight自身の解析結果ではありません。")
    print(f"  比較した参照: {report.compared}件（索引が対象にしたファイルの中。自身の解決状態: " + ", ".join(f"{k} {v}" for k, v in sorted(report.by_state.items())) + "）")
    print(f"  一致（自身の解決先が、索引の定義と同じ）: {report.confirmed}件（うち、自身が「推定」としていたもの {report.confirmed_inferred}件）")
    print(f"  どちらもプロジェクトの外・解決なし: {report.consistent_external}件 / その行に同名の出現箇所が索引にない: {report.no_occurrence}件")
    print(f"\n■ 解決先が食い違う: {len(report.conflicts)}件（どちらが正しいかは、ここでは判定しません）")
    for item in report.conflicts[: args.limit]:
        print(_row(item))
    print(f"\n■ 自身は解決できず、索引は定義を示す: {len(report.index_resolves)}件（自身の解析結果には採用していません）")
    for item in report.index_resolves[: args.limit]:
        print(_row(item))
    if report.stale_files:
        print(f"\n  ※ 取り込み後に変更されたため、比較から除いたファイル: {', '.join(safe(p) for p in report.stale_files[:5])}" + (" …" if len(report.stale_files) > 5 else ""))
    warn_if_stale(stale)
    print("※ 比較は、行と名前の一致による近似です（同じ行に同名の出現箇所が複数ある場合など）。", file=sys.stderr)
    return 0


def _json(item: Comparison) -> dict:
    return {"path": item.path, "line": item.line, "name": item.symbol, "ours": item.ours, "index_definitions": [{"path": p, "line": ln} for p, ln in item.index],
            "resolution": item.reference.resolution_status.value, "confidence": item.reference.confidence.value}

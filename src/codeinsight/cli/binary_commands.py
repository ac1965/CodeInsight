"""外部ツール radare2 の結果（バイナリの関数・呼び出し）のコマンド（import-r2 / compare-r2）。解析は application/external_binary_service。

radare2 は、利用者が実行して出力（aflj・agCj・ij）を保存したファイルを読むだけで、CodeInsight は実行しない。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codeinsight.application.external_binary_service import BinaryComparison, EdgeItem, ExternalBinaryService
from codeinsight.cli.common import CliError, emit_json, open_project_context, prepare_read, safe, warn_if_stale
from codeinsight.infrastructure.r2 import R2Error

HOW_TO = (
    "radare2 の出力は、利用者が実行して用意します（CodeInsight は radare2 を実行しません）。例（デバッグ情報つきでビルドしたバイナリを推奨）:\n"
    "  r2 -q -c 'aaa; aflj' ./app > funcs.json\n"
    "  r2 -q -c 'aaa; agCj' ./app > callgraph.json\n"
    "  r2 -q -c 'ij' ./app > info.json"
)


def cmd_import_r2(args: argparse.Namespace) -> int:
    repository, project = open_project_context(args)
    service = ExternalBinaryService(repository)
    if args.clear:
        count = service.clear(project)
        print(f"外部ツール radare2 の結果を {count}件 削除しました")
        return 0
    if not args.file:
        raise CliError("関数の一覧（aflj の出力のJSON）を指定してください（または --clear）。\n" + HOW_TO)
    try:
        report = service.import_r2(
            project, Path(args.file).expanduser(), Path(args.callgraph).expanduser() if args.callgraph else None,
            Path(args.info).expanduser() if args.info else None, Path(args.binary).expanduser() if args.binary else None, args.tool_version or "",
        )
    except R2Error as exc:
        raise CliError(f"radare2 の出力を取り込めません: {exc}\n" + HOW_TO) from exc
    if args.format == "json":
        emit_json(vars(report))
        return 0
    print(f"外部ツール radare2 の結果を取り込みました（関数 {report.functions}件（うち外部ライブラリ {report.imports}件）、呼び出しの辺 {report.calls}件）")
    print(f"  デバッグ情報のソースファイルをプロジェクトのファイルに対応づけられた数: {report.files_matched}件"
          + (f"（対応づけられなかった: {', '.join(safe(f) for f in report.files_unmatched[:5])}）" if report.files_unmatched else ""))
    if report.binary_path:
        print(f"  バイナリ: {safe(report.binary_path)}（内容のハッシュ: {'記録' if report.binary_hashed else '記録できず'}）")
    for note in report.notes:
        print(f"  ※ {note}")
    print("※ バイナリの解析結果は外部ツール（radare2）のもので、CodeInsight自身の解析結果ではありません。compare-r2 で、自身の解析結果と比較できます。radare2 は実行していません。", file=sys.stderr)
    return 0


def _edge(item: EdgeItem) -> str:
    text = f"  {safe(item.caller)} → {safe(item.callee)}"
    if item.candidates:
        text += f"\n      解決候補（採用はしない）: 呼び出し元の未解決・曖昧な呼び出し {safe(', '.join(item.candidates[:3]))}"
    if item.note:
        text += f"\n      {item.note}"
    return text


def cmd_compare_r2(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    service = ExternalBinaryService(repository)
    if not service.summaries(project):
        raise CliError("取り込み済みの radare2 の結果がありません。import-r2 で取り込んでください。\n" + HOW_TO)
    report = service.compare(project, index)
    if args.format == "json":
        emit_json(_json(report))
        return 0
    print(f"外部ツール radare2 の結果との比較（{safe('; '.join(report.binaries))}）。バイナリの結果はCodeInsight自身の解析結果ではありません。")
    print(f"  ソースの関数に対応づけられたバイナリの関数: {len(report.matched)}件 / 曖昧: {len(report.ambiguous)}件 / ソースに対応する関数が見つからない: {len(report.binary_only)}件")
    print(f"  呼び出しの辺: 両方にある {len(report.edges_both)}件 / バイナリのみ {len(report.edges_binary_only)}件 / ソース（CodeInsight）のみ {len(report.edges_source_only)}件")
    limit = args.limit
    for title, items in (
        ("バイナリのみの辺（CodeInsight の確定済みの辺に無い。関数ポインタ先など、自身が未解決の呼び出しの手がかりになりうる）", report.edges_binary_only),
        ("ソースのみの辺（バイナリの辺に無い。インライン化・最適化・条件付きコンパイルなど）", report.edges_source_only),
    ):
        print(f"\n■ {title}: {len(items)}件")
        for item in items[:limit]:
            print(_edge(item))
        if len(items) > limit:
            print(f"  …ほか{len(items) - limit}件（--limit で変更）")
    print(f"\n■ ソースにあるが、バイナリに見つからない関数（バイナリに含まれるソースファイルの中）: {len(report.source_only)}件")
    for name in report.source_only[:limit]:
        print(f"  {safe(name)}")
    if report.ambiguous:
        print(f"\n■ 名前が一致するソースの関数が複数ある（1つを選びません）: {len(report.ambiguous)}件")
        for name, candidates in report.ambiguous[:limit]:
            print(f"  {safe(name)}: {safe(', '.join(candidates[:4]))}")
    if report.stale_files:
        print(f"\n  ※ 取り込み後に変更されたため、比較から除いたソース: {', '.join(safe(p) for p in report.stale_files[:5])}" + (" …" if len(report.stale_files) > 5 else ""))
    for note in report.notes:
        print(f"  ※ {note}", file=sys.stderr)
    warn_if_stale(stale)
    return 0


def _json(report: BinaryComparison) -> dict:
    def edges(items: list[EdgeItem]) -> list[dict]:
        return [{"caller": e.caller, "callee": e.callee, "candidates": list(e.candidates), "note": e.note} for e in items]

    return {
        "binaries": report.binaries, "matched": [vars(p) for p in report.matched], "ambiguous": [{"binary_function": n, "candidates": c} for n, c in report.ambiguous],
        "binary_only": report.binary_only, "source_only": report.source_only, "edges_both": edges(report.edges_both),
        "edges_binary_only": edges(report.edges_binary_only), "edges_source_only": edges(report.edges_source_only),
        "stale_files": report.stale_files, "notes": report.notes, "source": "radare2（外部ツール。CodeInsight自身の解析結果ではない）",
    }

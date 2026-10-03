"""`reading-report`: `make reading` の成果物を、1ファイルのPDF（またはHTML）にまとめる。"""

from __future__ import annotations

import argparse
from pathlib import Path

from codeinsight.cli.common import CliError, emit_json, safe
from codeinsight.presentation.pdf_export import PdfExportError, find_browser, html_to_pdf
from codeinsight.presentation.reading_report import DEFAULT_MAX_GRAPH_NODES, DEFAULT_MAX_LINES, build_report


def cmd_reading_report(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).expanduser()
    if not (out_dir / "README.md").is_file():
        raise CliError(f"{out_dir} に、make reading の成果物（README.md）が見つかりません。先に make reading を実行してください。", 2)
    report = build_report(out_dir, target=args.target or "", max_lines=args.max_lines, max_graph_nodes=args.max_graph_nodes)
    stem = args.name or f"{out_dir.resolve().name}-reading"
    html_path = Path(args.html).expanduser() if args.html else out_dir / f"{stem}.html"
    html_path.write_text(report.html, encoding="utf-8")
    result: dict[str, object] = {
        "html": str(html_path), "pdf": None, "sections": report.sections, "graphs": report.graphs, "omitted": report.omitted, "error": None,
    }
    code = 0
    if args.format == "pdf":
        pdf_path = Path(args.output).expanduser() if args.output else out_dir / f"{stem}.pdf"
        try:
            html_to_pdf(html_path, pdf_path, timeout=args.timeout)
            result["pdf"] = str(pdf_path)
        except PdfExportError as exc:
            result["error"] = str(exc)
            code = 1
    if args.json:
        emit_json(result)
        return code
    print(f"HTML: {safe(str(html_path))}（{len(report.sections)}章、図 {len(report.graphs)}点）")
    if args.format == "pdf":
        if result["pdf"]:
            print(f"PDF: {safe(str(result['pdf']))}（ブラウザ: {safe(find_browser() or '-')}）")
        else:
            print(f"PDFを作れませんでした: {safe(str(result['error']))}")
    for item in report.omitted:
        print(f"  ※ 省略・打ち切り: {safe(item)}")
    return code


def register(subparsers) -> None:
    sub = subparsers.add_parser("reading-report", help="make reading の成果物を、1ファイルのPDF（またはHTML）にまとめる")
    sub.add_argument("--out", required=True, help="make reading の出力ディレクトリ")
    sub.add_argument("--target", help="表紙に載せる対象のパス")
    sub.add_argument("--format", choices=("pdf", "html"), default="pdf", help="pdf: HTMLを作り、ブラウザでPDFに変換する / html: HTMLのみ")
    sub.add_argument("--output", help="PDFの出力先（既定: OUT/<名前>-reading.pdf）")
    sub.add_argument("--html", help="HTMLの出力先（既定: OUT/<名前>-reading.html）")
    sub.add_argument("--name", help="ファイル名の基（既定: OUT のディレクトリ名-reading）")
    sub.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES, help="各項目の最大行数（超えたら打ち切り、その旨を載せる）")
    sub.add_argument("--max-graph-nodes", type=int, default=DEFAULT_MAX_GRAPH_NODES, help="図に載せるノード数の上限（超えたら省略）")
    sub.add_argument("--timeout", type=float, default=180.0, help="PDF変換の時間の上限（秒）")
    sub.add_argument("--json", action="store_true", help="結果をJSONで出力する")
    sub.set_defaults(func=cmd_reading_report)

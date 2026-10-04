"""グラフ出力のコマンド（graph）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codeinsight.application import NavigationService, ProjectIndex
from codeinsight.application.graph_builder import GraphModel, Traversal
from codeinsight.application.graph_service import GraphRequest, GraphRequestError, GraphService
from codeinsight.cli.common import CliError, prepare_read, resolve_symbol_arg, warn_if_stale
from codeinsight.domain import Project
from codeinsight.presentation import render_html, to_dot, to_json, to_mermaid


def _build_graph(
    args: argparse.Namespace, index: ProjectIndex, navigation: NavigationService, project: Project
) -> GraphModel:
    """引数を解釈し（シンボル名の解決）、GraphService で組み立てる。"""

    if args.kind == "flow" and not args.root:
        raise CliError("graph flow には --root で関数・メソッド名を指定してください。")
    root_symbol = None
    if args.kind in ("call", "inherit", "flow") and args.root:
        root_symbol = resolve_symbol_arg(args, navigation, index, args.root, project).symbol
    request = GraphRequest(
        args.kind, root_symbol, args.root if args.kind == "deps" else None, args.depth, Traversal(args.direction),
        include_external=args.external, include_unresolved=not args.no_unresolved,
    )
    try:
        return GraphService(navigation).build(project, index, request)
    except GraphRequestError as exc:
        raise CliError(str(exc), 2 if args.kind == "deps" else 1) from exc


def cmd_graph(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    model = GraphService.annotate(_build_graph(args, index, NavigationService(repository), project), project, stale)
    warn_if_stale(stale)
    if len(model.nodes) > 200 and not args.root:
        print(
            f"ヒント: ノードが{len(model.nodes)}個あり、読み取りにくい可能性があります。"
            "--root と --depth、または --exclude で絞り込めます。",
            file=sys.stderr,
        )
    renderers = {"mermaid": to_mermaid, "dot": to_dot, "json": to_json, "html": render_html}
    output = renderers[args.format](model)
    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"出力しました: {args.output}（ノード {len(model.nodes)} / 辺 {len(model.edges)}）", file=sys.stderr)
    else:
        print(output, end="")
    return 0

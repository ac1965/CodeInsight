"""グラフ出力のコマンド（graph）。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from codeinsight.application.architecture_service import ArchitectureService
from codeinsight.application.external_service import ExternalService
from codeinsight.application import CfgBuilder, FlowAnalysisError, FlowService, NavigationService, ProjectIndex
from codeinsight.application.graph_builder import GraphBuilder, GraphModel, Traversal
from codeinsight.domain import Project
from codeinsight.presentation import render_html, to_dot, to_json, to_mermaid
from codeinsight.cli.common import CliError, prepare_read, resolve_symbol_arg, warn_if_stale, safe


def _build_graph(
    args: argparse.Namespace, index: ProjectIndex, navigation: NavigationService, project: Project
) -> GraphModel:
    builder = GraphBuilder(index)
    traversal = Traversal(args.direction)
    if args.kind == "call":
        root = resolve_symbol_arg(args, navigation, index, args.root, project).symbol if args.root else None
        return builder.call_graph(
            root,
            args.depth,
            traversal,
            include_unresolved=not args.no_unresolved,
            include_external=args.external,
        )
    if args.kind == "inherit":
        root = resolve_symbol_arg(args, navigation, index, args.root, project).symbol if args.root else None
        return builder.inheritance_graph(root, args.depth, traversal)
    if args.kind == "arch":
        architecture = ArchitectureService().build(index, ExternalService().report(index), args.depth)
        return builder.architecture_graph(architecture)
    if args.kind == "flow":
        if not args.root:
            raise CliError("graph flow には --root で関数・メソッド名を指定してください。")
        symbol = resolve_symbol_arg(args, navigation, index, args.root, project).symbol
        try:
            function = FlowService(navigation).function_ast(project, index, symbol)
        except FlowAnalysisError as exc:
            raise CliError(str(exc)) from exc
        return CfgBuilder().build(function, index.path_of(symbol.file_id), symbol.qualified_name)
    try:
        return builder.file_dependency_graph(
            args.root,
            args.depth,
            traversal,
            include_external=args.external,
            include_unresolved=not args.no_unresolved,
        )
    except LookupError as exc:
        raise CliError(f"ファイルが見つかりません: {safe(args.root)}", 2) from exc


def cmd_graph(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    model = _build_graph(args, index, NavigationService(repository), project)
    model.meta.update(
        {
            "project": project.name,
            "repository_revision": project.repository_revision,
            "stale_files": stale,
        }
    )
    if stale:
        model.notes.append(f"解析後に変更されたファイルがあります（{len(stale)}件）。内容が古い可能性があります。")
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

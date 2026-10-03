from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import AnalysisFileStatus, Symbol, SymbolKind


@dataclass
class TreeNode:
    label: str
    kind: str  # project / directory / file / <シンボル種別>
    path: str | None = None
    line: int | None = None
    status: str | None = None  # 解析に失敗したファイルなどの注記
    children: list["TreeNode"] = field(default_factory=list)


_VARIABLE_KINDS = frozenset(
    {
        SymbolKind.GLOBAL_VARIABLE,
        SymbolKind.STATIC_VARIABLE,
        SymbolKind.LOCAL_VARIABLE,
        SymbolKind.CLASS_VARIABLE,
    }
)


def build_structure_tree(
    index: ProjectIndex,
    project_name: str,
    include_locals: bool = False,
    include_variables: bool = True,
    max_symbol_depth: int | None = None,
) -> TreeNode:
    """ディレクトリ -> ファイル -> シンボル(親子関係)の階層を組み立てる（3.3）。

    Pythonのモジュールシンボルはファイルと同じ内容なので、ファイルノードに統合する。
    max_symbol_depth はファイルの下に表示するシンボルの階層数（1ならトップレベルのみ）。
    """

    root = TreeNode(project_name, "project")
    directories: dict[str, TreeNode] = {"": root}

    def directory(path: str) -> TreeNode:
        if path in directories:
            return directories[path]
        parent = directory(str(PurePosixPath(path).parent) if "/" in path else "")
        node = TreeNode(PurePosixPath(path).name + "/", "directory", path)
        parent.children.append(node)
        directories[path] = node
        return node

    symbols_by_file: dict[str, list[Symbol]] = {}
    for symbol in index.symbols.values():
        if symbol.kind == SymbolKind.LOCAL_VARIABLE and not include_locals:
            continue
        if symbol.kind in _VARIABLE_KINDS and not include_variables:
            continue
        symbols_by_file.setdefault(symbol.file_id, []).append(symbol)

    for source_file in sorted(index.files.values(), key=lambda f: f.relative_path):
        parent_path = str(PurePosixPath(source_file.relative_path).parent)
        parent = directory("" if parent_path == "." else parent_path)
        status = None
        if source_file.analysis_status == AnalysisFileStatus.FAILED:
            status = "解析失敗"
        elif source_file.analysis_status != AnalysisFileStatus.ANALYZED:
            status = "未解析"
        file_node = TreeNode(
            PurePosixPath(source_file.relative_path).name,
            "file",
            source_file.relative_path,
            status=status,
        )
        parent.children.append(file_node)
        _attach_symbols(
            file_node,
            symbols_by_file.get(source_file.file_id, []),
            source_file.relative_path,
            max_symbol_depth,
        )

    _sort(root)
    return root


def _attach_symbols(
    file_node: TreeNode, symbols: list[Symbol], path: str, max_depth: int | None
) -> None:
    ordered = sorted(symbols, key=lambda s: (s.start_line, s.end_line))
    nodes: dict[str, TreeNode] = {}
    for symbol in ordered:
        if symbol.kind != SymbolKind.MODULE:  # ファイルノードと重複するため統合
            nodes[symbol.symbol_id] = TreeNode(symbol.name, symbol.kind.value, path, symbol.start_line)
    module_ids = {s.symbol_id for s in ordered if s.kind == SymbolKind.MODULE}
    for symbol in ordered:
        node = nodes.get(symbol.symbol_id)
        if node is None:
            continue
        parent_id = symbol.parent_symbol_id
        parent = nodes.get(parent_id) if parent_id and parent_id not in module_ids else None
        (parent or file_node).children.append(node)
    if max_depth is not None:
        _prune(file_node, max_depth)


def _prune(node: TreeNode, remaining: int) -> None:
    if remaining <= 0:
        node.children = []
        return
    for child in node.children:
        _prune(child, remaining - 1)


def _sort(node: TreeNode) -> None:
    # ディレクトリ、ファイルの順。シンボルは定義順(既にソート済み)を保つ。
    if node.kind in ("project", "directory"):
        node.children.sort(key=lambda c: (c.kind != "directory", c.label))
    for child in node.children:
        _sort(child)


def render_tree_text(root: TreeNode) -> str:
    lines = [root.label]

    def walk(node: TreeNode, prefix: str) -> None:
        for position, child in enumerate(node.children):
            last = position == len(node.children) - 1
            connector = "└── " if last else "├── "
            lines.append(prefix + connector + _describe(child))
            walk(child, prefix + ("    " if last else "│   "))

    walk(root, "")
    return "\n".join(lines) + "\n"


def _describe(node: TreeNode) -> str:
    if node.kind in ("directory", "file"):
        suffix = f"  [{node.status}]" if node.status else ""
        return node.label + suffix
    return f"{node.label}  ({node.kind}, L{node.line})"


def tree_to_dict(node: TreeNode) -> dict:
    return {
        "label": node.label,
        "kind": node.kind,
        "path": node.path,
        "line": node.line,
        "status": node.status,
        "children": [tree_to_dict(c) for c in node.children],
    }

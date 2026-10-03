from __future__ import annotations

import ast
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field

from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Language, Project, SourceFile, Symbol, SymbolKind


class OwnerLookup:
    """行番号から、それを含む最も内側のシンボルを返す。"""

    def __init__(self, symbols: list[Symbol]) -> None:
        self._symbols = sorted(
            (s for s in symbols if s.kind != SymbolKind.MODULE), key=lambda s: s.end_line - s.start_line
        )
        self._module = next((s for s in symbols if s.kind == SymbolKind.MODULE), None)

    def at(self, line: int) -> Symbol | None:
        for symbol in self._symbols:
            if symbol.start_line <= line <= symbol.end_line:
                return symbol
        return self._module

    def name_at(self, line: int) -> str:
        symbol = self.at(line)
        return symbol.qualified_name if symbol else ""


@dataclass
class ScannedFile:
    source_file: SourceFile
    text: str
    tree: ast.Module
    owner: OwnerLookup
    module_name: str  # モジュールの修飾名（ファイルのモジュールシンボル）


@dataclass
class ScanResult:
    skipped: list[str] = field(default_factory=list)  # 解析後に変更された・読めない・構文エラーで対象外にしたファイル


def iter_python_files(project: Project, index: ProjectIndex, result: ScanResult) -> Iterator[ScannedFile]:
    """解析済みのPythonファイルを、解析時と同じ内容であることを確認して順に返す。

    解析後に変更されている・読めない・構文エラーのファイルは、位置が対応しないため
    返さず、result.skipped に記録する。
    """

    by_file: dict[str, list[Symbol]] = {}
    for symbol in index.symbols.values():
        by_file.setdefault(symbol.file_id, []).append(symbol)
    for source_file in sorted(index.files.values(), key=lambda f: f.relative_path):
        if source_file.language != Language.PYTHON:
            continue
        try:
            data = (project.root_path / source_file.relative_path).read_bytes()
        except OSError:
            result.skipped.append(source_file.relative_path)
            continue
        if hashlib.sha256(data).hexdigest() != source_file.content_hash:
            result.skipped.append(source_file.relative_path)
            continue
        text = data.decode("utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            result.skipped.append(source_file.relative_path)
            continue
        symbols = by_file.get(source_file.file_id, [])
        module = next((s for s in symbols if s.kind == SymbolKind.MODULE), None)
        yield ScannedFile(source_file, text, tree, OwnerLookup(symbols), module.qualified_name if module else "")

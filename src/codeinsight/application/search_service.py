from __future__ import annotations

import enum
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import (
    AnalysisFileStatus,
    FileFreshness,
    Project,
    SourceFile,
    Symbol,
    SymbolKind,
)
from codeinsight.infrastructure.analysis_repository import AnalysisRepository


class MatchMode(enum.Enum):
    EXACT = "exact"
    PREFIX = "prefix"
    SUBSTRING = "substring"


@dataclass(frozen=True)
class SymbolHit:
    """構文解析に基づくシンボル検索の結果（文字列一致の結果とは区別する）。"""

    symbol: Symbol
    path: str

    @property
    def location(self) -> str:
        return f"{self.path}:{self.symbol.start_line}-{self.symbol.end_line}"


@dataclass(frozen=True)
class TextHit:
    """テキスト全文検索の結果。解析結果ではなく、現在のファイル内容の文字列一致。"""

    path: str
    line: int
    text: str
    freshness: FileFreshness


class SearchService:
    """ファイル名・シンボル・テキストの検索（3.4）。

    シンボル検索は解析結果（構文解析に基づく定義）を、テキスト検索は現在のファイル
    内容の文字列一致を対象とし、結果の型も分けて混同しないようにする。
    """

    def __init__(self, repository: AnalysisRepository) -> None:
        self._repository = repository

    def search_files(self, project: Project, query: str) -> list[SourceFile]:
        needle = query.lower()
        files = self._repository.list_source_files(project.project_id)
        return [f for f in files if needle in f.relative_path.lower()]

    def search_symbols(
        self,
        project: Project,
        query: str,
        *,
        kinds: Iterable[SymbolKind] | None = None,
        match: MatchMode = MatchMode.SUBSTRING,
        file: str | None = None,
        limit: int | None = None,
    ) -> list[SymbolHit]:
        index = ProjectIndex.load(self._repository, project.project_id)
        return search_symbols_in_index(index, query, kinds=kinds, match=match, file=file, limit=limit)

    def search_text(
        self,
        project: Project,
        query: str,
        *,
        ignore_case: bool = False,
        regex: bool = False,
        limit: int | None = None,
    ) -> list[TextHit]:
        flags = re.IGNORECASE if ignore_case else 0
        pattern = re.compile(query if regex else re.escape(query), flags)
        hits: list[TextHit] = []
        for source_file in self._repository.list_source_files(project.project_id):
            try:
                data = (project.root_path / source_file.relative_path).read_bytes()
            except OSError:
                continue
            freshness = _freshness(source_file, data)
            text = data.decode("utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line):
                    hits.append(TextHit(source_file.relative_path, number, line, freshness))
                    if limit is not None and len(hits) >= limit:
                        return hits
        return hits


def search_symbols_in_index(
    index: ProjectIndex,
    query: str,
    *,
    kinds: Iterable[SymbolKind] | None = None,
    match: MatchMode = MatchMode.SUBSTRING,
    file: str | None = None,
    limit: int | None = None,
) -> list[SymbolHit]:
    kind_filter = set(kinds) if kinds else None
    needle = query.lower()
    hits: list[SymbolHit] = []
    for symbol in index.symbols.values():
        if kind_filter is not None and symbol.kind not in kind_filter:
            continue
        path = index.path_of(symbol.file_id)
        if file is not None and path != file:
            continue
        if match == MatchMode.EXACT:
            matched = query in (symbol.name, symbol.qualified_name)
        elif match == MatchMode.PREFIX:
            matched = symbol.name.lower().startswith(needle) or symbol.qualified_name.lower().startswith(needle)
        else:
            matched = needle in symbol.qualified_name.lower()
        if matched:
            hits.append(SymbolHit(symbol, path))
    hits.sort(key=lambda h: (h.path, h.symbol.start_line, h.symbol.qualified_name))
    return hits[:limit] if limit is not None else hits


def _freshness(source_file: SourceFile, data: bytes) -> FileFreshness:
    if source_file.analysis_status != AnalysisFileStatus.ANALYZED:
        return FileFreshness.UNANALYZED
    if hashlib.sha256(data).hexdigest() == source_file.content_hash:
        return FileFreshness.FRESH
    return FileFreshness.STALE

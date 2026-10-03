from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from codeinsight.application.freshness_service import FreshnessService
from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.search_service import SymbolHit
from codeinsight.domain import (
    FileFreshness,
    Language,
    Project,
    ReferenceKind,
    ResolutionStatus,
    Symbol,
    SymbolKind,
)

_MEMBER_KINDS = frozenset(
    {
        SymbolKind.METHOD,
        SymbolKind.FUNCTION,
        SymbolKind.CLASS,
        SymbolKind.CLASS_VARIABLE,
        SymbolKind.GLOBAL_VARIABLE,
        SymbolKind.STATIC_VARIABLE,
        SymbolKind.STRUCT,
        SymbolKind.UNION,
        SymbolKind.ENUM,
        SymbolKind.TYPEDEF,
        SymbolKind.MACRO,
    }
)
_MAX_DECLARATION_LINES = 12


@dataclass(frozen=True)
class SymbolDescription:
    """1つのシンボルについて、静的解析で確認できた事実をまとめたもの。

    すべてソースまたは解析結果から得た事実（行番号付き）で、AIによる要約ではない。
    summary はソースのdocstring/ドキュメントコメントの先頭行の転記。
    """

    hit: SymbolHit
    freshness: FileFreshness
    declaration: list[tuple[int, str]]  # 宣言部分のソース行（行番号, 内容）
    members: list[Symbol]
    derived_classes: list[Symbol]
    callers: int  # 呼び出し元の数（解決済み）
    callees: dict[ResolutionStatus, int]  # 呼び出し先の数（解決状態別。クラスはメンバの合計）
    callees_include_members: bool
    references: dict[ReferenceKind, int]  # このシンボルを指す解決済み参照の数（種類別）
    referencing_files: int


class DescribeService:
    """シンボルの詳細（宣言・要約・メンバ・呼び出し/参照の件数）をまとめる（コードリーディング用）。"""

    def __init__(
        self, navigation: NavigationService, freshness: FreshnessService | None = None
    ) -> None:
        self._navigation = navigation
        self._freshness = freshness or FreshnessService()

    def describe(self, project: Project, index: ProjectIndex, symbol: Symbol) -> SymbolDescription:
        source_file = index.files[symbol.file_id]
        hit = SymbolHit(symbol, source_file.relative_path)
        freshness = self._freshness.check_file(project, source_file)
        declaration = _read_declaration(project, source_file.relative_path, symbol, source_file.language)

        members = sorted(
            (
                s
                for s in index.symbols.values()
                if s.parent_symbol_id == symbol.symbol_id and s.kind in _MEMBER_KINDS
            ),
            key=lambda s: s.start_line,
        )
        derived = sorted(
            {
                index.symbols[r.source_symbol_id].symbol_id: index.symbols[r.source_symbol_id]
                for r in index.references
                if r.reference_kind == ReferenceKind.INHERITANCE
                and r.target_symbol_id == symbol.symbol_id
                and r.source_symbol_id in index.symbols
            }.values(),
            key=lambda s: s.qualified_name,
        )

        callers = self._navigation.callers(index, symbol)
        # クラスは、自身よりメソッド内の呼び出しのほうが意味を持つため、メンバの分も合計する。
        sources = [symbol, *members] if symbol.kind in (SymbolKind.CLASS, SymbolKind.STRUCT) else [symbol]
        callees = Counter(
            h.reference.resolution_status
            for source in sources
            for h in self._navigation.callees(index, source)
        )
        incoming = self._navigation.references_to(index, symbol)
        return SymbolDescription(
            hit=hit,
            freshness=freshness,
            declaration=declaration,
            members=members,
            derived_classes=derived,
            callers=len(callers),
            callees=dict(callees),
            callees_include_members=len(sources) > 1,
            references=dict(Counter(h.reference.reference_kind for h in incoming)),
            referencing_files=len({h.path for h in incoming}),
        )


def _read_declaration(
    project: Project, relative_path: str, symbol: Symbol, language: Language
) -> list[tuple[int, str]]:
    """宣言（シグネチャ）部分のソース行を読む。括弧が閉じ、行末が `:` / `{` / `;` になるまで。"""

    root = project.root_path.resolve()
    path = (root / relative_path).resolve()
    if root not in path.parents:
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if symbol.kind == SymbolKind.MODULE:
        return []
    collected: list[tuple[int, str]] = []
    depth = 0
    for number in range(symbol.start_line, min(len(lines), symbol.end_line) + 1):
        text = lines[number - 1]
        collected.append((number, text.rstrip()))
        depth += sum(text.count(c) for c in "([") - sum(text.count(c) for c in ")]")
        stripped = text.split("#", 1)[0].rstrip() if language == Language.PYTHON else text.rstrip()
        if depth <= 0 and stripped.endswith((":", "{", ";")):
            break
        if len(collected) >= _MAX_DECLARATION_LINES:
            break
    if language == Language.PYTHON and symbol.kind in (SymbolKind.GLOBAL_VARIABLE, SymbolKind.CLASS_VARIABLE):
        return collected[:1]
    return collected

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.search_service import SymbolHit
from codeinsight.domain import (
    AnalysisFileStatus,
    Language,
    ReferenceKind,
    ResolutionStatus,
    SymbolKind,
)

_CALLABLE_KINDS = frozenset({SymbolKind.FUNCTION, SymbolKind.METHOD})
_ENTRY_NAMES = frozenset({"main"})


@dataclass(frozen=True)
class ModuleSummary:
    path: str
    summary: str  # モジュールのdocstring先頭行（ソースの転記）。無ければ空
    definitions: int  # クラス・関数・メソッドの数
    lines: int
    fan_in: int  # このファイルに依存しているファイル数
    fan_out: int  # このファイルが依存している（解決済みの）ファイル数


@dataclass(frozen=True)
class Ranked:
    hit: SymbolHit
    value: int


@dataclass
class Overview:
    """初めて扱うリポジトリの全体像（AGENTS.md §2.2-1）。静的解析で確認できた事実のみ。"""

    languages: dict[Language, int]
    symbol_counts: dict[SymbolKind, int]
    modules: list[ModuleSummary]
    entry_points: list[SymbolHit]
    most_called: list[Ranked]
    most_calling: list[Ranked]
    largest: list[Ranked]
    cycles: list[list[str]]
    reference_status: dict[ResolutionStatus, int]
    failed_files: list[str] = field(default_factory=list)


class OverviewService:
    def __init__(self, navigation: NavigationService) -> None:
        self._navigation = navigation

    def build(self, index: ProjectIndex, top: int = 10) -> Overview:
        symbols = index.symbols

        languages = Counter(f.language for f in index.files.values())
        symbol_counts = Counter(
            s.kind for s in symbols.values() if s.kind != SymbolKind.LOCAL_VARIABLE
        )

        fan_in: Counter[str] = Counter()
        fan_out: dict[str, set[str]] = defaultdict(set)
        for dependency in index.dependencies:
            if (
                dependency.resolution_status == ResolutionStatus.RESOLVED
                and dependency.target_file_id
                and dependency.target_file_id != dependency.source_file_id
            ):
                fan_out[dependency.source_file_id].add(dependency.target_file_id)
        for targets in fan_out.values():
            fan_in.update(targets)

        definitions: Counter[str] = Counter()
        module_summary: dict[str, str] = {}
        lines: dict[str, int] = defaultdict(int)
        for symbol in symbols.values():
            lines[symbol.file_id] = max(lines[symbol.file_id], symbol.end_line)
            if symbol.kind == SymbolKind.MODULE:
                module_summary[symbol.file_id] = symbol.summary
            elif symbol.kind in (SymbolKind.CLASS, SymbolKind.FUNCTION, SymbolKind.METHOD):
                definitions[symbol.file_id] += 1
        modules = [
            ModuleSummary(
                path=f.relative_path,
                summary=module_summary.get(fid, ""),
                definitions=definitions[fid],
                lines=lines[fid],
                fan_in=fan_in[fid],
                fan_out=len(fan_out[fid]),
            )
            for fid, f in index.files.items()
        ]
        modules.sort(key=lambda m: (-m.fan_in, -m.definitions, m.path))

        entry_points = sorted(
            (
                SymbolHit(s, index.path_of(s.file_id))
                for s in symbols.values()
                if (s.kind == SymbolKind.FUNCTION and s.name in _ENTRY_NAMES)
                or (s.kind == SymbolKind.MODULE and index.path_of(s.file_id).endswith("__main__.py"))
            ),
            key=lambda h: (h.path, h.symbol.start_line),
        )

        callers: dict[str, set[str]] = defaultdict(set)
        callees: dict[str, set[str]] = defaultdict(set)
        for reference in index.references:
            if (
                reference.reference_kind == ReferenceKind.CALL
                and reference.resolution_status == ResolutionStatus.RESOLVED
                and reference.target_symbol_id
                and reference.target_symbol_id != reference.source_symbol_id
            ):
                callers[reference.target_symbol_id].add(reference.source_symbol_id)
                callees[reference.source_symbol_id].add(reference.target_symbol_id)

        def ranked(values: dict[str, set[str]]) -> list[Ranked]:
            order = sorted(values.items(), key=lambda kv: (-len(kv[1]), symbols[kv[0]].qualified_name))
            return [
                Ranked(SymbolHit(symbols[sid], index.path_of(symbols[sid].file_id)), len(group))
                for sid, group in order
                if sid in symbols
            ][:top]

        largest = sorted(
            (s for s in symbols.values() if s.kind in _CALLABLE_KINDS | {SymbolKind.CLASS}),
            key=lambda s: (-(s.end_line - s.start_line + 1), s.qualified_name),
        )[:top]

        return Overview(
            languages=dict(languages),
            symbol_counts=dict(symbol_counts),
            modules=modules[:top],
            entry_points=entry_points,
            most_called=ranked(callers),
            most_calling=ranked(callees),
            largest=[
                Ranked(SymbolHit(s, index.path_of(s.file_id)), s.end_line - s.start_line + 1)
                for s in largest
            ],
            cycles=self._navigation.dependency_cycles(index),
            reference_status=dict(Counter(r.resolution_status for r in index.references)),
            failed_files=sorted(
                f.relative_path
                for f in index.files.values()
                if f.analysis_status == AnalysisFileStatus.FAILED
            ),
        )

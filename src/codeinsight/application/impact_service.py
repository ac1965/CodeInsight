from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from codeinsight.application.architecture_service import component_of
from codeinsight.application.paths import is_test_path
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import ReferenceKind, ResolutionStatus, Symbol, SymbolKind

_USES = (ReferenceKind.CALL, ReferenceKind.NAME_REF, ReferenceKind.FUNCTION_REF, ReferenceKind.INHERITANCE, ReferenceKind.TYPE_USE)


@dataclass
class Affected:
    symbol: Symbol
    path: str
    distance: int
    route: list[str]  # 影響を受ける側から、対象へ向かう経路
    is_test: bool


@dataclass
class ImpactReport:
    symbol: Symbol
    affected: list[Affected] = field(default_factory=list)
    entry_points: list[Affected] = field(default_factory=list)  # 入口（エントリポイント・CLI・HTTP・イベント等）に届くもの
    files: set[str] = field(default_factory=set)
    components: set[str] = field(default_factory=set)
    unresolved_callers: int = 0  # 同じ名前を呼ぶが解決できず、影響を追えていない参照の数


class ImpactService:
    """シンボルを変更したとき、静的に確認できる範囲で何に影響するか（利用者側への波及）を調べる。

    解決済みの呼び出し・参照・継承を逆向きにたどる。動的な呼び出し・リフレクション・外部からの利用
    （公開API）は含まれない。名前が同じで解決できなかった参照は、件数のみ示す。
    """

    def impact(
        self,
        index: ProjectIndex,
        symbol: Symbol,
        depth: int = 4,
        entry_symbol_ids: set[str] | None = None,
        component_depth: int = 3,
    ) -> ImpactReport:
        incoming: dict[str, list] = {}
        for reference in index.references:
            if (
                reference.reference_kind in _USES
                and reference.resolution_status == ResolutionStatus.RESOLVED
                and reference.target_symbol_id
                and reference.source_symbol_id != reference.target_symbol_id
            ):
                incoming.setdefault(reference.target_symbol_id, []).append(reference)

        report = ImpactReport(symbol)
        ids = {symbol.symbol_id}
        if symbol.usr:
            ids |= {s.symbol_id for s in index.symbols.values() if s.usr == symbol.usr}
        queue: deque[tuple[str, int, list[str]]] = deque((sid, 0, [symbol.qualified_name]) for sid in ids)
        seen = set(ids)
        entry_ids = entry_symbol_ids or set()
        while queue:
            current, distance, route = queue.popleft()
            if distance >= depth:
                continue
            for reference in incoming.get(current, []):
                source = index.symbols.get(reference.source_symbol_id)
                if source is None or source.symbol_id in seen:
                    continue
                seen.add(source.symbol_id)
                step = [source.qualified_name, *route]
                path = index.path_of(source.file_id)
                affected = Affected(source, path, distance + 1, step, is_test_path(path))
                report.affected.append(affected)
                report.files.add(path)
                report.components.add(component_of(path, component_depth))
                if source.symbol_id in entry_ids or (source.kind == SymbolKind.FUNCTION and source.name == "main"):
                    report.entry_points.append(affected)
                queue.append((source.symbol_id, distance + 1, step))
        report.unresolved_callers = sum(
            1
            for r in index.references
            if r.reference_kind == ReferenceKind.CALL
            and r.resolution_status in (ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS)
            and r.target_name.rsplit(".", 1)[-1] == symbol.name
        )
        report.affected.sort(key=lambda a: (a.distance, a.path, a.symbol.qualified_name))
        return report

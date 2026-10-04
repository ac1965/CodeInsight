"""呼び出しグラフの範囲から、該当する関数のソースを切り出す（コードリーディング用）。

起点の関数と、そこから呼び出し先（または呼び出し元）を深さまでたどった関数の本体を、根拠位置・解決状態つきで集める。
解決できた呼び出しだけをたどり、未解決・曖昧・外部の呼び出しは、ソースを出さず、その旨だけを示す（推測しない）。
解析後にファイルが変更されている場合は、行の位置が対応しないため、ソースを出さない。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from codeinsight.application.navigation_service import NavigationService, SymbolNotFoundError
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Confidence, FileFreshness, Project, ResolutionStatus, Symbol, SymbolKind

_CALLABLE = (SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.FUNCTION_DECLARATION, SymbolKind.MACRO)
DIRECTIONS = ("callees", "callers", "both")


@dataclass
class ExtractedItem:
    symbol: Symbol
    path: str
    role: str  # root / callee / caller
    depth: int
    lines: list[tuple[int, str]] = field(default_factory=list)  # 本体（行番号, 行）。出せない場合は空
    total_lines: int = 0
    omitted_reason: str = ""  # ソースを出さなかった理由（古い・宣言のみ・行数の上限など）
    inferred: bool = False  # 解決が「推定」の呼び出しを経て到達した
    via: str = ""  # 親からこの関数への呼び出しの位置（path:line）


@dataclass(frozen=True)
class OmittedCall:
    source: str  # 呼び出し元の修飾名
    name: str
    status: str  # unresolved / ambiguous / external
    location: str
    note: str = ""


@dataclass
class ExtractResult:
    root: ExtractedItem
    items: list[ExtractedItem] = field(default_factory=list)  # 起点を除く。深さ・パス・行の順
    omitted_calls: list[OmittedCall] = field(default_factory=list)
    stale_files: list[str] = field(default_factory=list)
    truncated: bool = False  # 件数の上限で打ち切った
    depth: int = 1
    direction: str = "callees"


class ExtractService:
    def __init__(self, navigation: NavigationService) -> None:
        self._navigation = navigation

    def extract(
        self, project: Project, index: ProjectIndex, root: Symbol, direction: str = "callees", depth: int = 1,
        max_items: int = 30, max_lines: int = 200,
    ) -> ExtractResult:
        if direction not in DIRECTIONS:
            raise ValueError(f"direction は {', '.join(DIRECTIONS)} のいずれかです")
        result = ExtractResult(self._item(project, index, root, "root", 0, max_lines), depth=depth, direction=direction)
        seen = {root.symbol_id}
        queue: deque[tuple[Symbol, int]] = deque([(root, 0)])
        omitted: dict[tuple[str, str, str, str], OmittedCall] = {}
        found: list[ExtractedItem] = []
        while queue:
            current, level = queue.popleft()
            if level >= depth:
                continue
            steps: list[tuple[str, object]] = []
            if direction in ("callees", "both"):
                steps += [("callee", hit) for hit in self._navigation.callees(index, current)]
            if direction in ("callers", "both"):
                steps += [("caller", hit) for hit in self._navigation.callers(index, current)]
            for role, hit in steps:
                reference = hit.reference  # type: ignore[attr-defined]
                neighbour = hit.target if role == "callee" else hit.source  # type: ignore[attr-defined]
                if neighbour is None or reference.resolution_status != ResolutionStatus.RESOLVED:
                    if role == "callee":
                        key = (current.qualified_name, reference.target_name, reference.resolution_status.value, hit.location)  # type: ignore[attr-defined]
                        omitted.setdefault(key, OmittedCall(*key, note=reference.note))
                    continue
                if neighbour.symbol_id in seen or neighbour.kind not in _CALLABLE:
                    continue
                if len(found) >= max_items:
                    result.truncated = True
                    continue
                seen.add(neighbour.symbol_id)
                item = self._item(project, index, neighbour, role, level + 1, max_lines)
                item.inferred = reference.confidence == Confidence.INFERRED
                item.via = hit.location  # type: ignore[attr-defined]
                found.append(item)
                queue.append((neighbour, level + 1))
        result.items = sorted(found, key=lambda i: (i.depth, i.role, i.path, i.symbol.start_line))
        result.omitted_calls = sorted(omitted.values(), key=lambda o: (o.source, o.location))
        result.stale_files = sorted({i.path for i in [result.root, *result.items] if i.omitted_reason.startswith("古い")})
        return result

    def _item(self, project: Project, index: ProjectIndex, symbol: Symbol, role: str, depth: int, max_lines: int) -> ExtractedItem:
        path = index.path_of(symbol.file_id)
        item = ExtractedItem(symbol, path, role, depth, total_lines=symbol.end_line - symbol.start_line + 1)
        if symbol.kind == SymbolKind.FUNCTION_DECLARATION:
            item.omitted_reason = "宣言のみ（定義はプロジェクト内で確認できていません）"
        try:
            view = self._navigation.show_source(project, index, path, symbol.start_line, symbol.end_line, 0)
        except (SymbolNotFoundError, OSError) as exc:
            item.omitted_reason = f"ソースを読み込めません: {exc}"
            return item
        if view.freshness != FileFreshness.FRESH:
            label = {"stale": "解析後に変更されています", "missing": "ファイルが存在しません", "unanalyzed": "解析に成功していません"}[view.freshness.value]
            item.omitted_reason = f"古い: {path} は{label}。行の位置が対応しないため、ソースを出していません（再解析してください）"
            return item
        if item.omitted_reason:
            item.lines = view.lines[:max_lines]  # 宣言は、その行だけ
            return item
        item.lines = view.lines[:max_lines]
        if len(view.lines) > max_lines:
            item.omitted_reason = f"本体が{len(view.lines)}行あり、先頭の{max_lines}行だけを出しています"
        return item

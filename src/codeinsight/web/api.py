"""WebビューアーのAPI。HTTPには依存せず、(パス, クエリ) から辞書を返す。すべて読み取り専用。

入力は信頼できないものとして、種類・長さ・範囲を検査する。ソースの取得は、解析対象のファイル（プロジェクトのルートの中）に限る。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from codeinsight.application import NavigationService
from codeinsight.application.extract_service import DIRECTIONS, ExtractService
from codeinsight.application.freshness_service import FreshnessService
from codeinsight.application.graph_builder import Traversal
from codeinsight.application.graph_service import GRAPH_KINDS, GraphRequest, GraphRequestError, GraphService
from codeinsight.application.navigation_service import AmbiguousSymbolError, SymbolNotFoundError
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.search_service import search_symbols_in_index
from codeinsight.domain import FileFreshness, Project
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.presentation import extract_export
from codeinsight.presentation.graph_export import model_to_dict

MAX_TEXT = 300
MAX_LINES = 2000


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra: object) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


Params = dict[str, list[str]]


def _text(params: Params, name: str, default: str | None = None, required: bool = False) -> str | None:
    values = params.get(name)
    if not values or values[0] == "":
        if required:
            raise ApiError(400, f"パラメータ {name} が必要です")
        return default
    value = values[0]
    if len(value) > MAX_TEXT:
        raise ApiError(400, f"パラメータ {name} が長すぎます（{MAX_TEXT}文字まで）")
    return value


def _int(params: Params, name: str, default: int | None, low: int, high: int) -> int | None:
    raw = _text(params, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(400, f"パラメータ {name} は整数で指定してください") from exc
    if not low <= value <= high:
        raise ApiError(400, f"パラメータ {name} は {low}〜{high} で指定してください")
    return value


def _flag(params: Params, name: str, default: bool = False) -> bool:
    raw = _text(params, name)
    return default if raw is None else raw in ("1", "true", "yes")


class ViewerApi:
    def __init__(self, repository: AnalysisRepository, project: Project) -> None:
        self._repository = repository
        self._project = project
        self._navigation = NavigationService(repository)
        self.refresh()
        self._routes: dict[str, Callable[[Params], dict]] = {
            "/api/project": self._project_info,
            "/api/symbols": self._symbols,
            "/api/graph": self._graph,
            "/api/source": self._source,
            "/api/extract": self._extract,
            "/api/refresh": self._refresh,
        }

    def refresh(self) -> None:
        """DBと、解析後に変更されたファイルを読み直す（読み取りのみ）。"""

        self._index: ProjectIndex = self._navigation.load_index(self._project)
        states = FreshnessService().check_project(self._project, list(self._index.files.values()))
        self._stale = sorted(self._index.files[fid].relative_path for fid, state in states.items() if state in (FileFreshness.STALE, FileFreshness.MISSING))

    def handle(self, path: str, params: Params) -> dict:
        route = self._routes.get(path)
        if route is None:
            raise ApiError(404, "存在しないAPIです")
        return route(params)

    # --- 各API ---

    def _refresh(self, params: Params) -> dict:
        self.refresh()
        return {"stale_files": self._stale}

    def _project_info(self, params: Params) -> dict:
        languages = Counter(f.language.value for f in self._index.files.values())
        return {
            "name": self._project.name, "root_path": str(self._project.root_path), "repository_revision": self._project.repository_revision,
            "files": len(self._index.files), "symbols": len(self._index.symbols), "languages": dict(languages),
            "stale_files": self._stale[:50], "stale_count": len(self._stale), "graph_kinds": list(GRAPH_KINDS), "directions": list(DIRECTIONS),
        }

    def _symbols(self, params: Params) -> dict:
        query = _text(params, "q", required=True) or ""
        limit = _int(params, "limit", 30, 1, 100) or 30
        hits = search_symbols_in_index(self._index, query, limit=None)
        callable_first = sorted(hits, key=lambda h: (h.symbol.kind.value not in ("function", "method", "class"), len(h.symbol.qualified_name)))[:limit]
        return {"symbols": [
            {"name": h.symbol.name, "qualified_name": h.symbol.qualified_name, "kind": h.symbol.kind.value, "path": h.path,
             "start_line": h.symbol.start_line, "end_line": h.symbol.end_line}
            for h in callable_first
        ], "total": len(hits)}

    def _resolve(self, params: Params, name: str = "root", required: bool = True):
        query = _text(params, name, required=required)
        if query is None:
            return None
        try:
            return self._navigation.resolve_symbol(self._index, query, file=_text(params, "file")).symbol
        except SymbolNotFoundError as exc:
            raise ApiError(404, f"シンボルが見つかりません: {query}") from exc
        except AmbiguousSymbolError as exc:
            raise ApiError(
                409, f"'{query}' は複数のシンボルに一致します。修飾名・file・`名前@行番号` で絞り込んでください",
                candidates=[{"qualified_name": h.symbol.qualified_name, "path": h.path, "start_line": h.symbol.start_line} for h in exc.candidates[:20]],
            ) from exc

    def _graph(self, params: Params) -> dict:
        kind = _text(params, "kind", "call") or "call"
        if kind not in GRAPH_KINDS:
            raise ApiError(400, f"kind は {', '.join(GRAPH_KINDS)} のいずれかです")
        direction = _text(params, "direction", "both") or "both"
        if direction not in ("in", "out", "both"):
            raise ApiError(400, "direction は in / out / both のいずれかです")
        root_symbol = self._resolve(params, required=kind == "flow") if kind in ("call", "inherit", "flow") else None
        root_path = _text(params, "root") if kind == "deps" else None
        request = GraphRequest(kind, root_symbol, root_path, _int(params, "depth", None, 0, 10), Traversal(direction),
                               include_external=_flag(params, "external"), include_unresolved=_flag(params, "unresolved", True))
        try:
            model = GraphService(self._navigation).build(self._project, self._index, request)
        except GraphRequestError as exc:
            raise ApiError(400, str(exc)) from exc
        model = GraphService.annotate(model, self._project, self._stale)
        if len(model.nodes) > 2000:
            raise ApiError(413, f"ノードが{len(model.nodes)}個あり、多すぎます。root と depth で絞り込んでください")
        return model_to_dict(model)

    def _source(self, params: Params) -> dict:
        path = _text(params, "path", required=True) or ""
        start = _int(params, "start", None, 1, 10_000_000)
        end = _int(params, "end", start, 1, 10_000_000)
        context = _int(params, "context", 3, 0, 50) or 0
        if start is not None and end is not None and end < start:
            raise ApiError(400, "end は start 以上で指定してください")
        try:
            view = self._navigation.show_source(self._project, self._index, path, start, end, context)
        except SymbolNotFoundError as exc:
            raise ApiError(404, "解析対象のファイルではありません") from exc  # 解析対象外・ルートの外のパスは読まない
        except OSError as exc:
            raise ApiError(404, "ファイルを読み込めません") from exc
        lines = view.lines[:MAX_LINES]
        return {
            "path": view.path, "freshness": view.freshness.value, "highlight_start": view.highlight_start, "highlight_end": view.highlight_end,
            "truncated": len(view.lines) > MAX_LINES, "lines": [{"n": n, "text": t} for n, t in lines],
        }

    def _extract(self, params: Params) -> dict:
        root = self._resolve(params)
        direction = _text(params, "direction", "callees") or "callees"
        if direction not in DIRECTIONS:
            raise ApiError(400, f"direction は {', '.join(DIRECTIONS)} のいずれかです")
        result = ExtractService(self._navigation).extract(
            self._project, self._index, root, direction, _int(params, "depth", 1, 0, 6) or 0,
            _int(params, "max_items", 30, 1, 100) or 30, _int(params, "max_lines", 200, 1, 1000) or 200,
        )
        return {"markdown": extract_export.to_markdown(result), "data": extract_export.to_dict(result)}

"""WebビューアーのAPI。HTTPには依存せず、(パス, クエリ) から辞書を返す。すべて読み取り専用。

入力は信頼できないものとして、種類・長さ・範囲を検査する。ソースの取得は、解析対象のファイル（プロジェクトのルートの中）に限る。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeinsight.application import NavigationService
from codeinsight.application.boundary_service import KIND_LABELS as BOUNDARY_LABELS
from codeinsight.application.boundary_service import BoundaryService
from codeinsight.application.c_flow_service import C_FAMILY, CFlowService
from codeinsight.application.config_service import KIND_LABELS as CONFIG_LABELS
from codeinsight.application.config_service import ConfigService
from codeinsight.application.external_binary_service import ExternalBinaryService
from codeinsight.application.external_findings_service import ExternalFindingsService
from codeinsight.application.external_index_service import ExternalIndexService
from codeinsight.application.extract_service import DIRECTIONS, ExtractService
from codeinsight.application.flow_service import FlowAnalysisError
from codeinsight.application.freshness_service import FreshnessService
from codeinsight.application.graph_builder import Traversal
from codeinsight.application.graph_service import GRAPH_KINDS, GraphRequest, GraphRequestError, GraphService
from codeinsight.application.navigation_service import AmbiguousSymbolError, SymbolNotFoundError
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.search_service import search_symbols_in_index
from codeinsight.application.understand_service import UnderstandService
from codeinsight.domain import Confidence, FileFreshness, Project, ReferenceKind, ResolutionStatus
from codeinsight.dynamic.diff import NOTE as NOTE_DYNAMIC
from codeinsight.dynamic.diff import compare as compare_with_static
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.mcp.serialize import to_plain
from codeinsight.presentation import extract_export
from codeinsight.presentation.graph_export import model_to_dict

MAX_TEXT = 300
MAX_LINES = 2000


@dataclass(frozen=True)
class BinaryResponse:
    content_type: str
    data: bytes


GUIDE_DIR = Path(__file__).resolve().parent / "guide"
READING_EXTENSIONS = {".md": "markdown", ".txt": "text", ".mmd": "text", ".dot": "text"}
MAX_FILE_BYTES = 2 * 1024 * 1024
_READING_TITLES = {
    "README.md": ("目次", 0), "overview.txt": ("全体像", 1), "architecture.txt": ("アーキテクチャ（層・循環・外部連携）", 2), "boundaries.txt": ("入口と境界", 3),
    "config.txt": ("設定値", 4), "externals.txt": ("外部連携", 5), "environment.txt": ("実行環境", 6), "risks.txt": ("リスクの手がかり", 7),
    "tests-untested.txt": ("テストが届いていない関数", 8), "unused.txt": ("未使用のコード", 9), "history.txt": ("履歴", 10), "docs-check.txt": ("文書とコードの差分", 11),
    "analysis/unresolved.txt": ("未解決の関係", 12), "analysis/status.txt": ("解析状況", 13),
}
_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".svg": "image/svg+xml"}


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
    def __init__(self, repository: AnalysisRepository, project: Project, reading_dir: Path | None = None) -> None:
        self._repository = repository
        self._project = project
        self._reading_dir = reading_dir.resolve() if reading_dir is not None else None
        self._navigation = NavigationService(repository)
        self.refresh()
        self._routes: dict[str, Callable[[Params], dict | BinaryResponse]] = {
            "/api/project": self._project_info,
            "/api/symbols": self._symbols,
            "/api/graph": self._graph,
            "/api/source": self._source,
            "/api/extract": self._extract,
            "/api/refresh": self._refresh,
            "/api/reading": self._reading,
            "/api/reading/file": self._reading_file,
            "/api/guide": self._guide,
            "/api/guide/image": self._guide_image,
            "/api/card": self._card,
            "/api/marks": self._marks,
            "/api/unresolved": self._unresolved,
            "/api/fields": self._fields,
            "/api/config": self._config,
            "/api/boundaries": self._boundaries,
            "/api/status": self._status,
            "/api/inferences": self._inferences,
            "/api/inference": self._inference,
            "/api/external": self._external,
            "/api/dynamic": self._dynamic,
            "/api/dynamic-diff": self._dynamic_diff,
        }

    def refresh(self) -> None:
        """DBと、解析後に変更されたファイルを読み直す（読み取りのみ）。"""

        self._index: ProjectIndex = self._navigation.load_index(self._project)
        self._cache: dict[str, object] = {}  # 重い走査（設定値・境界・欄外の印）の結果。再読み込みで捨てる
        states = FreshnessService().check_project(self._project, list(self._index.files.values()))
        self._stale = sorted(self._index.files[fid].relative_path for fid, state in states.items() if state in (FileFreshness.STALE, FileFreshness.MISSING))

    def handle(self, path: str, params: Params) -> dict | BinaryResponse:
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
        data = model_to_dict(model)
        overlays = {o for o in (_text(params, "overlay") or "").split(",") if o in ("observed", "ai")}
        if overlays and kind == "call":
            self._overlay(data, overlays)
        return data

    def _hashes_current(self, hashes: dict[str, str]) -> bool:
        files = {p: self._index.file_by_path(p) for p in hashes}
        return all(f is not None and f.content_hash == h for (p, h), f in ((item, files[item[0]]) for item in hashes.items()))

    def _observed_edges(self) -> tuple[dict[tuple[str, str], int], str | None]:
        """最新の動的解析の実行（完了・異常終了。実行後にソースが変わっていないもの）で観測された呼び出しの辺。解析結果とは別の区分。"""

        for run in self._repository.list_dynamic_runs(self._project.project_id):
            if run.status.value not in ("completed", "failed"):
                continue
            if not self._hashes_current(run.source_hashes):
                continue  # 実行後に変わったソース: 古い観測は、重ねない
            counts: dict[tuple[str, str], int] = {}
            for observation in self._repository.list_dynamic_observations(run.run_id):
                if observation.kind.value == "call" and observation.symbol_id and observation.target_symbol_id:
                    key = (observation.symbol_id, observation.target_symbol_id)
                    counts[key] = counts.get(key, 0) + observation.count
            return counts, run.run_id
        return {}, None

    def _ai_edges(self) -> list[tuple[str, str, str, str]]:
        """保存済みで、根拠が変わっていないAIの推測から、「関数 → 推測された呼び出し先」（【推論】の行に、バッククォートで書かれた、プロジェクトの関数）。解析結果ではない。"""

        by_name: dict[str, list] = defaultdict(list)
        for symbol in self._index.symbols.values():
            if symbol.kind.value in ("function", "method"):
                by_name[symbol.name].append(symbol)
                if symbol.qualified_name != symbol.name:
                    by_name[symbol.qualified_name].append(symbol)
        by_qualified = {s.qualified_name: s for s in self._index.symbols.values()}
        edges: list[tuple[str, str, str, str]] = []
        seen: set[str] = set()
        for explanation in self._repository.list_explanations(self._project.project_id, "inference"):
            if explanation.target in seen:
                continue
            seen.add(explanation.target)
            caller = by_qualified.get(explanation.target)
            if caller is None or not self._hashes_current(explanation.source_hashes):
                continue
            candidates = set()
            for line in explanation.text.splitlines():
                if "【推論】" in line:
                    candidates |= set(re.findall(r"`([A-Za-z_][\w:.]*)`", line))
            for name in sorted(candidates):
                found = by_name.get(name, [])
                if len(found) == 1 and found[0].symbol_id != caller.symbol_id:
                    edges.append((caller.symbol_id, found[0].symbol_id, explanation.explanation_id, explanation.status.value))
        return edges

    def _overlay(self, data: dict, overlays: set[str]) -> None:
        """グラフの辺に、観測・AIの推測を重ねる（解析結果の辺とは別の区分。混ぜない）。どちらも、グラフに含まれるノードの間の辺だけ。"""

        ids = {n["id"] for n in data["nodes"]}
        notes: list[str] = []
        existing = {(e["source"], e["target"]): e for e in data["edges"] if e["kind"] == "call"}
        if "observed" in overlays:
            counts, run_id = self._observed_edges()
            if run_id is None:
                notes.append("重ねられる観測がありません（動的解析の実行が無い、または実行後にソースが変わっています）。")
            for (source, target), count in counts.items():
                if (source, target) in existing:
                    existing[(source, target)]["observed"] = count
                elif source in ids and target in ids:
                    data["edges"].append({"source": source, "target": target, "kind": "call", "style": "observed_only", "label": "", "evidence": [], "count": count, "observed": count,
                                          "note": "観測のみ: この実行で観測されたが、静的な確定済みの辺には無い。観測は、この1回の実行で起きたことの記録です。"})
            notes.append("観測は、この1回の実行で起きたことの記録です。観測されなかった辺は、呼ばれないことを意味しません。")
        if "ai" in overlays:
            added = 0
            for source, target, inference_id, status in self._ai_edges():
                if source in ids and target in ids and (source, target) not in existing:
                    data["edges"].append({"source": source, "target": target, "kind": "call", "style": "ai", "label": "AI", "evidence": [], "count": 1,
                                          "note": f"AIの推測（確定ではありません。検証状態: {status}。推測ID: {inference_id[:8]}）。解析結果の解決状態は、変わりません。"})
                    added += 1
            notes.append(f"AIの推測の辺: {added}本（確定ではない。解析の辺とは別の区分）。")
        data["overlay"] = {"observed": "observed" in overlays, "ai": "ai" in overlays, "notes": notes}

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

    # --- 資料（make reading の成果物）・使い方 ---

    def _reading_files(self) -> list[dict]:
        assert self._reading_dir is not None
        root = self._reading_dir
        found: list[dict] = []
        for path in sorted(root.rglob("*")):
            try:
                relative = path.relative_to(root).as_posix()
                if not path.is_file() or path.is_symlink() or path.suffix.lower() not in READING_EXTENSIONS:
                    continue
                if relative.startswith(("logs/", ".")) or "/." in relative or path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            title, order = _READING_TITLES.get(relative, (None, 100))
            group = "資料"
            if relative.startswith("functions/"):
                group, title, order = "主要な関数の読解カード", path.stem, 200
            elif relative.startswith("ai/"):
                group, title, order = "AIによる解説（解析結果ではありません）", path.stem, 300
            elif relative.startswith("graphs/"):
                group, title, order = "図の元データ（Mermaid・DOT）", relative.removeprefix("graphs/"), 400
            found.append({"name": relative, "title": title or relative, "group": group, "kind": READING_EXTENSIONS[path.suffix.lower()], "order": order})
        return sorted(found, key=lambda f: (f["order"], f["name"]))

    def _reading(self, params: Params) -> dict:
        if self._reading_dir is None or not self._reading_dir.is_dir():
            return {"available": False, "files": [], "message": "資料の出力先が指定されていません（make reading で起動するか、--reading-dir を指定してください）"}
        return {"available": True, "files": self._reading_files()}

    def _reading_file(self, params: Params) -> dict:
        if self._reading_dir is None:
            raise ApiError(404, "資料の出力先が指定されていません")
        name = _text(params, "name", required=True) or ""
        listed = {f["name"]: f for f in self._reading_files()}
        entry = listed.get(name)  # 一覧にあるものだけを読む（パスの指定を、直接ファイルに使わない）
        if entry is None:
            raise ApiError(404, "資料が見つかりません")
        path = (self._reading_dir / name).resolve()
        if self._reading_dir not in path.parents:
            raise ApiError(404, "資料が見つかりません")
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            raise ApiError(404, "資料を読み込めません") from exc
        return {"name": name, "title": entry["title"], "kind": entry["kind"], "text": text}

    def _guide(self, params: Params) -> dict:
        path = GUIDE_DIR / "VIEWER.md"
        if not path.is_file():
            return {"available": False, "text": "", "images": []}
        images = sorted(p.name for p in (GUIDE_DIR / "images").glob("*") if p.suffix.lower() in _IMAGE_TYPES) if (GUIDE_DIR / "images").is_dir() else []
        return {"available": True, "text": path.read_text(encoding="utf-8"), "images": images}

    def _guide_image(self, params: Params) -> dict | BinaryResponse:
        name = _text(params, "name", required=True) or ""
        directory = GUIDE_DIR / "images"
        path = directory / name
        if "/" in name or "\\" in name or path.suffix.lower() not in _IMAGE_TYPES or not path.is_file() or path.is_symlink():
            raise ApiError(404, "画像が見つかりません")
        return BinaryResponse(_IMAGE_TYPES[path.suffix.lower()], path.read_bytes())

    # --- 読解（読み取り専用。解析結果の表示だけ） ---

    def _card(self, params: Params) -> dict:
        """関数の読解カード（8つの問い）。確定・推定・未解決は、項目ごとの resolution・confidence で区別できる。"""

        symbol = self._resolve(params, "symbol")
        try:
            card = UnderstandService(self._navigation).understand(self._project, self._index, symbol, _int(params, "depth", 3, 1, 4) or 3)
        except FlowAnalysisError as exc:
            raise ApiError(422, str(exc)) from exc
        data = to_plain(card, True)  # ビューアーは、ソースを表示できるため、断片も含める
        data["note"] = "確認できた事実だけの読解カードです。resolution・confidence が inferred・unresolved のものは、確定ではありません。limitations に、確認できなかったこと・対応範囲外のことを示します。"
        return data

    def _marks_by_file(self) -> dict[str, list[dict]]:
        cached = self._cache.get("marks")
        if cached is not None:
            return cached  # type: ignore[return-value]
        marks: dict[str, list[dict]] = defaultdict(list)
        for reference in self._index.references:
            if reference.reference_kind != ReferenceKind.CALL:
                continue
            if reference.resolution_status == ResolutionStatus.UNRESOLVED:
                kind = "unresolved"
            elif reference.resolution_status == ResolutionStatus.AMBIGUOUS:
                kind = "ambiguous"
            elif reference.resolution_status == ResolutionStatus.RESOLVED and reference.confidence == Confidence.INFERRED:
                kind = "inferred"
            else:
                continue
            source = self._index.symbols.get(reference.source_symbol_id)
            path = self._index.path_of(reference.source_location.file_id)
            marks[path].append({
                "line": reference.source_location.start_line, "kind": kind, "label": reference.target_name, "detail": reference.note,
                "function": source.qualified_name if source else "",
            })
        for items in marks.values():
            items.sort(key=lambda m: (m["line"], m["label"]))
        self._cache["marks"] = dict(marks)
        return self._cache["marks"]  # type: ignore[return-value]

    def _marks(self, params: Params) -> dict:
        """ソースの欄外に出す印（未解決・曖昧・推定の呼び出し）。解析結果に保存された事実だけで、ファイルは読まない。"""

        path = _text(params, "path", required=True) or ""
        if self._index.file_by_path(path) is None:
            raise ApiError(404, "解析対象のファイルではありません")
        return {"path": path, "stale": path in self._stale, "marks": self._marks_by_file().get(path, [])[:2000]}

    def _inference_by_target(self) -> dict[str, dict]:
        found: dict[str, dict] = {}
        for explanation in self._repository.list_explanations(self._project.project_id, "inference"):  # 新しい順
            if explanation.target in found:
                continue
            stale = [p for p, h in explanation.source_hashes.items() if (self._index.file_by_path(p) is None or self._index.file_by_path(p).content_hash != h)]  # type: ignore[union-attr]
            found[explanation.target] = {"id": explanation.explanation_id, "status": explanation.status.value, "stale": bool(stale), "model": explanation.model,
                                         "created_at": explanation.created_at.isoformat(timespec="seconds")}
        return found

    def _unresolved(self, params: Params) -> dict:
        """未解決・曖昧な呼び出しを、関数ごとにまとめた一覧（多い順）。AIの推測の入口（案 A）に使う。"""

        wanted = _text(params, "status", "all") or "all"
        if wanted not in ("all", "unresolved", "ambiguous"):
            raise ApiError(400, "status は all / unresolved / ambiguous のいずれかです")
        query = (_text(params, "q") or "").lower()
        limit = _int(params, "limit", 50, 1, 200) or 50
        offset = _int(params, "offset", 0, 0, 1_000_000) or 0
        groups: dict[str, list] = defaultdict(list)
        for reference in self._index.references:
            if reference.reference_kind != ReferenceKind.CALL or reference.resolution_status not in (ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS):
                continue
            state = reference.resolution_status.value
            if wanted != "all" and state != wanted:
                continue
            groups[reference.source_symbol_id].append(reference)
        inferences = self._inference_by_target()
        rows: list[dict[str, Any]] = []
        for symbol_id, references in groups.items():
            symbol = self._index.symbols.get(symbol_id)
            if symbol is None or (query and query not in symbol.qualified_name.lower() and not any(query in r.target_name.lower() for r in references)):
                continue
            rows.append({
                "function": symbol.qualified_name, "path": self._index.path_of(symbol.file_id), "line": symbol.start_line, "count": len(references),
                "unresolved": sum(1 for r in references if r.resolution_status == ResolutionStatus.UNRESOLVED),
                "ambiguous": sum(1 for r in references if r.resolution_status == ResolutionStatus.AMBIGUOUS),
                "inference": inferences.get(symbol.qualified_name),
                "calls": [
                    {"line": r.source_location.start_line, "name": r.target_name, "status": r.resolution_status.value, "note": r.note}
                    for r in sorted(references, key=lambda r: r.source_location.start_line)[:30]
                ],
            })
        rows.sort(key=lambda r: (-r["count"], r["function"]))
        return {"total_functions": len(rows), "total_calls": sum(r["count"] for r in rows), "offset": offset, "functions": rows[offset:offset + limit]}

    def _fields(self, params: Params) -> dict:
        """C/C++ の構造体のフィールドを、プロジェクト全体で読み書きしている箇所（`fields` と同じ）。"""

        name = _text(params, "name", required=True) or ""
        owner = None
        for separator in ("::", "."):
            if separator in name:
                owner, _, name = name.rpartition(separator)
                owner = owner.split("::")[-1].split(".")[-1]
                break
        if not re.fullmatch(r"[A-Za-z_]\w*", name) or (owner is not None and not re.fullmatch(r"[A-Za-z_]\w*", owner)):
            raise ApiError(400, "フィールド名は、識別子（または 構造体.フィールド）で指定してください")
        if not any(f.language in C_FAMILY for f in self._index.files.values()):
            raise ApiError(422, "C/C++ のファイルがありません（フィールド単位の読み書きは、C/C++ が対象です）")
        report = CFlowService(self._navigation).field_uses(self._project, self._index, name, owner)
        return {
            "field": name, "owner": owner, "owners": sorted(report.owners), "scanned_files": report.scanned, "skipped": report.skipped[:20],
            "uses": [vars(u) for u in report.uses[:2000]],
            "note": "構文から確認できた読み書きです。ポインタ経由の別名・マクロの内部・位置による初期化は追えません。",
        }

    def _config(self, params: Params) -> dict:
        items = self._cache.get("config")
        if items is None:
            items, skipped = ConfigService().scan(self._project, self._index)
            self._cache["config"] = items
            self._cache["config_skipped"] = skipped
        kinds = set(params.get("kind", []))
        query = (_text(params, "q") or "").lower()
        chosen = [i for i in items if (not kinds or i.kind in kinds) and (not query or query in i.name.lower())]  # type: ignore[attr-defined]
        limit = _int(params, "limit", 300, 1, 1000) or 300
        return {
            "kinds": CONFIG_LABELS, "total": len(chosen), "skipped": list(self._cache.get("config_skipped", []))[:20],  # type: ignore[call-overload]
            "items": [
                {"kind": i.kind, "name": i.name, "path": i.path, "line": i.line, "owner": i.owner, "default": i.default, "detail": i.detail, "confidence": i.confidence,
                 "uses": [{"path": u.path, "line": u.line, "owner": u.owner, "how": u.how} for u in i.uses[:20]]}
                for i in chosen[:limit]
            ],
        }

    def _boundaries(self, params: Params) -> dict:
        items = self._cache.get("boundaries")
        if items is None:
            items, skipped = BoundaryService().scan(self._project, self._index)
            self._cache["boundaries"] = items
            self._cache["boundaries_skipped"] = skipped
        kinds = set(params.get("kind", []))
        query = (_text(params, "q") or "").lower()
        chosen = [i for i in items if (not kinds or i.kind in kinds) and (not query or query in i.label.lower() or query in i.owner.lower())]  # type: ignore[attr-defined]
        limit = _int(params, "limit", 300, 1, 1000) or 300
        return {
            "kinds": BOUNDARY_LABELS, "total": len(chosen), "skipped": list(self._cache.get("boundaries_skipped", []))[:20],  # type: ignore[call-overload]
            "items": [
                {"kind": i.kind, "label": i.label, "path": i.path, "line": i.line, "owner": i.owner, "target": i.target.qualified_name if i.target else "", "detail": i.detail,
                 "confidence": i.confidence}
                for i in chosen[:limit]
            ],
        }

    def _status(self, params: Params) -> dict:
        """解析の状態: 前回の解析の結果、古いファイル、失敗の原因の一覧（隠さない）。"""

        results = self._repository.list_analysis_results(self._project.project_id)
        latest = results[0] if results else None
        by_language: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for file in self._index.files.values():
            by_language[file.language.value][file.analysis_status.value] += 1
        return {
            "latest": None if latest is None else {
                "status": latest.status.value, "timestamp": latest.analysis_timestamp.isoformat(timespec="seconds"), "analyzer_version": latest.analyzer_version,
                "repository_revision": latest.repository_revision, "warning_count": len(latest.warnings), "error_count": len(latest.errors),
            },
            "files_by_language": {k: dict(v) for k, v in by_language.items()},
            "stale_files": self._stale[:200], "stale_count": len(self._stale),
            "error_groups": _group_errors(latest.errors if latest else ()),
            "warnings": [w[:300] for w in (latest.warnings if latest else ())][:50],
            "note": "失敗したファイルは、正常に解析したものとして扱っていません。古いファイルの位置は、解析時と対応しない可能性があります。",
        }

    def _external(self, params: Params) -> dict:
        """外部ツールの取り込み結果（SARIF・SCIP・radare2）と、CodeInsight自身の結果との比較。外部の結果は事実ではなく、食い違いは並べて示す（採用しない）。"""

        limit = _int(params, "limit", 50, 1, 200) or 50
        findings = ExternalFindingsService(self._repository).with_staleness(self._project)
        by_tool: dict[str, int] = defaultdict(int)
        for finding, _ in findings:
            by_tool[f"{finding.tool} {finding.tool_version}".strip()] += 1
        index_service = ExternalIndexService(self._repository)
        binary_service = ExternalBinaryService(self._repository)
        index_summaries = index_service.summaries(self._project)
        binary_summaries = binary_service.summaries(self._project)
        return {
            "sarif": {
                "total": len(findings), "by_tool": dict(by_tool),
                "findings": [
                    {"tool": f.tool, "tool_version": f.tool_version, "rule": f.rule_id, "level": f.level, "message": f.message[:500], "path": f.path,
                     "start_line": f.start_line, "end_line": f.end_line, "source": f.source_name, "imported_at": f.imported_at.isoformat(timespec="seconds"), "stale": stale}
                    for f, stale in findings[:limit]
                ],
            },
            "scip": {"indexes": _plain_rows(index_summaries), "comparison": to_plain(index_service.compare(self._project, self._index), True) if index_summaries else None},
            "binary": {"binaries": _plain_rows(binary_summaries), "comparison": to_plain(binary_service.compare(self._project, self._index), True) if binary_summaries else None},
            "note": "外部ツールの結果は、CodeInsight自身の解析結果（事実）とは別の区分です。確定した事実として扱いません。食い違いは、並べて示すだけで、どちらも採用していません。取り込み後に変更されたファイルは、古いと示します。",
        }

    def _dynamic(self, params: Params) -> dict:
        """動的解析の実行の一覧（保存済みの記録。実行はしない）。"""

        runs = []
        for run in self._repository.list_dynamic_runs(self._project.project_id):
            stale = [p for p, h in run.source_hashes.items() if not self._hashes_current({p: h})]
            runs.append({
                "id": run.run_id, "started_at": run.started_at.isoformat(timespec="seconds"), "command": list(run.command), "status": run.status.value, "collector": run.collector,
                "exit_code": run.exit_code, "duration_seconds": run.duration_seconds, "stale": bool(stale), "stale_files": stale[:20], "sandbox": run.sandbox, "notes": run.notes[:10],
            })
        return {"runs": runs[:50], "note": "観測は、この1回の実行で起きたことの記録です。観測されなかったことは、呼ばれないことを意味しません。"}

    def _dynamic_diff(self, params: Params) -> dict:
        """観測と静的な呼び出し関係の差分（保存済みの観測を読むだけ）。"""

        wanted = _text(params, "run")
        runs = [r for r in self._repository.list_dynamic_runs(self._project.project_id) if r.status.value in ("completed", "failed")]
        if wanted:
            runs = [r for r in runs if r.run_id.startswith(wanted)]
        if not runs:
            raise ApiError(404, "比較できる観測がありません（完了・異常終了の実行のみ）")
        run = runs[0]
        limit = _int(params, "limit", 50, 1, 500) or 50
        diff = compare_with_static(self._index, run, self._repository.list_dynamic_observations(run.run_id))
        plain = to_plain(diff, True)
        counts = {k: len(plain[k]) for k in ("both", "observed_only", "unmatched", "static_only")}
        for key in counts:
            plain[key] = plain[key][:limit]
        plain["counts"] = counts
        plain["note"] = NOTE_DYNAMIC
        return plain

    def _inferences(self, params: Params) -> dict:
        """保存済みのAIの推測（種類 inference）。解析結果ではない。根拠のファイルが変わったものは、古いと示す。"""

        return {"inferences": [
            {"function": target, **info} for target, info in sorted(self._inference_by_target().items())
        ], "note": "AIの推測は、解析結果ではありません。検証状態が「検証済み」でないものは、事実として扱わないでください。"}

    def _inference(self, params: Params) -> dict:
        wanted = _text(params, "id", required=True) or ""
        explanation = self._repository.get_explanation(self._project.project_id, wanted)
        if explanation is None or explanation.target_kind != "inference":
            raise ApiError(404, "AIの推測が見つかりません")
        info = self._inference_by_target().get(explanation.target, {})
        stale = [p for p, h in explanation.source_hashes.items() if (self._index.file_by_path(p) is None or self._index.file_by_path(p).content_hash != h)]  # type: ignore[union-attr]
        return {
            "id": explanation.explanation_id, "function": explanation.target, "model": explanation.model, "provider": explanation.provider,
            "created_at": explanation.created_at.isoformat(timespec="seconds"), "status": explanation.status.value, "stale": bool(stale), "stale_files": stale,
            "text": explanation.text, "validation": explanation.validation, "latest": info.get("id") == explanation.explanation_id,
            "ai_generated": True, "note": "AIの推測です（確定ではありません）。解析結果の解決状態は、変わりません。",
        }


def _plain_rows(rows: list[dict]) -> list[dict]:
    return [{k: (v if isinstance(v, str | int | float | bool) or v is None else str(v)) for k, v in row.items()} for row in rows]


def _group_errors(errors) -> list[dict]:
    """解析の失敗を、原因ごとにまとめる（パス・行・引用の中身を除いた文言で）。"""

    groups: dict[str, list[str]] = defaultdict(list)
    for error in errors:
        message = error.split(": ", 1)[1] if ": " in error else error
        message = re.sub(r"<SourceLocation[^>]*>:?\s*", "", message)
        key = re.sub(r"'[^']*'", "'…'", message)[:120]
        groups[key].append(error[:200])
    result = []
    for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        hint = ""
        if "file not found" in key:
            hint = "ヘッダーが見つかりません。ビルドで生成されるヘッダーや、このホストに無いシステムヘッダーの可能性があります。configure とビルドを実行して compile_commands.json を渡すと、解決することがあります。"
        elif "Error parsing translation unit" in key:
            hint = "構文解析を始められませんでした。コンパイル設定（compile_commands.json）を確認してください。"
        result.append({"cause": key, "count": len(items), "examples": items[:3], "hint": hint})
    return result[:30]

"""外部ツール radare2 が出力した、バイナリの関数・呼び出しの取り込みと、CodeInsight自身の解析結果との比較。

radare2 は、利用者が実行して出力（`aflj`・`agCj`・`ij`）をファイルにしたものを**読むだけ**で、CodeInsight は実行しない。
内容は解析結果（事実）とは別に、ツール名・版・入力のハッシュ・取り込み時のソースの内容ハッシュとともに保存する。
**バイナリの辺は、最適化（インライン化・末尾呼び出し・削除）や radare2 の推定に左右される**ため、確定として扱わず、
食い違いは並べて示す。どちらかを黙って採用したり、解析結果を書き換えたりしない（1.1「6. 外部ツールの結果の区別」）。
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from codeinsight.analysis.call_graph import CallGraph
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Language, Project, ResolutionStatus, Symbol, SymbolKind
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.infrastructure.r2 import R2Error, R2Function, candidate_names, read_callgraph, read_functions, read_info

TOOL = "radare2"
_CODE_KINDS = frozenset({SymbolKind.FUNCTION, SymbolKind.METHOD})
_C_FAMILY = frozenset({Language.C, Language.CPP})


@dataclass
class BinaryImportReport:
    tool: str = TOOL
    tool_version: str = ""
    functions: int = 0
    imports: int = 0  # sym.imp.*（外部ライブラリの関数）
    calls: int = 0
    files_matched: int = 0  # デバッグ情報のソースファイルを、プロジェクトのファイルに対応づけられた数
    files_unmatched: list[str] = field(default_factory=list)
    binary_path: str = ""
    binary_hashed: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FunctionPair:
    binary_name: str
    symbol: str  # 修飾名
    path: str
    addr: int
    size: int


@dataclass(frozen=True)
class EdgeItem:
    caller: str
    callee: str
    candidates: tuple[str, ...] = ()  # 自身の、呼び出し元にある未解決・曖昧な呼び出し（解決候補。採用はしない）
    note: str = ""


@dataclass
class BinaryComparison:
    binaries: list[str] = field(default_factory=list)
    matched: list[FunctionPair] = field(default_factory=list)
    ambiguous: list[tuple[str, list[str]]] = field(default_factory=list)  # (バイナリの関数, 候補の修飾名)
    binary_only: list[str] = field(default_factory=list)  # ソースに対応する関数が見つからない（ライブラリ・生成コード・名前の不一致）
    source_only: list[str] = field(default_factory=list)  # ソースにあるが、バイナリに見つからない（インライン化・削除・ビルド対象外）
    edges_both: list[EdgeItem] = field(default_factory=list)
    edges_binary_only: list[EdgeItem] = field(default_factory=list)
    edges_source_only: list[EdgeItem] = field(default_factory=list)
    stale_files: list[str] = field(default_factory=list)  # 取り込み後に変更されたソース（比較から除く）
    notes: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


class ExternalBinaryService:
    def __init__(self, repository: AnalysisRepository) -> None:
        self._repository = repository

    # --- 取り込み ---

    def import_r2(
        self, project: Project, functions_path: Path, callgraph_path: Path | None = None, info_path: Path | None = None,
        binary_path: Path | None = None, tool_version: str = "",
    ) -> BinaryImportReport:
        try:
            data = functions_path.read_bytes()
            functions = read_functions(data)
            edges = read_callgraph(callgraph_path.read_bytes()) if callgraph_path else []
            info = read_info(info_path.read_bytes()) if info_path else None
        except OSError as exc:
            raise R2Error(f"ファイルを読み込めません: {exc}") from exc
        report = BinaryImportReport(tool_version=tool_version, functions=len(functions), imports=sum(1 for f in functions if f.is_import), calls=len(edges))
        if not any(f.file for f in functions) and any(not f.is_import for f in functions):
            report.notes.append("デバッグ情報（DWARF）が無いため、関数をソースファイルに対応づけられません（名前だけで比較します。`-g` 付きでビルドしたバイナリを使うと精度が上がります）。")
        digest = hashlib.sha256(data).hexdigest()
        files, unmatched = self._map_files(project, {f.file for f in functions if f.file})
        report.files_matched, report.files_unmatched = len(files), unmatched
        binary_hash, path_text = "", ""
        candidate = binary_path or (Path(info.path) if info and info.path else None)
        if candidate is not None:
            path_text = str(candidate)[:500]
            binary_hash = _sha256(candidate.expanduser())
            report.binary_path, report.binary_hashed = path_text, bool(binary_hash)
            if not binary_hash:
                report.notes.append("バイナリのファイルを読めないため、内容のハッシュを記録していません（バイナリが変わったかを判定できません）。")
        if info is not None and info.stripped:
            report.notes.append("バイナリはストリップ済みです。関数名が失われ、ソースとの対応づけは限られます。")
        meta = {
            "binary_id": uuid.uuid4().hex, "tool": TOOL, "tool_version": tool_version[:60], "source_name": functions_path.name[:120], "source_sha256": digest,
            "imported_at": datetime.now().isoformat(), "binary_path": path_text, "binary_sha256": binary_hash,
            "arch": info.arch if info else "", "os": info.os if info else "", "stripped": (None if info is None or info.stripped is None else int(info.stripped)),
            "functions": len(functions), "calls": len(edges),
        }
        rows = [(f.name, f.addr, f.size, f.file, f.signature, f.nbbs, f.indegree, f.outdegree) for f in functions]
        self._repository.replace_external_binary(project.project_id, meta, files, rows, edges)
        return report

    def _map_files(self, project: Project, names: set[str]) -> tuple[dict[str, str], list[str]]:
        """デバッグ情報のソースファイル名を、プロジェクトのファイルに対応づける（パスの末尾が一意に一致するもの）。{相対パス: 内容ハッシュ}"""

        project_files = [f.relative_path for f in self._repository.list_source_files(project.project_id)]
        files: dict[str, str] = {}
        unmatched: list[str] = []
        for name in sorted(names):
            tail = name.replace("\\", "/").lstrip("./")
            found = [p for p in project_files if p == tail or p.endswith("/" + tail)]
            if len(found) != 1:
                unmatched.append(name)
                continue
            content_hash = _sha256(project.root_path / found[0])
            if content_hash:
                files[found[0]] = content_hash
        return files, unmatched

    def clear(self, project: Project, tool: str | None = None) -> int:
        return self._repository.delete_external_binaries(project.project_id, tool=tool)

    def summaries(self, project: Project) -> list[dict]:
        return [dict(row) for row in self._repository.list_external_binaries(project.project_id)]

    # --- 比較 ---

    def compare(self, project: Project, index: ProjectIndex) -> BinaryComparison:
        report = BinaryComparison()
        by_name: dict[str, list[Symbol]] = defaultdict(list)
        for symbol in index.symbols.values():
            source = index.files.get(symbol.file_id)
            if symbol.kind in _CODE_KINDS and source is not None and source.language in _C_FAMILY:
                by_name[symbol.name].append(symbol)
        graph = CallGraph(index.symbols.values(), index.references)
        for row in self._repository.list_external_binaries(project.project_id):
            version = row["tool_version"]
            label_text = version if version.lower().startswith(row["tool"].lower()) else f"{row['tool']} {version}".strip()
            report.binaries.append(f"{label_text} ({row['source_name']})")
            current = {p: _sha256(project.root_path / p) for p in self._repository.external_binary_files(row["binary_id"])}
            recorded = self._repository.external_binary_files(row["binary_id"])
            stale = sorted(p for p, h in recorded.items() if current.get(p) != h)
            report.stale_files += [p for p in stale if p not in report.stale_files]
            if row["binary_sha256"] and row["binary_path"] and _sha256(Path(row["binary_path"]).expanduser()) not in ("", row["binary_sha256"]):
                report.notes.append(f"取り込み後に、バイナリ（{row['binary_path']}）の内容が変わっています。古い結果です。")
            self._compare_one(project, index, row, by_name, graph, set(stale), report)
        report.notes.append(
            "バイナリの辺・関数は、最適化（インライン化・末尾呼び出し・削除）や radare2 の推定の影響を受けます。食い違いは、どちらが正しいかを意味しません。"
        )
        return report

    def _compare_one(self, project, index, row, by_name, graph: CallGraph, stale: set[str], report: BinaryComparison) -> None:
        functions = [R2Function(r["name"], r["addr"], r["size"], r["file"], r["signature"], r["nbbs"], r["indegree"], r["outdegree"]) for r in self._repository.external_binary_functions(row["binary_id"])]
        resolved: dict[str, Symbol] = {}  # バイナリの関数名 → 対応するシンボル
        for function in functions:
            if function.is_import:
                continue
            found = self._match(index, by_name, function)
            if len(found) == 1:
                symbol = found[0]
                path = index.path_of(symbol.file_id)
                if path in stale:
                    continue
                resolved[function.name] = symbol
                report.matched.append(FunctionPair(function.name, symbol.qualified_name, path, function.addr, function.size))
            elif len(found) > 1:
                report.ambiguous.append((function.name, sorted(s.qualified_name for s in found)))
            else:
                report.binary_only.append(function.name)
        seen_files = {s.file_id for s in resolved.values()}
        matched_ids = {s.symbol_id for s in resolved.values()}
        for symbol in sorted((s for group in by_name.values() for s in group), key=lambda s: s.qualified_name):
            if symbol.file_id in seen_files and symbol.symbol_id not in matched_ids and index.path_of(symbol.file_id) not in stale:
                report.source_only.append(symbol.qualified_name)
        # 辺: バイナリの辺（両端がソースの関数に対応づいたもの）と、自身の確定済みの呼び出し辺を比べる
        normalized: dict[str, Symbol] = {}  # ソース上の名前の候補 → シンボル（agCj の名前は、aflj と接頭辞が違うことがある）
        for function_name, matched_symbol in resolved.items():
            for candidate in candidate_names(function_name):
                normalized.setdefault(candidate, matched_symbol)
        pairs: set[tuple[str, str]] = set()
        for a, b in self._repository.external_binary_calls(row["binary_id"]):
            caller = next((normalized[n] for n in candidate_names(a) if n in normalized), None)
            callee = next((normalized[n] for n in candidate_names(b) if n in normalized), None)
            if caller is not None and callee is not None:
                pairs.add((caller.symbol_id, callee.symbol_id))
        def label(symbol_id: str) -> str:
            return index.symbols[symbol_id].qualified_name

        ours: dict[tuple[str, str], None] = {}
        for symbol in resolved.values():
            for reference in graph.calls_from(symbol.symbol_id):
                if reference.resolution_status == ResolutionStatus.RESOLVED and reference.target_symbol_id in matched_ids:
                    ours[(symbol.symbol_id, reference.target_symbol_id)] = None
        for pair in sorted(pairs & set(ours), key=lambda p: (label(p[0]), label(p[1]))):
            report.edges_both.append(EdgeItem(label(pair[0]), label(pair[1])))
        for pair in sorted(pairs - set(ours), key=lambda p: (label(p[0]), label(p[1]))):
            candidates = tuple(
                f"L{r.source_location.start_line} {r.target_name}" for r in graph.calls_from(pair[0])
                if r.resolution_status in (ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS)
            )
            report.edges_binary_only.append(EdgeItem(label(pair[0]), label(pair[1]), candidates))
        # 自身にあるが、バイナリの辺に無いもの（呼び出し元の辺がバイナリに1本も無い関数は、radare2 が追えていない可能性があるため除く）
        callers_with_edges = {c for c, _ in pairs}
        for pair in sorted(set(ours) - pairs, key=lambda p: (label(p[0]), label(p[1]))):
            if pair[0] in callers_with_edges:
                report.edges_source_only.append(EdgeItem(label(pair[0]), label(pair[1]), note="バイナリの辺に無い（インライン化・最適化・条件付きコンパイルなど）"))

    @staticmethod
    def _match(index: ProjectIndex, by_name: dict[str, list[Symbol]], function: R2Function) -> list[Symbol]:
        found: list[Symbol] = []
        for name in candidate_names(function.name):
            found = by_name.get(name, [])
            if found:
                break
        if function.file and len(found) > 1:
            tail = function.file.replace("\\", "/").lstrip("./")
            narrowed = [s for s in found if (index.path_of(s.file_id) == tail or index.path_of(s.file_id).endswith("/" + tail))]
            found = narrowed or found
        elif function.file and len(found) == 1:
            tail = function.file.replace("\\", "/").lstrip("./")
            path = index.path_of(found[0].file_id)
            if not (path == tail or path.endswith("/" + tail)):
                return []  # 名前は同じでも、デバッグ情報のソースファイルが違う（別の関数）
        return found

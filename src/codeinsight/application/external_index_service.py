"""外部のコード索引（SCIP）の取り込みと、CodeInsight自身の参照解決との比較。

索引は、利用者が外部ツール（scip-python・scip-clang など）で生成したファイルを**読むだけ**で、ツールの実行も、
対象リポジトリの変更もしない。索引の内容は、CodeInsight自身の解析結果（事実）とは別に保存し、食い違いは並べて示す。
どちらかを黙って採用したり、解析結果を書き換えたりしない。
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Confidence, Project, Reference, ResolutionStatus, Symbol
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.infrastructure.scip import ScipError, read_scip, symbol_name


@dataclass
class IndexImportReport:
    tool: str = ""
    tool_version: str = ""
    documents: int = 0
    occurrences: int = 0
    skipped_outside: int = 0  # ルートの外・解釈できないパスの文書
    skipped_missing: int = 0  # ルートの中に存在しないファイルの文書
    root_note: str = ""


@dataclass(frozen=True)
class Comparison:
    reference: Reference
    path: str
    line: int
    ours: str  # 自身の解決先（修飾名）。解決できなければ空
    index: tuple[tuple[str, int], ...]  # 索引の定義の位置（パス, 行）
    symbol: str  # 索引のシンボル名


@dataclass
class ComparisonReport:
    indexes: list[str] = field(default_factory=list)
    compared: int = 0  # 索引が対象にしたファイルの中の、比較した参照の数
    confirmed: int = 0  # 自身の解決先が、索引の定義と一致
    confirmed_inferred: int = 0  # うち、自身は「推定」としていたもの
    conflicts: list[Comparison] = field(default_factory=list)  # 解決先が食い違う
    index_resolves: list[Comparison] = field(default_factory=list)  # 自身は解決できず、索引は定義を示す（採用はしない）
    consistent_external: int = 0  # 自身も索引も、プロジェクトの外
    no_occurrence: int = 0  # その行に、同名の出現箇所が索引にない
    stale_files: list[str] = field(default_factory=list)  # 取り込み後に変更されたファイル（比較から除く）
    by_state: dict[str, int] = field(default_factory=dict)


def _hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _map_root(project_root: str, root: Path) -> tuple[str, str]:
    """索引のルートが、プロジェクトのルートの下なら、その相対パス（接頭辞）。(接頭辞, 注記)"""

    if not project_root:
        return "", ""
    parsed = urlparse(project_root)
    if parsed.scheme not in ("", "file"):
        return "", "索引のルートがファイルのURIではないため、文書のパスをそのままプロジェクトのルートからの相対パスとして扱います"
    try:
        relative = Path(unquote(parsed.path)).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return "", "索引のルートがプロジェクトのルートの下にないため、文書のパスをそのままプロジェクトのルートからの相対パスとして扱います"
    return ("" if str(relative) == "." else relative.as_posix() + "/"), ""


class ExternalIndexService:
    def __init__(self, repository: AnalysisRepository) -> None:
        self._repository = repository

    def import_scip(self, project: Project, path: Path) -> IndexImportReport:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise ScipError(f"ファイルを読み込めません: {exc}") from exc
        metadata, documents = read_scip(data)
        digest = hashlib.sha256(data).hexdigest()
        prefix, note = _map_root(metadata.project_root, project.root_path)
        report = IndexImportReport(metadata.tool, metadata.tool_version, root_note=note)
        files: dict[str, str] = {}
        occurrences: list[tuple] = []
        symbols: dict[str, tuple] = {}
        for document in documents:
            relative = self._relative(prefix + document.relative_path, project.root_path)
            if relative is None:
                report.skipped_outside += 1
                continue
            content_hash = _hash(project.root_path / relative)
            if not content_hash:
                report.skipped_missing += 1
                continue
            files[relative] = content_hash
            report.documents += 1
            for occurrence in document.occurrences:
                if occurrence.symbol.startswith("local ") or not occurrence.symbol:
                    continue  # 関数内の局所シンボルは、プロジェクトをまたぐ参照ではない
                occurrences.append((relative, occurrence.start_line, occurrence.start_char, occurrence.end_line, occurrence.end_char, occurrence.symbol, occurrence.roles))
            for info in document.symbols:
                if not info.symbol.startswith("local "):
                    symbols[info.symbol] = (info.symbol, info.display_name, info.kind, info.documentation)
        report.occurrences = len(occurrences)
        meta = {
            "index_id": uuid.uuid4().hex, "tool": metadata.tool or "不明なツール", "tool_version": metadata.tool_version, "project_root": metadata.project_root,
            "source_name": path.name[:120], "source_sha256": digest, "imported_at": datetime.now().isoformat(), "documents": report.documents, "occurrences": report.occurrences,
        }
        self._repository.replace_external_index(project.project_id, meta, files, occurrences, symbols.values())
        return report

    @staticmethod
    def _relative(value: str, root: Path) -> str | None:
        try:
            candidate = Path(value)
            resolved_root = root.resolve()
            return (candidate if candidate.is_absolute() else resolved_root / candidate).resolve().relative_to(resolved_root).as_posix()
        except (ValueError, OSError):
            return None

    def clear(self, project: Project, tool: str | None = None) -> int:
        return self._repository.delete_external_indexes(project.project_id, tool=tool)

    def summaries(self, project: Project) -> list[dict]:
        return [dict(row) for row in self._repository.list_external_indexes(project.project_id)]

    # --- 比較 ---

    def compare(self, project: Project, index: ProjectIndex) -> ComparisonReport:
        report = ComparisonReport()
        rows = self._repository.list_external_indexes(project.project_id)
        symbols_by_path: dict[str, list[Symbol]] = defaultdict(list)
        for symbol in index.symbols.values():
            symbols_by_path[index.path_of(symbol.file_id)].append(symbol)
        for row in rows:
            report.indexes.append(f"{row['tool']} {row['tool_version']}")
            files = self._repository.external_index_files(row["index_id"])
            fresh = {p for p, h in files.items() if _hash(project.root_path / p) == h}
            report.stale_files += sorted(set(files) - fresh)
            definitions: dict[str, list[tuple[str, int]]] = defaultdict(list)
            at_line: dict[tuple[str, int], list[tuple[str, str]]] = defaultdict(list)  # (パス, 行) -> [(名前, シンボル)]
            for occ in self._repository.external_index_occurrences(row["index_id"]):
                if occ["path"] not in fresh:
                    continue
                if occ["roles"] & 0x1:
                    definitions[occ["symbol"]].append((occ["path"], occ["start_line"] + 1))
                else:
                    at_line[(occ["path"], occ["start_line"] + 1)].append((symbol_name(occ["symbol"]), occ["symbol"]))
            for reference in index.references:
                path = index.path_of(reference.source_location.file_id)
                if path not in fresh or not reference.target_name:
                    continue
                line = reference.source_location.start_line
                name = reference.target_name.rsplit(".", 1)[-1]
                candidates = {symbol for n, symbol in at_line.get((path, line), []) if n == name}
                report.compared += 1
                state = reference.resolution_status.value + ("・推定" if reference.confidence == Confidence.INFERRED and reference.resolution_status == ResolutionStatus.RESOLVED else "")
                report.by_state[state] = report.by_state.get(state, 0) + 1
                if not candidates:
                    report.no_occurrence += 1
                    continue
                locations = tuple(sorted({loc for symbol in candidates for loc in definitions.get(symbol, [])}))
                target = index.symbols.get(reference.target_symbol_id) if reference.target_symbol_id else None
                ours = target.qualified_name if target else ""
                comparison = Comparison(reference, path, line, ours, locations, name)
                if reference.resolution_status == ResolutionStatus.RESOLVED and target is not None:
                    target_path = index.path_of(target.file_id)
                    if any(p == target_path and target.start_line <= ln <= target.end_line for p, ln in locations):
                        report.confirmed += 1
                        if reference.confidence == Confidence.INFERRED:
                            report.confirmed_inferred += 1
                    else:
                        report.conflicts.append(comparison)  # 索引の定義が別の場所、または索引はプロジェクトの外とみなしている
                elif locations:
                    report.index_resolves.append(comparison)
                else:
                    report.consistent_external += 1
        report.stale_files = sorted(set(report.stale_files))
        report.conflicts.sort(key=lambda c: (c.path, c.line))
        report.index_resolves.sort(key=lambda c: (c.path, c.line))
        return report

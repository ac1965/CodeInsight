"""収集器が書いたJSON（codeinsight.observation/1）を、観測（Observation）へ変換し、解析結果のシンボルと対応づける。

対応づけは、ファイルの相対パスと関数の修飾名・開始行による。対応づけられない観測も、パスと名前つきで残す
（静的解析が見つけていない関数が、実際に実行された、という情報になる）。値は、収集器の段階で記録していない。
"""

from __future__ import annotations

import hashlib

from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Symbol, SymbolKind
from codeinsight.domain.observation import Observation, ObservationKind

SCHEMA = "codeinsight.observation/1"
_FUNCTION_KINDS = frozenset({SymbolKind.FUNCTION, SymbolKind.METHOD})


class SymbolMatcher:
    def __init__(self, index: ProjectIndex) -> None:
        self._by_path: dict[str, list[Symbol]] = {}
        for symbol in index.symbols.values():
            if symbol.kind in _FUNCTION_KINDS and symbol.file_id in index.files:
                self._by_path.setdefault(index.files[symbol.file_id].relative_path, []).append(symbol)

    def match(self, path: str, qualname: str, line: int) -> Symbol | None:
        """パスと修飾名（末尾の一致）で絞り、複数なら開始行が近いものを選ぶ。1つに絞れなければ None。"""

        qualname = qualname.replace(".<locals>.", ".")
        candidates = [
            s for s in self._by_path.get(path, [])
            if s.qualified_name == qualname or s.qualified_name.endswith("." + qualname)
        ]
        if len(candidates) > 1:
            exact = [s for s in candidates if s.start_line <= line <= s.end_line and abs(s.start_line - line) <= 1]
            candidates = exact or candidates
        return candidates[0] if len(candidates) == 1 else None


def _oid(run_id: str, *parts: object) -> str:
    return hashlib.sha256("\x00".join([run_id, *map(str, parts)]).encode()).hexdigest()[:24]


def to_observations(run_id: str, data: dict, matcher: SymbolMatcher) -> list[Observation]:
    if data.get("schema") != SCHEMA:
        raise ValueError(f"収集器の出力の形式が想定と違います: {data.get('schema')!r}")
    out: list[Observation] = []

    def ref(entry: dict) -> tuple[str, str, int, str | None]:
        symbol = matcher.match(entry["file"], entry["qualname"], entry["line"])
        return entry["file"], entry["qualname"], entry["line"], symbol.symbol_id if symbol else None

    for item in data.get("functions", []):
        path, name, line, symbol_id = ref(item)
        out.append(Observation(_oid(run_id, "fn", path, name, line), run_id, ObservationKind.COVERAGE, symbol_id=symbol_id,
                               start_line=line, path=path, name=name, count=item["count"], detail={"what": "function"}))
    for item in data.get("calls", []):
        path, name, line, symbol_id = ref(item["caller"])
        tpath, tname, tline, target_id = ref(item["callee"])
        out.append(Observation(_oid(run_id, "call", path, name, tpath, tname), run_id, ObservationKind.CALL, symbol_id=symbol_id,
                               start_line=line, path=path, name=name, target_symbol_id=target_id, target_name=tname,
                               target_path=tpath, count=item["count"], detail={"target_line": tline}))
    for path, numbers in data.get("lines", {}).items():
        out.append(Observation(_oid(run_id, "lines", path), run_id, ObservationKind.COVERAGE, path=path, count=len(numbers),
                               detail={"what": "lines", "lines": numbers}))
    for item in data.get("raised", []):
        out.append(Observation(_oid(run_id, "raise", item["type"], item["file"], item["line"]), run_id, ObservationKind.FAILURE,
                               start_line=item["line"], path=item["file"], name=item["function"], count=item["count"],
                               detail={"what": "raised", "type": item["type"]}))
    uncaught = data.get("uncaught")
    if uncaught:
        out.append(Observation(_oid(run_id, "uncaught"), run_id, ObservationKind.FAILURE, start_line=uncaught.get("line"),
                               path=uncaught.get("file") or "", name=uncaught.get("function") or "", detail={"what": "uncaught", "type": uncaught["type"]}))
    return out

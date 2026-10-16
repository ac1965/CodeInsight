"""観測（動的解析）と静的解析の結果の差分（D4）。保存済みの結果を読むだけで、何も実行しない。

観測は「この1回の実行で起きたこと」の証拠であり、静的な事実とは別の区分。ここでの区分は次のとおり。

* 静的にも確認: 観測された呼び出しが、静的な確定済みの辺としても存在する。
* 観測のみ: 観測されたが、静的な確定済みの辺に無い。呼び出し元に未解決・曖昧な呼び出しがあれば、
  **解決候補**として併記する（確定にはしない。他の入力では別の先になりうる）。
* 静的のみ（呼び出し元は実行された）: 静的にあるが、この実行では観測されなかった。条件分岐・入力により通らなかっただけ
  かもしれず、デッドコードの断定には使えない。
* 解析後にソースが変わったファイルのシンボルは、位置が対応しないため比較から外し、その旨を示す（3.10）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codeinsight.analysis.call_graph import CallGraph
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import SymbolKind
from codeinsight.domain.observation import DynamicRun, Observation, ObservationKind
from codeinsight.domain.reference import ResolutionStatus
from codeinsight.dynamic.collectors import COLLECTORS

_FUNCTION_KINDS = frozenset({SymbolKind.FUNCTION, SymbolKind.METHOD})
NOTE = (
    "観測は、この1回の実行で起きたことの記録です。観測されなかったことは、存在しない・呼ばれないことを意味しません。"
    "静的な呼び出し関係も、実行された経路ではありません。"
)


@dataclass(frozen=True)
class Candidate:
    """観測のみの辺の解決候補（呼び出し元にある、静的に未解決・曖昧な呼び出し）。"""

    line: int
    target_name: str
    status: str  # unresolved / ambiguous
    note: str = ""


@dataclass(frozen=True)
class EdgeDiff:
    caller: str
    caller_path: str
    callee: str
    callee_path: str
    count: int = 0
    candidates: tuple[Candidate, ...] = ()
    reason: str = ""  # 静的のみ・対応づけ不能の理由


@dataclass
class DiffResult:
    run_id: str
    command: tuple[str, ...]
    status: str
    both: list[EdgeDiff] = field(default_factory=list)
    observed_only: list[EdgeDiff] = field(default_factory=list)  # 解析結果のシンボルに対応づいた、静的に無い辺
    unmatched: list[EdgeDiff] = field(default_factory=list)  # 解析結果のシンボルに対応づけられない辺（静的解析が見つけていない関数）
    static_only: list[EdgeDiff] = field(default_factory=list)  # 呼び出し元は実行されたが、辺は観測されない
    not_executed: int = 0  # 観測された範囲のファイルにある関数のうち、実行が観測されなかったもの
    executed_functions: int = 0
    stale_files: list[str] = field(default_factory=list)  # 実行後に解析結果と内容が変わった（比較から外したもの）
    not_compared: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def compare(index: ProjectIndex, run: DynamicRun, observations: list[Observation]) -> DiffResult:
    graph = CallGraph(index.symbols.values(), index.references)
    hash_of = {f.relative_path: f.content_hash for f in index.files.values()}
    # 実行時のソースと、解析結果のソースが一致するファイルだけを比べる
    stale = sorted(p for p, h in run.source_hashes.items() if p in hash_of and hash_of[p] != h)
    stale_set = set(stale)
    result = DiffResult(run.run_id, run.command, run.status.value, stale_files=stale, notes=[NOTE])
    if run.source_hashes and not hash_of:
        result.notes.append("解析結果にファイルがありません。")

    def path_of(symbol_id: str) -> str:
        return index.path_of(index.symbols[symbol_id].file_id)

    def comparable(symbol_id: str) -> bool:
        return path_of(symbol_id) not in stale_set

    executed_ids = {
        o.symbol_id for o in observations
        if o.kind == ObservationKind.COVERAGE and o.detail.get("what") == "function" and o.symbol_id and comparable(o.symbol_id)
    }
    result.executed_functions = len({o.name + o.path for o in observations if o.detail.get("what") == "function"})

    observed_edges: dict[tuple[str, str], int] = {}
    for o in observations:
        if o.kind != ObservationKind.CALL:
            continue
        if not (o.symbol_id and o.target_symbol_id):
            result.unmatched.append(EdgeDiff(o.name, o.path, o.target_name, o.target_path, o.count,
                                             reason="解析結果のシンボルに対応づけられません（静的解析が見つけていない関数、または名前の曖昧さ）"))
            continue
        if not (comparable(o.symbol_id) and comparable(o.target_symbol_id)):
            result.not_compared.append(f"{o.name} → {o.target_name}")
            continue
        observed_edges[(o.symbol_id, o.target_symbol_id)] = observed_edges.get((o.symbol_id, o.target_symbol_id), 0) + o.count

    static_edges: dict[tuple[str, str], list[int]] = {}
    for caller_id in {s for s, _ in observed_edges} | executed_ids:
        for reference in graph.calls_from(caller_id):
            if reference.resolution_status == ResolutionStatus.RESOLVED and reference.target_symbol_id:
                static_edges.setdefault((caller_id, reference.target_symbol_id), []).append(reference.source_location.start_line)

    def edge(caller_id: str, callee_id: str, **kw) -> EdgeDiff:
        caller, callee = index.symbols[caller_id], index.symbols[callee_id]
        return EdgeDiff(caller.qualified_name, path_of(caller_id), callee.qualified_name, path_of(callee_id), **kw)

    for (caller_id, callee_id), count in sorted(observed_edges.items()):
        if (caller_id, callee_id) in static_edges:
            result.both.append(edge(caller_id, callee_id, count=count))
            continue
        candidates = tuple(
            Candidate(r.source_location.start_line, r.target_name, r.resolution_status.value, r.note)
            for r in graph.calls_from(caller_id)
            if r.resolution_status in (ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS)
        )
        result.observed_only.append(edge(caller_id, callee_id, count=count, candidates=candidates,
                                         reason="" if candidates else "呼び出し元に、未解決・曖昧な呼び出しも見つかりません"))
    spec = next((c for c in COLLECTORS if c.name == run.collector), None)
    observes_calls = spec is None or ObservationKind.CALL in spec.kinds
    if not observes_calls:
        result.notes.append(
            f"この実行の収集器（{run.collector}）は、呼び出しの辺を観測できないため、辺の比較（観測のみ・静的のみ）はしていません。"
            "関数が実行されたかどうか（実行された関数・実行を観測できなかった関数）だけが比較の対象です。"
        )
    for (caller_id, callee_id), lines in sorted(static_edges.items() if observes_calls else []):
        if caller_id in executed_ids and (caller_id, callee_id) not in observed_edges:
            result.static_only.append(edge(caller_id, callee_id, reason=f"呼び出し元は実行されたが、この辺は観測されない（静的な呼び出し位置: L{', L'.join(map(str, sorted(set(lines))))}）"))

    observed_paths = {o.path for o in observations if o.path and o.detail.get("what") in ("function", "lines")} - stale_set
    result.not_executed = sum(
        1 for s in index.symbols.values()
        if s.kind in _FUNCTION_KINDS and s.symbol_id not in executed_ids and index.path_of(s.file_id) in observed_paths
    )
    if stale:
        result.notes.append(f"実行後（解析時）にソースが変わった {len(stale)} ファイルは、位置が対応しないため比較から外しました。")
    if result.unmatched:
        result.notes.append("解析結果に対応づけられない辺があります。静的解析が見つけていない関数が実行された可能性を示します。")
    return result

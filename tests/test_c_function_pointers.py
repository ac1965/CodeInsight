"""C の関数ポインタ経由の呼び出しを、代入・初期化の根拠から推定する（確定にはしない）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeinsight.application import NavigationService
from codeinsight.cli import main
from codeinsight.domain import Confidence, ResolutionStatus
from codeinsight.domain.reference import ReferenceKind
from codeinsight.infrastructure.analysis_repository import AnalysisRepository

SOURCE = """\
struct ops { int (*open)(int); int (*close)(int); int (*never)(int); };
static int a_open(int x) { return x; }
static int a_close(int x) { return x; }
static int b_open(int x) { return x + 1; }
static const struct ops A = { .open = a_open, .close = a_close };
static const struct ops B = { b_open, a_close };
int (*global_cb)(int);
void setup(struct ops *o) { o->open = b_open; global_cb = &a_open; }
int run(struct ops *o, int v) { global_cb(v); return o->open(v) + o->close(v) + o->never(v); }
int local(int (*cb)(int), int v) { return cb(v); }
"""


@pytest.fixture
def calls(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "fp.c").write_text(SOURCE, encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    repository = AnalysisRepository(Path(db))
    project = repository.list_projects()[0]
    index = NavigationService(repository).load_index(project)
    by_name = {s.qualified_name: s for s in index.symbols.values()}

    def of(function: str) -> dict[str, object]:
        return {r.target_name: r for r in index.references if r.source_symbol_id == by_name[function].symbol_id and r.reference_kind == ReferenceKind.CALL}

    yield of, index
    repository.close()


def test_single_assigned_function_is_inferred_not_confirmed(calls) -> None:
    of, index = calls
    close = of("run")["close"]
    assert close.resolution_status == ResolutionStatus.RESOLVED and close.confidence == Confidence.INFERRED  # 初期化子（指示付き・位置）から
    assert index.symbols[close.target_symbol_id].name == "a_close"
    assert "再代入" in close.note  # 推定の理由と限界を示す
    cb = of("run")["global_cb"]
    assert cb.resolution_status == ResolutionStatus.RESOLVED and cb.confidence == Confidence.INFERRED  # `&a_open` の代入から
    assert index.symbols[cb.target_symbol_id].name == "a_open"


def test_multiple_candidates_are_ambiguous_and_never_pick_one(calls) -> None:
    of, _ = calls
    opened = of("run")["open"]
    assert opened.resolution_status == ResolutionStatus.AMBIGUOUS and opened.target_symbol_id is None
    assert "a_open" in opened.note and "b_open" in opened.note  # 指示付き・位置による初期化・代入の3箇所から集めた候補


def test_without_any_assignment_or_slot_it_stays_unresolved(calls) -> None:
    of, _ = calls
    never = of("run")["never"]
    assert never.resolution_status == ResolutionStatus.UNRESOLVED and "見つからない" in never.note
    assert of("local")["cb"].resolution_status == ResolutionStatus.UNRESOLVED  # 引数の関数ポインタは置き場を特定できない


def test_resolution_is_idempotent(tmp_path: Path, calls) -> None:
    of, index = calls
    before = {k: (v.resolution_status, v.target_symbol_id, v.note) for k, v in of("run").items()}
    from codeinsight.analysis.reference_resolver import ReferenceResolver

    ReferenceResolver().resolve(list(index.files.values()), list(index.symbols.values()), list(index.references), list(index.dependencies))
    after = {k: (v.resolution_status, v.target_symbol_id, v.note) for k, v in of("run").items()}
    assert before == after

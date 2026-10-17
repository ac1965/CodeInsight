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


LOCAL_PARAM_SOURCE = """\
static int mul(int a, int b) { return a * b; }
static int add(int a, int b) { return a + b; }
static int sub(int a, int b) { return a - b; }
int apply(int (*f)(int, int), int x) { return f(x, 2); }
int forward(int (*g)(int, int), int x) { return apply(g, x); }
int via_local(int x) { int (*op)(int, int) = mul; return op(x, 3); }
int via_assign(int x) { int (*op)(int, int); op = add; return op(x, 4); }
int via_two(int x, int c) { int (*op)(int, int) = c ? add : sub; op = sub; return op(x, 5); }
int unknown_local(int (*h)(int, int)) { int (*op)(int, int) = h; return op(1, 2); }
int strcmp_like(int a, int b) { return a - b; }
int main(void) { return apply(mul, 1) + apply(add, 2) + forward(sub, 3) + apply(strcmp_like, 4); }
"""


@pytest.fixture
def local_calls(tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "lp.c").write_text(LOCAL_PARAM_SOURCE, encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    repository = AnalysisRepository(Path(db))
    project = repository.list_projects()[0]
    index = NavigationService(repository).load_index(project)
    by_name = {s.qualified_name: s for s in index.symbols.values()}

    def of(function: str, target: str):
        (found,) = [r for r in index.references if r.source_symbol_id == by_name[function].symbol_id and r.reference_kind == ReferenceKind.CALL and r.target_name == target]
        return found

    yield of, index
    repository.close()


def test_local_function_pointer_is_resolved_from_its_assignment(local_calls) -> None:
    of, index = local_calls
    local = of("via_local", "op")
    assert local.resolution_status == ResolutionStatus.RESOLVED and local.confidence == Confidence.INFERRED
    assert index.symbols[local.target_symbol_id].name == "mul" and "この変数に" in local.note
    assigned = of("via_assign", "op")
    assert index.symbols[assigned.target_symbol_id].name == "add"


def test_local_with_several_assignments_is_ambiguous(local_calls) -> None:
    of, _ = local_calls
    two = of("via_two", "op")
    assert two.resolution_status == ResolutionStatus.AMBIGUOUS and "add" in two.note and "sub" in two.note  # 流れ非依存なので、代入された全部を候補にする（1つを選ばない）


def test_parameter_function_pointer_is_resolved_from_callers(local_calls) -> None:
    of, _ = local_calls
    applied = of("apply", "f")
    assert applied.resolution_status == ResolutionStatus.AMBIGUOUS  # 呼び出し元が渡す: mul・add・strcmp_like と、forward が転送する g（forward の呼び出し元が渡す sub）
    for name in ("mul", "add", "strcmp_like", "sub"):
        assert name in applied.note.split("［候補")[1]
    assert "この引数に、呼び出し元が渡している関数" in applied.note and "ほかに" not in applied.note  # 転送は、転送元の候補を引き継ぐ（関数名でない値ではない）


def test_local_copy_of_a_parameter_follows_the_forwarding(local_calls) -> None:
    of, _ = local_calls
    unknown = of("unknown_local", "op")
    assert unknown.resolution_status == ResolutionStatus.UNRESOLVED  # `op = h`（h は引数）。unknown_local は、プロジェクトの中で呼ばれないため、h に渡される関数が無い


FORWARD_SOURCE = """\
static int a(void) { return 1; }
static int b(void) { return 2; }
static int c(void) { return 3; }
int run(int (*f)(void)) { return f(); }
int mid(int (*g)(void)) { return run(g); }
int top(int (*h)(void)) { return mid(h); }
int loop_a(int (*p)(void), int n);
int loop_b(int (*q)(void), int n) { return n ? loop_a(q, n - 1) : q(); }
int loop_a(int (*p)(void), int n) { return loop_b(p, n); }
int unknown_arg(int (*get(void))(void)) { return run(get()); }
int main(void) { return top(a) + mid(b) + loop_a(c, 2); }
"""


def test_forwarding_is_followed_transitively_and_cycles_terminate(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "fw.c").write_text(FORWARD_SOURCE, encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    repository = AnalysisRepository(Path(db))
    project = repository.list_projects()[0]
    index = NavigationService(repository).load_index(project)
    by_name = {s.qualified_name: s for s in index.symbols.values()}

    def call(function: str, target: str):
        (found,) = [r for r in index.references if r.source_symbol_id == by_name[function].symbol_id and r.reference_kind == ReferenceKind.CALL and r.target_name == target]
        return found

    run = call("run", "f")  # run ← mid(g) ← top(h) ← main の a、mid ← main の b、unknown_arg が関数の戻り値を渡す
    assert run.resolution_status == ResolutionStatus.AMBIGUOUS
    note = run.note.split("［候補")[1]
    assert all(name in note for name in ("a", "b")) and "ほかに" in note  # 2段の転送も引き継ぐ。戻り値（get()）が渡されるので、全部は列挙できない
    cycle = call("loop_b", "q")  # loop_a ⇄ loop_b の循環を、止まって解決する（main が loop_a に c を渡す）
    assert cycle.resolution_status == ResolutionStatus.RESOLVED and index.symbols[cycle.target_symbol_id].name == "c"
    repository.close()

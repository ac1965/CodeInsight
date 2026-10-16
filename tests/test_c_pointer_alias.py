"""C の関数内の、ローカルポインタ経由の書き込み先の推定（1回だけ代入された先に限る。推定として区別し、追えない場合は追わない）。"""

from __future__ import annotations

import uuid

import clang.cindex as ci
import pytest

from codeinsight.analysis import c_flow_analysis as cf
from codeinsight.analysis.c_analyzer import CAnalyzer
from codeinsight.analysis.language_adapter import FileAnalysis, SourceUnit

SOURCE = """\
struct cfg { int level; };
int g_total;
static int s_buf[4];
struct cfg g_cfg;

void via_address(void) { int *p = &g_total; *p = 1; }
void via_assign(void) { struct cfg *c; c = &g_cfg; c->level = 2; }
void via_array(void) { int *q = s_buf; q[1] = 3; }
void via_param(struct cfg *arg) { struct cfg *c = arg; c->level = 4; }
void reassigned(int *other) { int *p = &g_total; p = other; *p = 5; }
void incremented(void) { int *p = s_buf; p++; *p = 6; }
void pointer_copied(int **out) { int *p = &g_total; *out = p; *p = 7; }
void pointer_address_taken(void) { int *p = &g_total; int **pp = &p; *pp = 0; *p = 7; }
void local_target(void) { int x; int *p = &x; *p = 8; }
void direct(void) { g_total = 9; }
"""


@pytest.fixture(scope="module")
def function(tmp_path_factory):
    path = (tmp_path_factory.mktemp("alias") / "alias.c").resolve()
    path.write_text(SOURCE, encoding="utf-8")
    data = path.read_bytes()
    translation_unit = CAnalyzer().parse(SourceUnit(str(uuid.uuid4()), path, "alias.c", data), FileAnalysis())
    assert translation_unit is not None

    def get(name: str) -> cf.CFunction:
        start = next(c.extent.start.line for c in translation_unit.cursor.get_children()
                     if c.kind == ci.CursorKind.FUNCTION_DECL and c.spelling == name and c.is_definition())
        found = cf.find_function(translation_unit, str(path), name, start, data)
        assert found is not None
        return found

    return get


def writes(function, name: str) -> list[tuple[str, str, bool]]:
    state = cf.analyze_state(function(name))
    return [(a.name, a.scope, "ポインタ" in a.detail) for a in state.accesses if a.mode == "write"] + [
        (m.parameter, "param", "ポインタ" in m.detail) for m in state.parameter_mutations
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("via_address", [("g_total", "global", True)]),
        ("via_assign", [("g_cfg", "global", True)]),
        ("via_array", [("s_buf", "static", True)]),
        ("via_param", [("arg", "param", True)]),
        ("pointer_copied", [("g_total", "global", True), ("out", "param", False)]),  # p の値を他へコピーしても、p の指す先は変わらない
    ],
)
def test_single_assignment_pointer_writes_are_attributed_and_marked_as_inferred(function, name: str, expected) -> None:
    assert writes(function, name) == expected  # 書き込み先を示し、「ポインタ経由（推定）」の印が付く


@pytest.mark.parametrize("name", ["reassigned", "incremented", "pointer_address_taken", "local_target"])
def test_untraceable_pointer_writes_are_not_attributed(function, name: str) -> None:
    assert [w for w in writes(function, name) if w[2]] == []  # 再代入・p++・&p（p の指す先を別経路で変えうる）・ローカルの対象は、推測で結び付けない


def test_direct_write_is_not_marked_as_pointer_inference(function) -> None:
    assert writes(function, "direct") == [("g_total", "global", False)]

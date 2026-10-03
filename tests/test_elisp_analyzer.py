from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from codeinsight.analysis.elisp_analyzer import ElispAnalyzer, _char_literal_end
from codeinsight.analysis.language_adapter import SourceUnit
from codeinsight.domain import Confidence, ReferenceKind, ResolutionStatus, SymbolKind

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "elisp_sample"


def _analyze(name: str):
    return ElispAnalyzer().analyze_file(SourceUnit.from_path(str(uuid.uuid4()), FIXTURE / name))


@pytest.mark.parametrize(
    ("text", "end"),
    [
        ("?a)", 2),
        ("?\\(x", 3),
        ("?\\C-a)", 5),
        ("?\\C-\\M-x)", 8),
        ("?\\s--)", 5),
        ("?\\s-?)", 5),
        ("?\\^\\\\ ", 5),
        ("?\\^\\] ", 5),
        ("?\\x41)", 5),
    ],
)
def test_char_literal_end(text: str, end: int) -> None:
    assert _char_literal_end(text, 0) == end


def test_extracts_definitions() -> None:
    result = _analyze("core.el")
    assert result.succeeded, result.errors
    kinds = {s.name: s.kind for s in result.symbols}
    assert kinds["core-greet"] == SymbolKind.FUNCTION
    assert kinds["core-with-log"] == SymbolKind.MACRO
    assert kinds["core-item"] == SymbolKind.STRUCT
    assert kinds["core-counter"] == SymbolKind.GLOBAL_VARIABLE
    assert kinds["core-mode"] == SymbolKind.FUNCTION
    # 文字リテラルの括弧・文字列中の括弧で、解析がずれない
    assert next(s for s in result.symbols if s.name == "core-greet").start_line == 15


def test_calls_ignore_locals_and_quoted_data() -> None:
    result = _analyze("core.el")
    calls = {r.target_name for r in result.references if r.reference_kind == ReferenceKind.CALL}
    assert {"util-log", "util-helper", "core-open-paren-p", "core-item-create"} <= calls
    assert "msg" not in calls  # let の局所変数
    assert "not-a-call" not in calls  # クォートされたデータ
    functions = {r.target_name for r in result.references if r.reference_kind == ReferenceKind.FUNCTION_REF}
    assert "core-greet" in functions  # #'core-greet


def test_macro_template_calls_are_recorded() -> None:
    result = _analyze("core.el")
    macro = next(s for s in result.symbols if s.name == "core-with-log")
    assert any(
        r.target_name == "util-log" and r.source_symbol_id == macro.symbol_id for r in result.references
    )


def test_unbalanced_parens_fail_not_silently() -> None:
    result = _analyze("broken.el")
    assert not result.succeeded
    assert any("括弧" in e for e in result.errors)


def test_non_utf8_file_is_decoded_with_warning(tmp_path: Path) -> None:
    path = tmp_path / "latin.el"
    path.write_bytes("(defun latin-fn () \"caf\xe9\")\n".encode("latin-1"))
    result = ElispAnalyzer().analyze_file(SourceUnit.from_path(str(uuid.uuid4()), path))
    assert result.succeeded, result.errors
    assert any(s.name == "latin-fn" for s in result.symbols)
    assert result.warnings


def test_resolution_is_inferred_ambiguous_or_external(analyzed) -> None:
    repo, project, _ = analyzed(FIXTURE)
    symbols = {s.symbol_id: s for s in repo.list_symbols_for_project(project.project_id)}
    refs = [r for r in repo.list_references_for_project(project.project_id) if r.reference_kind == ReferenceKind.CALL]

    def of(source: str, target: str):
        return [r for r in refs if symbols[r.source_symbol_id].qualified_name.endswith(source) and r.target_name == target]

    (log,) = of("core-greet", "util-log")
    assert log.resolution_status == ResolutionStatus.RESOLVED
    assert log.confidence == Confidence.INFERRED  # 名前の一致のみ。確定にしない
    (fmt,) = of("core-greet", "format")
    assert fmt.resolution_status == ResolutionStatus.EXTERNAL
    (plat,) = of("extras-call", "plat-fn")
    assert plat.resolution_status == ResolutionStatus.AMBIGUOUS  # 条件付きの二重定義。1つを選ばない
    (ext,) = of("extras-call", "undefined-external-fn")
    assert ext.resolution_status == ResolutionStatus.EXTERNAL

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from codeinsight.ai.context import ContextBuilder
from codeinsight.ai.evaluation import _Workspace
from codeinsight.ai.retrieval import normalize, query_terms, retrieve_symbols, split_identifier

CASES = Path(__file__).resolve().parent.parent / "eval" / "retrieval_cases.toml"
TOP_K = 4


def _search(project, index, navigation, question: str) -> list[str]:
    builder = ContextBuilder(navigation)
    symbols = retrieve_symbols(index, question, TOP_K, read_lines=lambda path: builder._try_read(project, index, path))
    return [s.qualified_name for s in symbols]


def test_identifiers_are_split_and_normalized() -> None:
    assert split_identifier("place_order") == ["place", "order", "placeorder"]
    assert split_identifier("ThreadPoolExecutor")[:3] == ["thread", "pool", "executor"]
    assert normalize("orders") == "order" and normalize("retries") == "retry" and normalize("applied") == "apply"
    assert normalize("is") == "is" and normalize("class") == "class"  # 短い語・語尾の ss は壊さない


def test_japanese_terms_expand_to_english_words_and_generic_words_are_dropped() -> None:
    latin, japanese = query_terms("環境変数 APP_TOKEN を読む関数は?")
    assert {"environ", "getenv", "app", "token"} <= latin and "関数" not in japanese
    assert query_terms("この処理の仕組みを説明してください") == (set(), set())


def test_ranking_uses_the_body_and_ignores_stale_files(tmp_path: Path) -> None:
    workspace = _Workspace()
    try:
        project_dir = str(Path(__file__).resolve().parent / "fixtures" / "python_flow")
        _, project, index, navigation = workspace.get(project_dir)
        builder = ContextBuilder(navigation)
        names = [s.qualified_name for s in retrieve_symbols(index, "pickle", 3, read_lines=lambda p: builder._try_read(project, index, p))]
        assert names[:1] == ["risky.load"]  # 名前にも docstring にも無い語を、本文から見つける
        assert retrieve_symbols(index, "pickle", 3) == []  # 本文を読まない場合は見つからない（名前・docstringのみ）
        assert retrieve_symbols(index, "pickle", 3, read_lines=lambda p: None) == []  # 読めないファイルの本文は使わない
    finally:
        workspace.close()


def test_retrieval_benchmark_meets_the_recorded_level() -> None:
    cases = tomllib.loads(CASES.read_text(encoding="utf-8"))["case"]
    workspace = _Workspace()
    hits = 0
    misses: list[str] = []
    try:
        for case in cases:
            _, project, index, navigation = workspace.get(str((CASES.parent / case["project"]).resolve()))
            found = _search(project, index, navigation, case["q"])
            if any(e in found for e in case["expect"]):
                hits += 1
            else:
                misses.append(case["q"])
    finally:
        workspace.close()
    # 基準は、検索の改良前（名前・docstringのみ）が 2/16。大きく下がったら改悪として検出する
    assert hits / len(cases) >= 0.85, f"recall@{TOP_K} が低下: {hits}/{len(cases)} 外れ: {misses}"


@pytest.mark.parametrize("question", ["", "   ", "これは何ですか"])
def test_questions_without_content_words_select_nothing(question: str) -> None:
    workspace = _Workspace()
    try:
        _, _, index, _ = workspace.get(str(Path(__file__).resolve().parent / "fixtures" / "python_flow"))
        assert retrieve_symbols(index, question) == []
    finally:
        workspace.close()

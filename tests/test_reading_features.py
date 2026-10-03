from __future__ import annotations

import json
import uuid
from pathlib import Path

from codeinsight.analysis.c_analyzer import CAnalyzer
from codeinsight.analysis.language_adapter import SourceUnit
from codeinsight.application import (
    DescribeService,
    NavigationService,
    OverviewService,
)
from codeinsight.cli import main
from codeinsight.domain import Confidence, ReferenceKind, ResolutionStatus, SymbolKind


def _setup(analyzed, root: Path):
    repo, project, result = analyzed(root)
    nav = NavigationService(repo)
    return project, nav, nav.load_index(project)


def _names(index, kind: ReferenceKind, source: str) -> dict[str, object]:
    out = {}
    for r in index.references:
        if r.reference_kind == kind and index.symbols[r.source_symbol_id].qualified_name == source:
            out.setdefault(r.target_name, []).append(r)
    return out


# --- 要約（docstring / ドキュメントコメントの転記） ---


def test_python_summaries_are_copied_from_docstrings(analyzed, python_doc_dir: Path) -> None:
    _, _, index = _setup(analyzed, python_doc_dir)
    by_name = {s.qualified_name: s for s in index.symbols.values()}

    assert by_name["pkg"].summary == "サンプルパッケージ。"
    assert by_name["pkg.config"].summary == "設定値を提供するモジュール。"  # 複数行のdocstringは先頭行のみ
    assert by_name["pkg.config.Settings"].summary == "利用者ごとの設定。"
    assert by_name["pkg.config.Settings.effective"].summary == "有効なタイムアウトを返す。"
    assert by_name["pkg.client.no_doc"].summary == ""  # docstringが無ければ空（創作しない）


def test_section_header_is_not_used_as_a_summary(tmp_path: Path, analyzed) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "a.py").write_text('def f(x):\n    """\n    Args:\n        x: 値\n    """\n')
    _, _, index = _setup(analyzed, root)
    (function,) = [s for s in index.symbols.values() if s.name == "f"]
    assert function.summary == ""


def test_c_brief_comment_is_used_as_a_summary(c_doc_dir: Path) -> None:
    result = CAnalyzer().analyze_file(SourceUnit.from_path(str(uuid.uuid4()), c_doc_dir / "api.h"))
    summaries = {s.name: s.summary for s in result.symbols if s.kind != SymbolKind.MACRO}
    assert summaries["add"] == "Adds two integers."
    # 複数行のコメントは、libclangが段落として連結した内容になる（先頭の段落全体）
    assert summaries["mul"].startswith("Multiplies two integers.")
    assert summaries["undocumented"] == ""


# --- 名前の参照・型注釈の参照 ---


def test_python_name_references_and_annotations_are_recorded(
    analyzed, python_doc_dir: Path
) -> None:
    _, _, index = _setup(analyzed, python_doc_dir)
    targets = {s.symbol_id: s.qualified_name for s in index.symbols.values()}

    fetch_names = _names(index, ReferenceKind.NAME_REF, "pkg.client.fetch")
    assert "DEFAULT_TIMEOUT" in fetch_names  # 変数の参照
    # 引数の既定値・型注釈は、読む人にとって関数のシグネチャが使うものなので、関数からの参照になる
    assert "config.RETRIES" in fetch_names
    assert "fetch" in fetch_names  # 関数を値として使う（呼び出しではない）
    assert "handler" not in fetch_names  # ローカル変数は記録しない
    assert "os.getcwd" not in fetch_names  # 標準ライブラリは記録しない
    (default_timeout,) = fetch_names["DEFAULT_TIMEOUT"]
    assert targets[default_timeout.target_symbol_id] == "pkg.config.DEFAULT_TIMEOUT"
    assert default_timeout.resolution_status == ResolutionStatus.RESOLVED
    assert default_timeout.confidence == Confidence.CONFIRMED

    type_uses = _names(index, ReferenceKind.TYPE_USE, "pkg.client.fetch")
    assert len(type_uses["Settings"]) == 2  # Optional[Settings] と "Settings | None"
    assert {r.reference_kind for r in type_uses["Settings"]} == {ReferenceKind.TYPE_USE}
    assert "Optional" not in type_uses  # typing は記録しない
    assert targets[type_uses["Settings"][0].target_symbol_id] == "pkg.config.Settings"
    assert "config.Settings" in _names(index, ReferenceKind.TYPE_USE, "pkg.client.no_doc")

    # クラス変数の初期値からの参照は、クラスからの参照として記録される
    assert "DEFAULT_TIMEOUT" in _names(index, ReferenceKind.NAME_REF, "pkg.config.Settings")


def test_call_targets_are_not_duplicated_as_name_references(
    analyzed, python_doc_dir: Path
) -> None:
    _, _, index = _setup(analyzed, python_doc_dir)
    main_refs = [
        r
        for r in index.references
        if index.symbols[r.source_symbol_id].qualified_name == "pkg.__main__.main"
    ]
    assert [(r.reference_kind, r.target_name) for r in main_refs] == [(ReferenceKind.CALL, "fetch")]


def test_references_to_a_python_variable_find_all_uses(analyzed, python_doc_dir: Path) -> None:
    _, nav, index = _setup(analyzed, python_doc_dir)
    variable = nav.resolve_symbol(index, "pkg.config.DEFAULT_TIMEOUT").symbol
    users = {
        (h.path, h.reference.reference_kind.value, h.source.qualified_name)
        for h in nav.references_to(index, variable)
    }
    assert ("pkg/client.py", "name_ref", "pkg.client.fetch") in users
    assert ("pkg/config.py", "name_ref", "pkg.config.Settings") in users
    assert ("pkg/client.py", "import", "pkg.client") in users  # importしている箇所


# --- describe ---


def test_describe_collects_facts_for_a_class(analyzed, python_doc_dir: Path) -> None:
    project, nav, index = _setup(analyzed, python_doc_dir)
    symbol = nav.resolve_symbol(index, "pkg.config.Settings").symbol
    description = DescribeService(nav).describe(project, index, symbol)

    assert description.declaration == [(10, "class Settings:")]
    assert [m.name for m in description.members] == ["timeout", "effective"]
    # fetch の2つの注釈 + no_doc + long_signature
    assert description.references[ReferenceKind.TYPE_USE] == 4
    assert description.referencing_files >= 1
    assert description.callees_include_members  # クラスはメンバの呼び出しを合計する


def test_describe_reads_a_multi_line_declaration(analyzed, python_doc_dir: Path) -> None:
    project, nav, index = _setup(analyzed, python_doc_dir)
    symbol = nav.resolve_symbol(index, "pkg.client.long_signature").symbol
    declaration = DescribeService(nav).describe(project, index, symbol).declaration

    assert [n for n, _ in declaration] == list(range(declaration[0][0], declaration[0][0] + 6))
    assert declaration[0][1] == "def long_signature("
    assert declaration[-1][1] == ") -> int:"  # 括弧が閉じて ':' で終わる行まで


def test_describe_counts_callers_and_callees(analyzed, c_callgraph_dir: Path) -> None:
    project, nav, index = _setup(analyzed, c_callgraph_dir)
    symbol = nav.resolve_symbol(index, "main").symbol
    description = DescribeService(nav).describe(project, index, symbol)

    assert description.callees[ResolutionStatus.RESOLVED] == 3  # apply, helper, fib
    assert description.callees[ResolutionStatus.UNRESOLVED] == 1  # 関数ポインタ
    assert description.callees[ResolutionStatus.EXTERNAL] == 1  # printf
    assert description.callers == 0


# --- overview ---


def test_overview_summarises_an_unfamiliar_repository(analyzed, python_doc_dir: Path) -> None:
    _, nav, index = _setup(analyzed, python_doc_dir)
    overview = OverviewService(nav).build(index, top=5)

    assert {h.symbol.qualified_name for h in overview.entry_points} == {
        "pkg.__main__",
        "pkg.__main__.main",
    }
    config = next(m for m in overview.modules if m.path == "pkg/config.py")
    assert config.summary == "設定値を提供するモジュール。"
    assert config.fan_in >= 1  # clientから依存されている
    called = {r.hit.symbol.qualified_name: r.value for r in overview.most_called}
    assert called["pkg.client.fetch"] == 1 and called["pkg.__main__.main"] == 1
    assert overview.cycles == []
    assert sum(overview.reference_status.values()) > 0


# --- CLI ---


def test_cli_describe_overview_show_symbol_and_tree_path(
    tmp_path: Path, python_doc_dir: Path, capsys
) -> None:
    db = str(tmp_path / "r.sqlite")
    assert main(["analyze", str(python_doc_dir), "--db", db]) == 0
    capsys.readouterr()

    def run(*argv: str):
        code = main([*argv, "--db", db])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    code, out, _ = run("describe", "pkg.config.Settings")
    assert code == 0
    assert "要約: 利用者ごとの設定。" in out and "class Settings:" in out
    assert "呼び出し先（メンバの合計）" in out and "参照:" in out

    code, out, _ = run("describe", "pkg.config.Settings", "--format", "json")
    data = json.loads(out)
    assert data["summary"] == "利用者ごとの設定。" and data["callees_include_members"] is True

    code, out, _ = run("overview")
    assert code == 0
    assert "pkg/config.py" in out and "設定値を提供するモジュール。" in out
    assert "pkg.__main__.main" in out and "静的解析で確認できた事実のみ" in out

    code, out, _ = run("show", "pkg.config.Settings.effective")
    assert code == 0
    assert "(method, pkg/config.py:" in out and "return self.timeout" in out

    code, out, _ = run("tree", "pkg/config.py", "--no-variables")
    assert "config.py" in out and "client.py" not in out
    code, _, err = run("tree", "nope/")
    assert code == 2 and "一致するファイルがありません" in err


def test_cli_callees_can_hide_external_calls(
    tmp_path: Path, c_callgraph_dir: Path, capsys
) -> None:
    db = str(tmp_path / "h.sqlite")
    main(["analyze", str(c_callgraph_dir), "--db", db])
    capsys.readouterr()
    main(["callees", "main", "--hide-external", "--db", db])
    out = capsys.readouterr().out
    assert "printf" not in out and "apply" in out

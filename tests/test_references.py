from __future__ import annotations

from pathlib import Path

from codeinsight.domain import (
    Confidence,
    ReferenceKind,
    ResolutionStatus,
)


def _view(repo, project):
    files = {f.file_id: f.relative_path for f in repo.list_source_files(project.project_id)}
    symbols = {s.symbol_id: s for s in repo.list_symbols_for_project(project.project_id)}
    refs = repo.list_references_for_project(project.project_id)

    def find(kind, source, target_name, path=None):
        return [
            r
            for r in refs
            if r.reference_kind == kind
            and symbols[r.source_symbol_id].qualified_name == source
            and r.target_name == target_name
            and (path is None or files[r.source_location.file_id] == path)
        ]

    def target(ref):
        return symbols[ref.target_symbol_id].qualified_name if ref.target_symbol_id else None

    return files, symbols, refs, find, target


def test_c_direct_calls_resolve_across_files(analyzed, c_callgraph_dir: Path) -> None:
    repo, project, result = analyzed(c_callgraph_dir)
    files, symbols, refs, find, target = _view(repo, project)

    (call_apply,) = find(ReferenceKind.CALL, "main", "apply")
    assert call_apply.resolution_status == ResolutionStatus.RESOLVED
    # 呼び出し先は、ヘッダーの宣言ではなくops.cの定義に解決される
    assert files[symbols[call_apply.target_symbol_id].file_id] == "ops.c"

    (call_helper,) = find(ReferenceKind.CALL, "main", "helper")
    assert call_helper.resolution_status == ResolutionStatus.RESOLVED
    assert target(call_helper) == "helper"


def test_c_function_pointer_calls_are_unresolved(analyzed, c_callgraph_dir: Path) -> None:
    repo, project, result = analyzed(c_callgraph_dir)
    _, _, _, find, _ = _view(repo, project)

    (via_variable,) = find(ReferenceKind.CALL, "main", "op")
    assert via_variable.resolution_status == ResolutionStatus.UNRESOLVED
    assert via_variable.target_symbol_id is None
    assert "関数ポインタ" in via_variable.note

    (via_member,) = find(ReferenceKind.CALL, "apply", "op")
    assert via_member.resolution_status == ResolutionStatus.UNRESOLVED
    assert "関数ポインタ" in via_member.note


def test_c_function_address_is_recorded_as_function_ref_not_call(
    analyzed, c_callgraph_dir: Path
) -> None:
    repo, project, result = analyzed(c_callgraph_dir)
    _, _, _, find, target = _view(repo, project)

    (ref_add,) = find(ReferenceKind.FUNCTION_REF, "main", "add")
    assert target(ref_add) == "add"
    assert not find(ReferenceKind.CALL, "main", "add")


def test_c_recursion_and_external_calls(analyzed, c_callgraph_dir: Path) -> None:
    repo, project, result = analyzed(c_callgraph_dir)
    _, _, _, find, target = _view(repo, project)

    recursive = find(ReferenceKind.CALL, "fib", "fib")
    assert len(recursive) == 2  # fib(n-1) と fib(n-2)
    assert all(target(r) == "fib" for r in recursive)

    (printf,) = find(ReferenceKind.CALL, "main", "printf")
    assert printf.resolution_status == ResolutionStatus.EXTERNAL


def test_c_inactive_conditional_branch_is_not_extracted(analyzed, c_callgraph_dir: Path) -> None:
    repo, project, result = analyzed(c_callgraph_dir)
    names = {s.name for s in repo.list_symbols_for_project(project.project_id)}
    assert "trace" not in names  # USE_DEBUG が未定義のため非アクティブ


def test_c_include_dependencies(analyzed, c_callgraph_dir: Path) -> None:
    repo, project, result = analyzed(c_callgraph_dir)
    files = {f.file_id: f.relative_path for f in repo.list_source_files(project.project_id)}
    deps = {
        (files[d.source_file_id], d.target_name): d
        for d in repo.list_dependencies_for_project(project.project_id)
    }

    internal = deps[("main.c", "ops.h")]
    assert internal.resolution_status == ResolutionStatus.RESOLVED
    assert files[internal.target_file_id] == "ops.h"
    assert deps[("main.c", "stdio.h")].resolution_status == ResolutionStatus.EXTERNAL


def test_python_name_resolution_across_modules(analyzed, python_pkg_dir: Path) -> None:
    repo, project, result = analyzed(python_pkg_dir)
    _, _, _, find, target = _view(repo, project)

    (ctor,) = find(ReferenceKind.CALL, "app.core.run", "Derived")
    assert target(ctor) == "app.models.Derived"
    assert ctor.confidence == Confidence.CONFIRMED

    (helper,) = find(ReferenceKind.CALL, "app.core.run", "util.helper")
    assert target(helper) == "app.util.helper"

    (inner,) = find(ReferenceKind.CALL, "app.core.fetch", "_inner")
    assert target(inner) == "app.core._inner"


def test_python_self_and_super_calls_are_inferred(analyzed, python_pkg_dir: Path) -> None:
    repo, project, result = analyzed(python_pkg_dir)
    _, _, _, find, target = _view(repo, project)

    (via_self,) = find(ReferenceKind.CALL, "app.models.Base.hello", "self.greet")
    assert target(via_self) == "app.models.Base.greet"
    assert via_self.confidence == Confidence.INFERRED  # サブクラスで上書きされうる

    (via_super,) = find(ReferenceKind.CALL, "app.models.Derived.greet", "super().greet")
    assert target(via_super) == "app.models.Base.greet"
    assert via_super.confidence == Confidence.INFERRED


def test_python_inheritance_is_resolved(analyzed, python_pkg_dir: Path) -> None:
    repo, project, result = analyzed(python_pkg_dir)
    _, _, _, find, target = _view(repo, project)

    (inheritance,) = find(ReferenceKind.INHERITANCE, "app.models.Derived", "Base")
    assert target(inheritance) == "app.models.Base"
    assert inheritance.resolution_status == ResolutionStatus.RESOLVED


def test_python_dynamic_calls_are_unresolved(analyzed, python_pkg_dir: Path) -> None:
    repo, project, result = analyzed(python_pkg_dir)
    _, _, refs, find, _ = _view(repo, project)

    (handler,) = find(ReferenceKind.CALL, "app.core.run", "handler")
    assert handler.resolution_status == ResolutionStatus.UNRESOLVED

    dynamic = [r for r in refs if "getattr による" in r.note]
    assert len(dynamic) == 1
    assert dynamic[0].resolution_status == ResolutionStatus.UNRESOLVED
    assert dynamic[0].target_symbol_id is None


def test_python_decorated_target_is_inferred(analyzed, python_pkg_dir: Path) -> None:
    repo, project, result = analyzed(python_pkg_dir)
    _, _, _, find, target = _view(repo, project)

    (call_run,) = find(ReferenceKind.CALL, "app.plugin.start", "core.run")
    assert target(call_run) == "app.core.run"
    assert call_run.confidence == Confidence.INFERRED
    assert "デコレータ" in call_run.note


def test_python_import_dependencies_and_dynamic_import_warning(
    analyzed, python_pkg_dir: Path
) -> None:
    repo, project, result = analyzed(python_pkg_dir)
    files = {f.file_id: f.relative_path for f in repo.list_source_files(project.project_id)}
    deps = [d for d in repo.list_dependencies_for_project(project.project_id) if d.is_visible]
    edges = {
        (files[d.source_file_id], files.get(d.target_file_id)) for d in deps if d.target_file_id
    }

    assert ("app/core.py", "app/plugin.py") in edges
    assert ("app/plugin.py", "app/core.py") in edges  # 循環import
    assert ("app/core.py", "app/models.py") in edges

    external = [d for d in deps if d.target_name == "os"]
    assert external and external[0].resolution_status == ResolutionStatus.EXTERNAL

    dynamic = [d for d in deps if d.target_name == "<dynamic>"]
    assert dynamic and dynamic[0].resolution_status == ResolutionStatus.UNRESOLVED
    assert any("動的インポート" in w for w in result.warnings)

    # 解決できなかった候補（from X import name の name がモジュールでない）は表示しない
    assert not any(d.target_name == "app.models.Base" for d in deps)


def test_python_module_names_follow_package_path(analyzed, python_pkg_dir: Path) -> None:
    repo, project, result = analyzed(python_pkg_dir)
    names = {s.qualified_name for s in repo.list_symbols_for_project(project.project_id)}
    assert {"app", "app.core", "app.models", "app.util"} <= names


def test_python_receiver_type_is_inferred_from_annotation_or_single_assignment(
    analyzed, python_pkg_dir: Path
) -> None:
    repo, project, result = analyzed(python_pkg_dir)
    _, _, _, find, target = _view(repo, project)

    # 型注釈（文字列注釈を含む）から推定
    (annotated,) = find(ReferenceKind.CALL, "app.util.describe", "shape.greet")
    assert target(annotated) == "app.models.Derived.greet"
    assert annotated.confidence == Confidence.INFERRED

    # `obj = Derived()` の単一代入から推定。継承元のメソッドも辿れる
    (assigned,) = find(ReferenceKind.CALL, "app.util.make", "obj.greet")
    assert target(assigned) == "app.models.Derived.greet"
    (inherited,) = find(ReferenceKind.CALL, "app.core.run", "obj.hello")
    assert target(inherited) == "app.models.Base.hello"
    assert inherited.confidence == Confidence.INFERRED


def test_python_untyped_or_unknown_type_stays_unresolved(analyzed, python_pkg_dir: Path) -> None:
    repo, project, result = analyzed(python_pkg_dir)
    _, _, _, find, _ = _view(repo, project)

    for name in ("thing.greet", "other.greet"):
        (ref,) = find(ReferenceKind.CALL, "app.util.untyped", name)
        assert ref.resolution_status == ResolutionStatus.UNRESOLVED
        assert ref.target_symbol_id is None


def test_python_reexport_through_package_init_is_followed(
    analyzed, python_reexport_dir: Path
) -> None:
    repo, project, result = analyzed(python_reexport_dir)
    _, _, _, find, target = _view(repo, project)

    (ctor,) = find(ReferenceKind.CALL, "main", "Widget", "main.py")
    assert target(ctor) == "lib.models.Widget"
    assert ctor.confidence == Confidence.CONFIRMED
    assert "再エクスポート" in ctor.note

    (make,) = find(ReferenceKind.CALL, "main.go", "make_widget")
    assert target(make) == "lib.models.make_widget"

    # import文そのものも、定義先へ解決されるIMPORT参照として記録される
    (imported,) = [
        r
        for r in find(ReferenceKind.IMPORT, "main", "lib.Widget")
    ]
    assert target(imported) == "lib.models.Widget"


def test_python_receiver_type_follows_reexported_class(
    analyzed, python_reexport_dir: Path
) -> None:
    repo, project, result = analyzed(python_reexport_dir)
    _, _, _, find, target = _view(repo, project)

    (via_variable,) = find(ReferenceKind.CALL, "main.go", "widget.run")  # モジュール変数 widget = Widget()
    assert target(via_variable) == "lib.models.Widget.run"
    assert via_variable.confidence == Confidence.INFERRED
    (via_constructed,) = find(ReferenceKind.CALL, "main.go", "Widget().run")
    assert target(via_constructed) == "lib.models.Widget.run"


def test_python_star_import_names_are_resolved_via_source_module(
    analyzed, python_reexport_dir: Path
) -> None:
    repo, project, result = analyzed(python_reexport_dir)
    _, _, _, find, target = _view(repo, project)

    (helper,) = find(ReferenceKind.CALL, "lib.cli.main", "shared_helper")
    assert target(helper) == "lib._shared.shared_helper"

    # star import元にも無い名前は、推測せず未解決のまま
    (unknown,) = find(ReferenceKind.CALL, "lib.cli.main", "undefined_name")
    assert unknown.resolution_status == ResolutionStatus.UNRESOLVED
    assert unknown.target_symbol_id is None


def test_python_builtin_and_external_receivers_are_not_reported_as_unresolved(
    analyzed, python_reexport_dir: Path
) -> None:
    repo, project, result = analyzed(python_reexport_dir)
    _, _, _, find, _ = _view(repo, project)

    for source, name, expected in (
        ("lib.cli.main", "names.append", "組み込み型(list)"),  # names = [] の単一代入
        ("lib.cli.main", "', '.join", "組み込み型"),  # リテラルのメソッド
        ("lib.cli", "app.command", "extlib.Typer"),  # app = extlib.Typer() （外部ライブラリの型）
    ):
        (ref,) = find(ReferenceKind.CALL, source, name)
        assert ref.resolution_status == ResolutionStatus.EXTERNAL
        assert expected in ref.note


def test_python_instance_attribute_types_are_inferred_only_when_unambiguous(
    analyzed, python_reexport_dir: Path
) -> None:
    repo, project, result = analyzed(python_reexport_dir)
    _, _, _, find, target = _view(repo, project)
    holder_go = "lib.models.Holder.go"

    # 型注釈付き引数の転記 / 生成するクラス
    (via_param,) = find(ReferenceKind.CALL, holder_go, "self.widget.run")
    assert target(via_param) == "lib.models.Widget.run"
    assert via_param.confidence == Confidence.INFERRED
    (via_constructed,) = find(ReferenceKind.CALL, holder_go, "self.other.run")
    assert target(via_constructed) == "lib.models.Widget.run"

    # 組み込み型（リテラル・注釈）のメソッドは外部として分類する
    for name in ("self.label.upper", "self.cache.get", "self.items.append"):
        (builtin,) = find(ReferenceKind.CALL, holder_go, name)
        assert builtin.resolution_status == ResolutionStatus.EXTERNAL

    # 複数回代入される属性（self.counter += 1）は、型を推定せず未解決のまま
    (reassigned,) = find(ReferenceKind.CALL, holder_go, "self.counter.bit_length")
    assert reassigned.resolution_status == ResolutionStatus.UNRESOLVED


def test_python_module_level_chained_assignments_do_not_break_analysis(
    analyzed, tmp_path: Path
) -> None:
    # 回帰: モジュール変数の型推定中に、別のモジュール変数のメソッド呼び出しを評価する
    root = tmp_path / "chain"
    root.mkdir()
    (root / "m.py").write_text(
        "class K:\n    def make(self):\n        return K()\n\n\n"
        "k = K()\nvalue = k.make()\nitems = []\ncount = len(items)\n"
    )
    repo, project, result = analyzed(root)
    assert not result.errors, result.errors
    _, _, _, find, target = _view(repo, project)
    (make,) = find(ReferenceKind.CALL, "m", "k.make")
    assert target(make) == "m.K.make"


def test_function_local_import_names_are_resolved(analyzed, tmp_path: Path) -> None:
    # 遅延import（関数内import）で取り込んだ名前の呼び出しを、ローカル変数扱いで未解決にしない
    root = tmp_path / "lazy"
    root.mkdir()
    (root / "heavy.py").write_text("def build():\n    return 1\n")
    (root / "main.py").write_text(
        "def run():\n    from heavy import build\n    return build()\n"
    )
    repo, project, result = analyzed(root)
    _, _, _, find, target = _view(repo, project)
    (call,) = find(ReferenceKind.CALL, "main.run", "build")
    assert target(call) == "heavy.build"


def test_result_of_a_function_call_is_not_mistaken_for_a_class_instance(
    analyzed, tmp_path: Path
) -> None:
    root = tmp_path / "ret"
    root.mkdir()
    (root / "m.py").write_text(
        "def make():\n    return object()\n\n\ndef use():\n    value = make()\n    return value.run()\n"
    )
    repo, project, result = analyzed(root)
    _, _, _, find, _ = _view(repo, project)
    (ref,) = find(ReferenceKind.CALL, "m.use", "value.run")
    assert ref.resolution_status == ResolutionStatus.UNRESOLVED
    assert "関数であり、戻り値の型を静的に確定できない" in ref.note

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

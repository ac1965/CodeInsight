from __future__ import annotations

from pathlib import Path

import pytest

from codeinsight.domain import Confidence, ReferenceKind, ResolutionStatus, SymbolKind

FILES = {
    "shapes.hpp": """\
#pragma once

namespace geo {

class Shape {
public:
    Shape(int id) : id_(id) {}
    virtual ~Shape() {}
    virtual double area() const = 0;
    int id() const;
protected:
    int id_;
};

class Circle : public Shape {
public:
    Circle(int id, double r);
    double area() const override;
private:
    double r_;
};

template <typename T>
T twice(T value) { return value + value; }

}  // namespace geo
""",
    "shapes.cpp": """\
#include "shapes.hpp"
#include <cstring>

namespace geo {

int Shape::id() const { return id_; }

Circle::Circle(int id, double r) : Shape(id), r_(r) {}

double Circle::area() const { return 3.14 * r_ * r_; }

unsigned long label_length(const char *text) { return std::strlen(text); }

}  // namespace geo
""",
    "main.cpp": """\
#include "shapes.hpp"

double total(const geo::Shape &shape) {
    return shape.area() + geo::twice(1.0);
}

int main() {
    geo::Circle circle(1, 2.0);
    return static_cast<int>(total(circle)) + circle.id();
}
""",
}


@pytest.fixture
def view(analyzed, tmp_path: Path):
    root = tmp_path / "cpp"
    root.mkdir()
    for name, text in FILES.items():
        (root / name).write_text(text, encoding="utf-8")
    repo, project, result = analyzed(root)
    symbols = {s.symbol_id: s for s in repo.list_symbols_for_project(project.project_id)}
    refs = repo.list_references_for_project(project.project_id)
    files = {f.file_id: f.relative_path for f in repo.list_source_files(project.project_id)}

    def symbol(qualified: str, path: str | None = None, kind: SymbolKind | None = None):
        found = [s for s in symbols.values() if s.qualified_name == qualified and (path is None or files[s.file_id] == path) and (kind is None or s.kind == kind)]
        assert len(found) == 1, (qualified, path, [(files[s.file_id], s.kind) for s in found])
        return found[0]

    def call(source: str, name: str):
        (found,) = [r for r in refs if r.reference_kind == ReferenceKind.CALL and symbols[r.source_symbol_id].qualified_name == source and r.target_name == name]
        return found

    def target(reference) -> str | None:
        return symbols[reference.target_symbol_id].qualified_name if reference.target_symbol_id else None

    return symbols, refs, symbol, call, target, result


def test_extracts_namespaces_classes_methods_and_templates(view) -> None:
    _, _, symbol, _, _, result = view
    assert result.status.value in ("success", "partial")
    assert symbol("geo", "shapes.hpp").kind == SymbolKind.NAMESPACE
    shape = symbol("geo::Shape", None, SymbolKind.CLASS)
    circle = symbol("geo::Circle", None, SymbolKind.CLASS)
    assert circle.base_classes == ("Shape",) and shape.kind == SymbolKind.CLASS
    assert symbol("geo::twice").kind == SymbolKind.FUNCTION  # 関数テンプレートの定義
    # クラスの外で定義されたメソッドも、クラスの修飾名で抽出する
    assert symbol("geo::Shape::id", "shapes.cpp").kind == SymbolKind.METHOD
    assert symbol("geo::Circle::Circle", "shapes.cpp").kind == SymbolKind.METHOD  # コンストラクタ
    assert symbol("geo::Circle::area", "shapes.cpp").kind == SymbolKind.METHOD
    assert symbol("geo::Circle::area", "shapes.hpp").kind == SymbolKind.FUNCTION_DECLARATION  # クラス内の宣言


def test_inheritance_and_calls_resolve_to_definitions(view) -> None:
    symbols, refs, symbol, call, target, _ = view
    inheritance = [r for r in refs if r.reference_kind == ReferenceKind.INHERITANCE]
    assert [(symbols[r.source_symbol_id].qualified_name, target(r)) for r in inheritance] == [("geo::Circle", "geo::Shape")]
    constructor = call("main", "Circle")
    assert constructor.resolution_status == ResolutionStatus.RESOLVED and target(constructor) == "geo::Circle::Circle"
    assert symbols[constructor.target_symbol_id].kind == SymbolKind.METHOD  # 宣言ではなく、cppの定義に解決する
    assert target(call("main", "id")) == "geo::Shape::id"
    assert target(call("main", "total")) == "total"


def test_virtual_and_template_calls_are_inferred_and_system_functions_external(view) -> None:
    _, _, _, call, target, _ = view
    virtual = call("total", "area")
    assert virtual.resolution_status == ResolutionStatus.RESOLVED and virtual.confidence == Confidence.INFERRED
    assert "仮想関数" in virtual.note and target(virtual) == "geo::Shape::area"  # 派生クラスのオーバーライドになりうる
    template = call("total", "twice")
    assert template.confidence == Confidence.INFERRED and target(template) == "geo::twice"
    strlen = call("geo::label_length", "strlen")
    assert strlen.resolution_status == ResolutionStatus.EXTERNAL and strlen.target_symbol_id is None

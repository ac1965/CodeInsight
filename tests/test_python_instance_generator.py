"""Pythonのインスタンス変数（`self.X = ...`）とジェネレータのシンボル化。"""

from __future__ import annotations

import json
from pathlib import Path

from codeinsight.cli import main
from codeinsight.domain import ResolutionStatus, SymbolKind

SOURCE = '''\
class Base:
    shared = 1

    def __init__(self, name, items):
        self.name = name
        self.items: list = items
        self.count = 0
        self.a, (self.b, *self.rest) = 1, (2, 3)
        self.shared = 2          # クラス変数と同名: クラス変数を正とし、インスタンス変数は作らない
        with open("f") as self.handle:
            pass

    def bump(self):
        self.count += 1
        self.late = True         # 2か所目以降ではなく、最初の代入の位置を使う
        self.late = False

    def run(self):
        self.callback()

    def callback(self):
        pass

    @classmethod
    def build(cls):
        cls.registry = {}        # クラスの属性。インスタンス変数にしない

    @staticmethod
    def helper(self):
        self.ignored = 1

    def outer(self):
        def inner():
            self.inner_only = 1  # ネストした関数の中は、その関数のもの
        return inner

    def go(self):
        self.callback = lambda: None   # メソッドと同名: メソッドを正とする


def numbers(n):
    for i in range(n):
        yield i


def delegating():
    yield from numbers(3)


def not_a_generator():
    def gen():
        yield 1
    return gen


async def agen():
    yield 1


def plain():
    return [1 for _ in range(3)]
'''


def symbols_of(analyzed, tmp_path: Path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(SOURCE, encoding="utf-8")
    repo, project, _ = analyzed(root)
    return repo, project, {s.qualified_name: s for s in repo.list_symbols_for_project(project.project_id)}


def test_instance_variables_are_symbols_at_their_first_assignment(analyzed, tmp_path: Path) -> None:
    _, _, symbols = symbols_of(analyzed, tmp_path)
    instance = {q.rsplit(".", 1)[1]: s for q, s in symbols.items() if s.kind == SymbolKind.INSTANCE_VARIABLE and q.startswith("mod.Base.")}
    assert set(instance) == {"name", "items", "count", "a", "b", "rest", "handle", "late"}  # tuple・注釈つき・with as・複合代入も含む
    assert instance["name"].start_line == 5 and instance["late"].start_line == 15  # 最初の代入の位置
    assert instance["name"].parent_symbol_id == symbols["mod.Base"].symbol_id
    for excluded in ("shared", "callback", "registry", "ignored", "inner_only"):
        assert not any(q == f"mod.Base.{excluded}" and s.kind == SymbolKind.INSTANCE_VARIABLE for q, s in symbols.items())
    assert symbols["mod.Base.shared"].kind == SymbolKind.CLASS_VARIABLE and symbols["mod.Base.callback"].kind == SymbolKind.METHOD  # 同名は、元の定義が正


def test_generators_are_flagged_without_counting_nested_functions(analyzed, tmp_path: Path) -> None:
    _, _, symbols = symbols_of(analyzed, tmp_path)
    flags = {name: symbols[f"mod.{name}"].is_generator for name in ("numbers", "delegating", "not_a_generator", "agen", "plain")}
    assert flags == {"numbers": True, "delegating": True, "not_a_generator": False, "agen": True, "plain": False}
    assert symbols["mod.agen"].is_async and symbols["mod.agen"].is_generator  # async ジェネレータは両方
    assert symbols["mod.not_a_generator.gen"].is_generator  # ネストした関数のyieldは、その関数のもの


def test_calling_an_instance_variable_is_explained_not_resolved(analyzed, tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "m.py").write_text("class C:\n    def __init__(self, fn):\n        self.fn = fn\n    def run(self):\n        return self.fn()\n", encoding="utf-8")
    repo, project, _ = analyzed(root)
    refs = [r for r in repo.list_references_for_project(project.project_id) if r.target_name == "self.fn"]
    assert refs and refs[0].resolution_status == ResolutionStatus.UNRESOLVED
    assert "インスタンス変数" in refs[0].note and "確定できない" in refs[0].note  # 変数を呼び出し先として確定しない


def test_cli_shows_kind_and_flags_and_persists_across_reload(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "mod.py").write_text(SOURCE, encoding="utf-8")
    db = str(tmp_path / "d.sqlite")
    assert main(["analyze", str(root), "--db", db]) == 0
    capsys.readouterr()
    assert main(["search", "numbers", "--db", db, "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    items = data["results"]
    assert any(i.get("is_generator") is True for i in items)  # 保存・再読み込み後も残る
    assert main(["search", "count", "--db", db, "--format", "json", "--kind", "instance_variable"]) == 0
    assert "instance_variable" in capsys.readouterr().out

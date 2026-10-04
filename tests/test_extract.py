from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from codeinsight.application import NavigationService
from codeinsight.application.extract_service import ExtractService
from codeinsight.cli import main
from codeinsight.presentation import extract_export

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def env(analyzed, tmp_path: Path):
    root = tmp_path / "proj"
    shutil.copytree(FIXTURES / "c_callgraph", root)
    repo, project, _ = analyzed(root)
    navigation = NavigationService(repo)
    index = navigation.load_index(project)
    return ExtractService(navigation), project, index, root


def _symbol(index, name):
    return next(s for s in index.symbols.values() if s.name == name and s.kind.value in ("function", "method"))


def test_extracts_callees_by_depth_with_resolution_and_evidence(env) -> None:
    service, project, index, _ = env
    result = service.extract(project, index, _symbol(index, "main"), "callees", 1)
    names = [i.symbol.name for i in result.items]
    assert "apply" in names and result.root.symbol.name == "main"
    apply_item = next(i for i in result.items if i.symbol.name == "apply")
    assert apply_item.via.startswith("main.c:") and apply_item.lines and apply_item.lines[0][0] == apply_item.symbol.start_line
    deeper = service.extract(project, index, _symbol(index, "main"), "callees", 3)
    assert len(deeper.items) >= len(result.items)


def test_callers_and_unresolved_calls_are_reported_not_followed(env) -> None:
    service, project, index, _ = env
    callers = service.extract(project, index, _symbol(index, "apply"), "callers", 1)
    assert [i.symbol.name for i in callers.items] == ["main"] and callers.items[0].role == "caller"
    result = service.extract(project, index, _symbol(index, "apply"), "callees", 1)
    assert any(c.status == "unresolved" for c in result.omitted_calls)  # 関数ポインタの呼び出しは、ソースを出さず、一覧にする
    assert not any(i.symbol.name == "op" for i in result.items)


def test_markdown_has_toc_notes_and_safe_fences(env) -> None:
    service, project, index, _ = env
    result = service.extract(project, index, _symbol(index, "main"), "callees", 1)
    markdown = extract_export.to_markdown(result)
    assert "## 目次" in markdown and "実行順序や実際に通る経路を示すものではありません" in markdown and "## たどれなかった呼び出し" in markdown
    # ソースの内容が、コードフェンスから抜け出せない
    result.root.lines = [(1, "int x; /* ```` */"), (2, "```")]
    fence_lines = [line for line in extract_export.to_markdown(result).splitlines() if line.startswith("`")]
    assert fence_lines[0].startswith(fence_lines[1]) and len(fence_lines[1]) >= 5


def test_stale_file_source_is_not_shown(env) -> None:
    service, project, index, root = env
    (root / "main.c").write_text("/* 追記 */\n" + (root / "main.c").read_text(encoding="utf-8"), encoding="utf-8")
    result = service.extract(project, index, _symbol(index, "main"), "callees", 1)
    assert result.root.lines == [] and result.root.omitted_reason.startswith("古い")  # 行がずれたソースを、解析結果として出さない
    assert "main.c" in result.stale_files and "古い" in extract_export.to_markdown(result)


def test_limits_truncate_and_json_schema(env) -> None:
    service, project, index, _ = env
    result = service.extract(project, index, _symbol(index, "main"), "callees", 3, max_items=1, max_lines=2)
    assert len(result.items) == 1 and result.truncated
    assert all(len(i.lines) <= 2 for i in [result.root, *result.items])
    data = json.loads(extract_export.to_json(result))
    assert data["schema"] == "codeinsight.extract/1" and data["root"]["symbol"] == "main" and data["truncated"] is True


def test_cli_extract_writes_markdown(tmp_path: Path, capsys) -> None:
    root = tmp_path / "proj"
    shutil.copytree(FIXTURES / "c_callgraph", root)
    db = str(tmp_path / "e.db")
    assert main(["analyze", str(root), "--db", db]) == 0
    capsys.readouterr()
    output = tmp_path / "out.md"
    assert main(["extract", "main", "--depth", "2", "--db", db, "--project", str(root), "-o", str(output)]) == 0
    text = output.read_text(encoding="utf-8")
    assert text.startswith("# 切り出し: main") and "```c" not in text.split("## 1.")[0]
    assert main(["extract", "main", "--format", "json", "--callees", "--callers", "--db", db, "--project", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["direction"] == "both"

"""切り出し（ExtractResult）の出力形式: Markdown・テキスト・JSON。

ソースは、解析対象（信頼できない入力）の内容なので、Markdownでは、内容に含まれるバッククォートの並びより長いコードフェンスで囲み、
フェンスから抜け出せないようにする。
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from codeinsight.application.extract_service import ExtractedItem, ExtractResult

NOTE = "静的解析で確認できた呼び出しの範囲から切り出したソースです。実行順序や実際に通る経路を示すものではありません。"
_FENCE_LANGUAGE = {".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".py": "python", ".pyi": "python", ".el": "elisp", ".org": "org"}
_ROLE = {"root": "起点", "callee": "呼び出し先", "caller": "呼び出し元"}


def _all(result: ExtractResult) -> list[ExtractedItem]:
    return [result.root, *result.items]


def _fence(lines: list[tuple[int, str]]) -> str:
    longest = max((len(m) for _, text in lines for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _code(item: ExtractedItem, numbered: bool) -> list[str]:
    if not item.lines:
        return []
    width = len(str(item.lines[-1][0]))
    body = [f"{n:>{width}} | {text}" if numbered else text for n, text in item.lines]
    fence = _fence(item.lines)
    language = _FENCE_LANGUAGE.get(PurePosixPath(item.path).suffix.lower(), "") if not numbered else "text"
    return [f"{fence}{language}", *body, fence]


def _status(item: ExtractedItem) -> str:
    if item.role == "root":
        return "起点"
    return "推定" if item.inferred else "確定"


def to_markdown(result: ExtractResult, numbered: bool = True) -> str:
    root = result.root.symbol
    direction = {"callees": "呼び出し先", "callers": "呼び出し元", "both": "呼び出し先と呼び出し元"}[result.direction]
    out = [f"# 切り出し: {root.qualified_name}", "", f"> {NOTE}", "",
           f"* 起点: `{result.root.path}:{root.start_line}-{root.end_line}`（{root.kind.value}）",
           f"* 範囲: {direction}、深さ {result.depth}（{len(result.items)}件{'・件数の上限で打ち切り' if result.truncated else ''}）"]
    if result.stale_files:
        out.append(f"* **古い**: 解析後に変更されたため、ソースを出していないファイル: {', '.join(f'`{p}`' for p in result.stale_files)}")
    out += ["", "## 目次", "", "| # | 関数 | 場所 | 関係 | 解決 |", "| --- | --- | --- | --- | --- |"]
    for number, item in enumerate(_all(result), 1):
        out.append(f"| {number} | `{item.symbol.qualified_name}` | `{item.path}:{item.symbol.start_line}-{item.symbol.end_line}` | {_ROLE[item.role]}（深さ {item.depth}） | {_status(item)} |")
    for number, item in enumerate(_all(result), 1):
        out += ["", f"## {number}. {item.symbol.qualified_name}", "",
                f"* 場所: `{item.path}:{item.symbol.start_line}-{item.symbol.end_line}`（{item.symbol.kind.value}）"]
        if item.via:
            out.append(f"* 呼び出しの位置: `{item.via}`（{'推定' if item.inferred else '確定'}）")
        if item.omitted_reason:
            out.append(f"* 注記: {item.omitted_reason}")
        code = _code(item, numbered)
        if code:
            out += ["", *code]
    if result.omitted_calls:
        out += ["", "## たどれなかった呼び出し", "", "未解決・曖昧・外部の呼び出しは、呼び出し先を確定できないため、ソースを出していません。", ""]
        for call in result.omitted_calls:
            note = f" — {call.note}" if call.note else ""
            out.append(f"* `{call.source}` → `{call.name}`（{call.status}、`{call.location}`）{note}")
    return "\n".join(out) + "\n"


def to_text(result: ExtractResult, numbered: bool = True) -> str:
    out = [f"切り出し: {result.root.symbol.qualified_name}  ({NOTE})"]
    for number, item in enumerate(_all(result), 1):
        out += ["", f"==== {number}. {item.symbol.qualified_name}  {item.path}:{item.symbol.start_line}-{item.symbol.end_line}  [{_ROLE[item.role]}・{_status(item)}]"]
        if item.omitted_reason:
            out.append(f"  ※ {item.omitted_reason}")
        width = len(str(item.lines[-1][0])) if item.lines else 1
        out += [f"{n:>{width}} | {text}" if numbered else text for n, text in item.lines]
    if result.omitted_calls:
        out += ["", "---- たどれなかった呼び出し（未解決・曖昧・外部）"]
        out += [f"  {c.source} -> {c.name}  [{c.status}] {c.location}" for c in result.omitted_calls]
    return "\n".join(out) + "\n"


def to_dict(result: ExtractResult) -> dict:
    def item(i: ExtractedItem) -> dict:
        return {
            "symbol": i.symbol.qualified_name, "kind": i.symbol.kind.value, "path": i.path, "start_line": i.symbol.start_line, "end_line": i.symbol.end_line,
            "role": i.role, "depth": i.depth, "resolution": _status(i), "via": i.via, "omitted_reason": i.omitted_reason,
            "lines": [{"n": n, "text": t} for n, t in i.lines],
        }

    return {
        "schema": "codeinsight.extract/1", "note": NOTE, "direction": result.direction, "depth": result.depth, "truncated": result.truncated,
        "stale_files": result.stale_files, "root": item(result.root), "items": [item(i) for i in result.items],
        "omitted_calls": [{"source": c.source, "name": c.name, "status": c.status, "location": c.location, "note": c.note} for c in result.omitted_calls],
    }


def to_json(result: ExtractResult) -> str:
    return json.dumps(to_dict(result), ensure_ascii=False, indent=2) + "\n"

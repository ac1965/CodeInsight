"""解析結果（読解カード・制御フロー）を、AIエージェントに返すJSONの形にする。

* シンボル・参照は、名前・場所・解決状態だけにする（循環や巨大なオブジェクトを渡さない）。
* 長いリストは、先頭だけを返し、残りの件数を示す。
* **ソースの本文に当たる項目**（宣言の行・ドキュメントの引用・呼び出し元の実引数・TODOコメント・戻り値の式・リスクや制御構造の式の断片）は、
  `allow_source` が許可されていない場合、件数・行番号だけにして、内容を返さない。
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any

from codeinsight.application.navigation_service import ReferenceHit
from codeinsight.domain import Reference, Symbol

MAX_ITEMS = 30
# 内容が、ソースの断片になる項目の名前（許可がなければ、内容を返さない）
SOURCE_FIELDS = frozenset({"detail", "declaration", "doc_mentions", "caller_arguments", "todo_comments", "text", "snippet", "origin", "call_text", "default", "annotation"})
_OMITTED = "（ソースの断片のため、--allow-source が無いと返しません）"


def symbol_brief(symbol: Symbol, path: str | None = None) -> dict:
    result: dict[str, Any] = {"qualified_name": symbol.qualified_name, "kind": symbol.kind.value, "start_line": symbol.start_line, "end_line": symbol.end_line}
    if path is not None:
        result["path"] = path
    return result


def to_plain(value: Any, allow_source: bool, depth: int = 0, key: str = "") -> Any:
    if depth > 8:
        return "…"
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Symbol):
        return symbol_brief(value)
    if isinstance(value, ReferenceHit):
        return {"source": value.source.qualified_name, "location": value.location, "resolution": value.reference.resolution_status.value, "confidence": value.reference.confidence.value}
    if isinstance(value, Reference):
        return {"target": value.target_name, "line": value.source_location.start_line, "resolution": value.resolution_status.value, "confidence": value.confidence.value}
    if isinstance(value, str | int | float | bool) or value is None:
        if isinstance(value, str) and key in SOURCE_FIELDS and not allow_source and value:
            return _OMITTED
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_plain(getattr(value, f.name), allow_source, depth + 1, f.name) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        if key in SOURCE_FIELDS and not allow_source:
            return {"count": len(value), "note": _OMITTED}
        items = list(value.items())[:MAX_ITEMS]
        plain = {str(k): to_plain(v, allow_source, depth + 1, str(k)) for k, v in items}
        if len(value) > MAX_ITEMS:
            plain["_omitted_count"] = len(value) - MAX_ITEMS
        return plain
    if isinstance(value, list | tuple | set | frozenset):
        sequence = sorted(value, key=str) if isinstance(value, set | frozenset) else list(value)
        if key in SOURCE_FIELDS and not allow_source:
            return {"count": len(sequence), "note": _OMITTED}
        if key == "returns" and not allow_source:  # (行, 式) の組は、行番号だけにする
            return [{"line": item[0]} for item in sequence[:MAX_ITEMS] if isinstance(item, tuple) and item]
        plain_list = [to_plain(v, allow_source, depth + 1, key) for v in sequence[:MAX_ITEMS]]
        if len(sequence) > MAX_ITEMS:
            plain_list.append({"_omitted_count": len(sequence) - MAX_ITEMS})
        return plain_list
    return str(value)

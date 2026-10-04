"""SCIP（Sourcegraphのコード索引の形式。Apache-2.0）の `index.scip` の最小限の読み取り。

protobufのバイナリを、標準ライブラリだけで読む（外部の依存を増やさない）。必要な項目（ツール情報・文書・出現箇所・
シンボル情報）だけを取り出し、他の項目は読み飛ばす。信頼できない入力として扱い、不正な入力は ScipError にする。
索引を生成する外部ツール（scip-python・scip-clang など）は、CodeInsightは実行しない。

スキーマ（scip.proto）: Index{metadata=1, documents=2}、Metadata{tool_info=2, project_root=3}、ToolInfo{name=1, version=2}、
Document{relative_path=1, occurrences=2, symbols=3, language=4}、Occurrence{range=1, symbol=2, symbol_roles=3}、
SymbolInformation{symbol=1, documentation=3, kind=5, display_name=6}。行・列は0始まり。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

MAX_BYTES = 1024 * 1024 * 1024
ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2


class ScipError(Exception):
    pass


@dataclass(frozen=True)
class ScipOccurrence:
    start_line: int  # 0始まり
    start_char: int
    end_line: int
    end_char: int
    symbol: str
    roles: int

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & ROLE_DEFINITION)


@dataclass(frozen=True)
class ScipSymbolInfo:
    symbol: str
    display_name: str
    kind: int
    documentation: str


@dataclass
class ScipDocument:
    relative_path: str
    language: str = ""
    occurrences: list[ScipOccurrence] = field(default_factory=list)
    symbols: list[ScipSymbolInfo] = field(default_factory=list)


@dataclass
class ScipMetadata:
    tool: str = ""
    tool_version: str = ""
    project_root: str = ""


# --- protobufのワイヤー形式 ---


def _varint(data: memoryview, position: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if position >= len(data):
            raise ScipError("索引が途中で終わっています（varint）")
        byte = data[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7
        if shift > 70:
            raise ScipError("不正なvarintです")


def _fields(data: memoryview) -> Iterator[tuple[int, int, int | memoryview]]:
    """(フィールド番号, ワイヤー形式, 値)。長さ付きは memoryview、varint・固定長は整数。"""

    position = 0
    while position < len(data):
        tag, position = _varint(data, position)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, position = _varint(data, position)
            yield number, wire, value
        elif wire == 2:
            length, position = _varint(data, position)
            if position + length > len(data):
                raise ScipError("索引が途中で終わっています（長さ付きの項目）")
            yield number, wire, data[position:position + length]
            position += length
        elif wire == 1:
            position += 8
        elif wire == 5:
            position += 4
        else:
            raise ScipError(f"対応していないワイヤー形式です: {wire}")
        if position > len(data):
            raise ScipError("索引が途中で終わっています")


def _text(value: int | memoryview) -> str:
    return bytes(value).decode("utf-8", errors="replace") if isinstance(value, memoryview) else ""


def _packed_ints(value: int | memoryview) -> list[int]:
    if isinstance(value, int):
        return [value]
    numbers, position = [], 0
    while position < len(value):
        number, position = _varint(value, position)
        numbers.append(number if number < 2**31 else number - 2**32)  # int32
    return numbers


def _occurrence(data: memoryview) -> ScipOccurrence | None:
    numbers: list[int] = []
    symbol, roles = "", 0
    for number, _, value in _fields(data):
        if number == 1:
            numbers.extend(_packed_ints(value))
        elif number == 2:
            symbol = _text(value)
        elif number == 3 and isinstance(value, int):
            roles = value
    if len(numbers) == 3:
        start_line, start_char, end_char = numbers
        return ScipOccurrence(start_line, start_char, start_line, end_char, symbol, roles)
    if len(numbers) == 4:
        return ScipOccurrence(numbers[0], numbers[1], numbers[2], numbers[3], symbol, roles)
    return None  # 範囲が不正な出現箇所は読み飛ばす


def _document(data: memoryview) -> ScipDocument:
    document = ScipDocument("")
    for number, _, value in _fields(data):
        if number == 1:
            document.relative_path = _text(value)
        elif number == 4:
            document.language = _text(value)
        elif number == 2 and isinstance(value, memoryview):
            occurrence = _occurrence(value)
            if occurrence is not None:
                document.occurrences.append(occurrence)
        elif number == 3 and isinstance(value, memoryview):
            symbol, display, kind, documentation = "", "", 0, ""
            for inner, _, item in _fields(value):
                if inner == 1:
                    symbol = _text(item)
                elif inner == 6:
                    display = _text(item)
                elif inner == 5 and isinstance(item, int):
                    kind = item
                elif inner == 3 and not documentation:
                    documentation = " ".join(_text(item).split())[:300]
            if symbol:
                document.symbols.append(ScipSymbolInfo(symbol, display[:200], kind, documentation))
    return document


def read_scip(data: bytes) -> tuple[ScipMetadata, Iterator[ScipDocument]]:
    """(メタデータ, 文書の列)。文書は1件ずつ読み取る（大きな索引でもまとめて展開しない）。"""

    if len(data) > MAX_BYTES:
        raise ScipError("索引が大きすぎます（1 GiB を超えています）")
    view = memoryview(data)
    metadata = ScipMetadata()
    documents: list[memoryview] = []
    try:
        for number, _, value in _fields(view):
            if number == 1 and isinstance(value, memoryview):
                for inner, _, item in _fields(value):
                    if inner == 3:
                        metadata.project_root = _text(item)[:500]
                    elif inner == 2 and isinstance(item, memoryview):
                        for tool_field, _, tool_value in _fields(item):
                            if tool_field == 1:
                                metadata.tool = _text(tool_value)[:80]
                            elif tool_field == 2:
                                metadata.tool_version = _text(tool_value)[:40]
            elif number == 2 and isinstance(value, memoryview):
                documents.append(value)
    except ScipError:
        raise
    except (IndexError, ValueError) as exc:  # 想定外の壊れ方も、取り込み失敗として扱う
        raise ScipError(f"索引を読み取れません: {exc}") from exc
    if not documents and metadata == ScipMetadata():
        raise ScipError("SCIPの索引ではないようです（メタデータも文書もありません）")

    def generate() -> Iterator[ScipDocument]:
        for chunk in documents:
            yield _document(chunk)

    return metadata, generate()


# --- シンボル名 ---

_NAME_CHARS = re.compile(r"[A-Za-z0-9_+\-$]+")


def symbol_name(symbol: str) -> str:
    """SCIPのシンボル文字列から、最後の記述子の名前（例: `pkg/mod.py/Class#method().` → method）。解釈できなければ空。

    形式: `<scheme> <manager> <package> <version> <descriptors>`（空白は二重の空白でエスケープ）。
    """

    if symbol.startswith("local "):
        return ""
    parts: list[str] = []
    current, index = "", 0
    while index < len(symbol) and len(parts) < 4:
        if symbol[index] == " ":
            if symbol[index + 1:index + 2] == " ":
                current += " "
                index += 2
                continue
            parts.append(current)
            current = ""
        else:
            current += symbol[index]
        index += 1
    if len(parts) < 4:
        return ""
    descriptors = symbol[index:]
    name, position = "", 0
    while position < len(descriptors):
        char = descriptors[position]
        if char == "`":
            end = position + 1
            buffer = ""
            while end < len(descriptors):
                if descriptors[end] == "`":
                    if descriptors[end + 1:end + 2] == "`":
                        buffer += "`"
                        end += 2
                        continue
                    break
                buffer += descriptors[end]
                end += 1
            name, position = buffer, end + 1
        elif char in "([":
            close = ")" if char == "(" else "]"
            end = descriptors.find(close, position)
            if end < 0:
                return name
            position = end + 1
            continue
        else:
            match = _NAME_CHARS.match(descriptors, position)
            if not match:
                position += 1
                continue
            name, position = match.group(0), match.end()
        # 名前の直後の接尾辞（/ # . : ! と、メソッドの (...)）
        if position < len(descriptors) and descriptors[position] == "(":
            end = descriptors.find(")", position)
            position = end + 1 if end >= 0 else len(descriptors)
        if position < len(descriptors) and descriptors[position] in "/#.:!":
            position += 1
    return name

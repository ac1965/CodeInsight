"""radare2（r2）が出力したJSONの最小限の読み取り。

radare2 は、利用者が実行し、出力（`aflj`・`agCj`・`ij`）をファイルへ保存したものを**読むだけ**で、CodeInsight は radare2 を実行しない
（1.1「4. 既存ソフトウェアの非侵襲的な解析」・3.12）。入力は信頼できない入力として扱い、型・件数・長さを検査する。
名前・シグネチャなど、バイナリ由来の文字列は、AIへの指示ではなくデータとして扱う（4.4）。

形式（radare2 6.x で確認）:
* `aflj`: `[{"name": "dbg.main", "addr": 4294968424, "size": 132, "file": "m.c", "signature": "...", "nbbs": 6, "indegree": 0, "outdegree": 2}, ...]`
  デバッグ情報（DWARF）があれば、名前は `dbg.<名前>`、`file` はソースファイル名。無ければ `sym.<名前>`・`fcn.<アドレス>`。外部関数は `sym.imp.<名前>`。
* `agCj`: `[{"name": "dbg.main", "size": 132, "imports": ["sym._helper", "sym.imp.printf"]}, ...]`（呼び出しの辺。呼び出しを持つ関数だけ）
* `ij`: `{"core": {"file": "..."}, "bin": {"arch": "arm", "os": "macos", "stripped": false, "lang": "c", ...}}`
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

MAX_BYTES = 256 * 1024 * 1024
MAX_FUNCTIONS = 500_000
MAX_EDGES = 5_000_000
MAX_TEXT = 1000

_PREFIXES = ("sym.imp.", "dbg.", "sym.", "fcn.", "method.", "loc.")


class R2Error(Exception):
    pass


@dataclass(frozen=True)
class R2Function:
    name: str
    addr: int
    size: int
    file: str  # デバッグ情報のソースファイル名（無ければ空）
    signature: str
    nbbs: int
    indegree: int
    outdegree: int

    @property
    def is_import(self) -> bool:
        return self.name.startswith("sym.imp.")


@dataclass(frozen=True)
class R2Info:
    path: str = ""
    arch: str = ""
    os: str = ""
    stripped: bool | None = None
    lang: str = ""


def _load(data: bytes, what: str) -> object:
    if len(data) > MAX_BYTES:
        raise R2Error(f"{what} が大きすぎます（{MAX_BYTES // (1024 * 1024)}MB まで）")
    try:
        return json.loads(data.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise R2Error(f"{what} をJSONとして読めません: {exc}") from exc


def _text(value: object) -> str:
    return value[:MAX_TEXT] if isinstance(value, str) else ""


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def read_functions(data: bytes) -> list[R2Function]:
    """`aflj` の出力。形式が違うもの（リストでない・名前が無い）は、読み飛ばさず、エラーにする。"""

    loaded = _load(data, "関数の一覧（aflj）")
    if not isinstance(loaded, list):
        raise R2Error("関数の一覧（aflj）は、JSONの配列である必要があります（`r2 -q -c 'aaa; aflj' <バイナリ>` の出力）")
    if len(loaded) > MAX_FUNCTIONS:
        raise R2Error(f"関数が多すぎます（{MAX_FUNCTIONS}件まで）")
    functions = []
    for item in loaded:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise R2Error("関数の一覧に、名前（name）のない項目があります。aflj の出力か確認してください")
        functions.append(R2Function(
            _text(item["name"]), _int(item.get("addr", item.get("offset"))), _int(item.get("size")), _text(item.get("file")),
            _text(item.get("signature")), _int(item.get("nbbs")), _int(item.get("indegree")), _int(item.get("outdegree")),
        ))
    return functions


def read_callgraph(data: bytes) -> list[tuple[str, str]]:
    """`agCj` の出力から、呼び出しの辺（呼び出し元の名前, 呼び出し先の名前）。重複は除く。"""

    loaded = _load(data, "呼び出しグラフ（agCj）")
    if not isinstance(loaded, list):
        raise R2Error("呼び出しグラフ（agCj）は、JSONの配列である必要があります（`r2 -q -c 'aaa; agCj' <バイナリ>` の出力）")
    edges: dict[tuple[str, str], None] = {}
    for item in loaded:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise R2Error("呼び出しグラフに、名前（name）のない項目があります。agCj の出力か確認してください")
        imports = item.get("imports", [])
        if not isinstance(imports, list):
            continue
        for callee in imports:
            if isinstance(callee, str) and callee:
                edges[(_text(item["name"]), _text(callee))] = None
        if len(edges) > MAX_EDGES:
            raise R2Error(f"呼び出しの辺が多すぎます（{MAX_EDGES}件まで）")
    return list(edges)


def read_info(data: bytes) -> R2Info:
    """`ij` の出力（無くてもよい）。"""

    loaded = _load(data, "バイナリの情報（ij）")
    if not isinstance(loaded, dict):
        raise R2Error("バイナリの情報（ij）は、JSONのオブジェクトである必要があります")
    core, binary = loaded.get("core", {}), loaded.get("bin", {})
    core = core if isinstance(core, dict) else {}
    binary = binary if isinstance(binary, dict) else {}
    stripped = binary.get("stripped")
    return R2Info(_text(core.get("file")), _text(binary.get("arch")), _text(binary.get("os")), stripped if isinstance(stripped, bool) else None, _text(binary.get("lang")))


def candidate_names(name: str) -> list[str]:
    """radare2 の関数名から、ソース上の名前の候補。

    接頭辞（`dbg.`・`sym.`・`sym.imp.`・`fcn.`・`method.`）と、引数リスト（C++ の demangle 名）を除く。
    Mach-O は、C の関数名の先頭に `_` を付けるため、先頭の `_` を除いたものも候補にする（元の名前を先に）。
    """

    base = name
    for prefix in _PREFIXES:
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    base = re.sub(r"\(.*$", "", base).strip()
    result = [base] if base else []
    if base.startswith("_") and len(base) > 1:
        result.append(base[1:])
    return result

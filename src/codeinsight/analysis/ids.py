from __future__ import annotations

import hashlib
from collections import Counter

from codeinsight.domain import Symbol, SymbolKind


def _digest(*parts: object) -> str:
    joined = "\x00".join(str(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


class IdAllocator:
    """1ファイル分の解析で決定的なIDを払い出す。

    同じファイル・種類・修飾名の組が複数ある場合（Pythonの再定義など）は、
    出現順の連番で区別する。行番号はIDに含めないため、行がずれるだけの編集で
    他ファイルからの参照が切れない。
    """

    def __init__(self, file_id: str) -> None:
        self._file_id = file_id
        self._counts: Counter[tuple] = Counter()

    def _next(self, *key: object) -> int:
        ordinal = self._counts[key]
        self._counts[key] += 1
        return ordinal

    def symbol_id(self, kind: SymbolKind, qualified_name: str) -> str:
        ordinal = self._next("symbol", kind, qualified_name)
        return _digest("symbol", self._file_id, kind.value, qualified_name, ordinal)

    def reference_id(
        self, source_symbol_id: str, kind: str, target_key: str | None, start_line: int
    ) -> str:
        ordinal = self._next("reference", source_symbol_id, kind, target_key, start_line)
        return _digest(
            "reference", self._file_id, source_symbol_id, kind, target_key, start_line, ordinal
        )

    def dependency_id(self, kind: str, target_name: str, start_line: int) -> str:
        ordinal = self._next("dependency", kind, target_name, start_line)
        return _digest("dependency", self._file_id, kind, target_name, start_line, ordinal)


def build_symbol(
    ids: IdAllocator,
    file_id: str,
    name: str,
    qualified_name: str,
    kind: SymbolKind,
    start_line: int,
    end_line: int,
    parent: Symbol | None = None,
    **extra: object,
) -> Symbol:
    """決定的なIDを持つSymbolを生成する（解析器間で生成方法を統一する）。"""

    return Symbol(
        symbol_id=ids.symbol_id(kind, qualified_name),
        file_id=file_id,
        name=name,
        qualified_name=qualified_name,
        kind=kind,
        start_line=start_line,
        end_line=end_line,
        parent_symbol_id=parent.symbol_id if parent else None,
        **extra,  # type: ignore[arg-type]
    )

# Cの関数の照合キー（USR）の末尾に付ける印: コンパイラが、システムヘッダーに宣言を見つけた関数（プロジェクトの外で定義される）
KEY_SYSTEM_HEADER = "|system-header"
# C++の仮想関数の呼び出しの照合キー（USR）の末尾に付ける印: 実際の呼び出し先は、派生クラスのオーバーライドになりうる
KEY_VIRTUAL = "|virtual"
# C++の関数テンプレートの特殊化への呼び出しの照合キー: `cpptemplate:<修飾名>`。特殊化のUSRはテンプレート自身のUSRと異なるため、修飾名の一致で解決する（推定）
KEY_TEMPLATE = "cpptemplate:"

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class SymbolKind(enum.Enum):
    """静的解析で確認できたシンボルの種類。

    C言語・Python共通の種類に加え、言語固有の種類を含む。
    ここに含まれる種類は、いずれも構文解析によって確認できた「定義」を表す
    （Phase1では呼び出し・参照・依存関係は扱わない）。
    """

    MODULE = "module"
    FUNCTION = "function"
    FUNCTION_DECLARATION = "function_declaration"  # C: プロトタイプ宣言
    CLASS = "class"  # Python
    METHOD = "method"  # Python
    STRUCT = "struct"  # C
    UNION = "union"  # C
    ENUM = "enum"  # C
    TYPEDEF = "typedef"  # C
    MACRO = "macro"  # C
    GLOBAL_VARIABLE = "global_variable"
    STATIC_VARIABLE = "static_variable"  # C
    LOCAL_VARIABLE = "local_variable"  # C
    CLASS_VARIABLE = "class_variable"  # Python


@dataclass
class Symbol:
    """静的解析で確認できたシンボルの定義箇所を表すドメインモデル。"""

    symbol_id: str
    file_id: str
    name: str
    qualified_name: str
    kind: SymbolKind
    start_line: int
    end_line: int
    parent_symbol_id: str | None = None
    decorators: tuple[str, ...] = field(default_factory=tuple)
    base_classes: tuple[str, ...] = field(default_factory=tuple)
    is_async: bool = False

from __future__ import annotations

from dataclasses import dataclass

from codeinsight.application.paths import is_test_path
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import ReferenceKind, ResolutionStatus, Symbol, SymbolKind

_REFERENCE_KINDS = (
    ReferenceKind.CALL, ReferenceKind.NAME_REF, ReferenceKind.FUNCTION_REF, ReferenceKind.TYPE_USE,
    ReferenceKind.INHERITANCE, ReferenceKind.IMPORT, ReferenceKind.VARIABLE_REF,
)
_CHECKED = (SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS, SymbolKind.FUNCTION_DECLARATION)
_TRANSPARENT = {"staticmethod", "classmethod", "property", "abstractmethod", "dataclass"}


@dataclass(frozen=True)
class UnusedCandidate:
    symbol: Symbol
    path: str
    confidence: str  # high / medium / low
    reason: str


class UnusedService:
    """どこからも参照されていないシンボルを、候補として挙げる（Python・C）。

    動的な呼び出し・リフレクション・外部からの利用（公開API）・フレームワークによる登録は
    静的には見えないため、あくまで「候補」。確度が低いものは理由を付けて区別する。
    """

    def candidates(self, index: ProjectIndex, entry_symbol_ids: set[str] | None = None) -> list[UnusedCandidate]:
        used: set[str] = set()
        by_target_tests: dict[str, bool] = {}
        for reference in index.references:
            if reference.reference_kind not in _REFERENCE_KINDS or not reference.target_symbol_id:
                continue
            if reference.resolution_status != ResolutionStatus.RESOLVED or reference.source_symbol_id == reference.target_symbol_id:
                continue
            source = index.symbols.get(reference.source_symbol_id)
            if source is None:
                continue
            target = reference.target_symbol_id
            from_test = is_test_path(index.path_of(source.file_id))
            # 自身の内部（メソッドからクラス自身など）からの参照は、利用とは見なさない
            if _inside(index, source, index.symbols.get(target)):
                continue
            used.add(target)
            by_target_tests[target] = by_target_tests.get(target, True) and from_test
        unresolved_names = {
            r.target_name.rsplit(".", 1)[-1]
            for r in index.references
            if r.resolution_status in (ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS)
        }
        # 基底クラスで定義されているメソッド名（オーバーライドは多態的に使われる）
        base_methods = {
            s.name for s in index.symbols.values() if s.kind == SymbolKind.METHOD and _class_has_bases(index, s)
        }
        entries = entry_symbol_ids or set()
        found: list[UnusedCandidate] = []
        for symbol in index.symbols.values():
            if symbol.kind not in _CHECKED:
                continue
            path = index.path_of(symbol.file_id)
            if is_test_path(path) or symbol.symbol_id in entries:
                continue
            if symbol.name.startswith("__") and symbol.name.endswith("__"):
                continue
            if symbol.name == "main" and symbol.kind == SymbolKind.FUNCTION:
                continue
            if symbol.kind == SymbolKind.FUNCTION_DECLARATION:
                continue  # 宣言は、定義側の利用で判定する
            if symbol.symbol_id in used and not by_target_tests.get(symbol.symbol_id, False):
                continue
            if symbol.symbol_id in used:
                found.append(UnusedCandidate(symbol, path, "low", "テストからのみ使用されている"))
                continue
            decorators = [d for d in symbol.decorators if d.split("(", 1)[0].strip() not in _TRANSPARENT]
            private = symbol.name.startswith("_")
            if decorators:
                found.append(UnusedCandidate(symbol, path, "low", f"デコレータ付き（{', '.join(decorators)}）。フレームワークに登録されている可能性"))
            elif symbol.kind == SymbolKind.METHOD and symbol.name in base_methods and _class_has_bases(index, symbol):
                found.append(UnusedCandidate(symbol, path, "low", "継承関係のあるクラスのメソッド。多態的に呼ばれている可能性"))
            elif symbol.name in unresolved_names:
                found.append(UnusedCandidate(symbol, path, "low", "同名の呼び出しが未解決のまま存在する（動的に呼ばれている可能性）"))
            elif private:
                found.append(UnusedCandidate(symbol, path, "high", "非公開の名前で、どこからも参照されていない"))
            else:
                found.append(UnusedCandidate(symbol, path, "medium", "公開の名前で参照がない（外部から利用される公開APIの可能性）"))
        order = {"high": 0, "medium": 1, "low": 2}
        return sorted(found, key=lambda c: (order[c.confidence], c.path, c.symbol.start_line))


def _inside(index: ProjectIndex, source: Symbol, target: Symbol | None) -> bool:
    """source が target の内側（target自身、またはその子孫）にあるか。"""

    current: Symbol | None = source
    while current is not None:
        if target is not None and current.symbol_id == target.symbol_id:
            return True
        current = index.symbols.get(current.parent_symbol_id) if current.parent_symbol_id else None
    return False


def _class_has_bases(index: ProjectIndex, method: Symbol) -> bool:
    parent = index.symbols.get(method.parent_symbol_id) if method.parent_symbol_id else None
    return parent is not None and parent.kind == SymbolKind.CLASS and bool(parent.base_classes)

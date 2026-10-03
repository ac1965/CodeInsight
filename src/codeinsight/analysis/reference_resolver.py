from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from codeinsight.analysis.python_analyzer import (
    KEY_DIRECT,
    KEY_EXPORT,
    KEY_IMPORT,
    KEY_STAR,
    KEY_SELF,
    KEY_SUPER,
    KEY_TYPED,
)
from codeinsight.domain import (
    Confidence,
    Dependency,
    DependencyKind,
    Language,
    Reference,
    ReferenceKind,
    ResolutionStatus,
    SourceFile,
    Symbol,
    SymbolKind,
)

# 呼び出し先を置き換えない（呼び出しの意味を変えない）と見なすデコレータ。
_TRANSPARENT_DECORATORS = frozenset(
    {"staticmethod", "classmethod", "property", "abstractmethod", "abc.abstractmethod",
     "dataclass", "dataclasses.dataclass"}
)
_MAX_REEXPORT_DEPTH = 6
# 小文字で始まるがクラスである、よく使われる標準ライブラリの名前（それ以外の小文字名は関数と見なす）。
_LOWERCASE_CLASSES = frozenset(
    {"datetime", "date", "time", "timedelta", "timezone", "tzinfo", "defaultdict", "deque", "partial",
     "partialmethod", "socket", "array", "property", "staticmethod", "classmethod", "count", "cycle", "chain",
     "ordereddict", "namedtuple"}
)


@dataclass(frozen=True)
class _Lookup:
    """名前解決の結果。targetがNoneなら、statusとnoteが解決できなかった理由を表す。"""

    status: ResolutionStatus
    target: Symbol | None = None
    note: str = ""
    confidence: Confidence = Confidence.CONFIRMED
_INHERITANCE_NOTE = "サブクラスでのオーバーライドにより、実際の呼び出し先が異なる可能性がある"


class ReferenceResolver:
    """プロジェクト全体のシンボル表を用いて、参照・依存関係の参照先を解決する。

    解析器が出力した照合キー（C: USR、Python: 修飾名候補）を、決定的な規則だけで
    シンボル/ファイルに結び付ける。一意に決まらない場合は AMBIGUOUS、静的に確定
    できない場合は UNRESOLVED のままとし、推測で確定しない（AGENTS.md 10.2）。
    AIは関与しない。

    解決結果は引数のオブジェクトを直接更新する。何度実行しても同じ結果になる。
    """

    def resolve(
        self,
        files: Sequence[SourceFile],
        symbols: Sequence[Symbol],
        references: Sequence[Reference],
        dependencies: Sequence[Dependency],
    ) -> None:
        self._files_by_id = {f.file_id: f for f in files}
        self._files_by_path = {f.relative_path: f for f in files}
        self._symbols_by_id = {s.symbol_id: s for s in symbols}

        self._by_usr: dict[str, list[Symbol]] = defaultdict(list)
        self._py_by_qname: dict[str, list[Symbol]] = defaultdict(list)
        for symbol in symbols:
            language = self._language_of(symbol.file_id)
            if language == Language.C and symbol.usr:
                self._by_usr[symbol.usr].append(symbol)
            elif language == Language.PYTHON:
                self._py_by_qname[symbol.qualified_name].append(symbol)
        self._py_suffix_index: dict[str, list[Symbol]] | None = None
        self._py_modules = {
            qn: [s for s in group if s.kind == SymbolKind.MODULE]
            for qn, group in self._py_by_qname.items()
        }
        # モジュール名の末尾一致（`a.b.c` に対する `b.c`、`c`）を引くための索引
        self._py_module_suffixes: dict[str, list[Symbol]] = defaultdict(list)
        for qn, group in self._py_modules.items():
            position = qn.find(".")
            while position != -1:
                self._py_module_suffixes[qn[position + 1:]].extend(group)
                position = qn.find(".", position + 1)
        self._py_name_components = {part for qn in self._py_by_qname for part in qn.split(".")}
        self._bases: dict[str, list[Symbol]] = defaultdict(list)
        self._unresolved_bases: set[str] = set()
        # モジュールが公開するimport名（再エクスポート・star importの解決用）
        self._exports: dict[str, dict[str, str]] = defaultdict(dict)
        for reference in references:
            key = reference.target_key or ""
            source = self._symbols_by_id.get(reference.source_symbol_id)
            if key.startswith(KEY_EXPORT) and source is not None:
                exposed, _, dotted = key[len(KEY_EXPORT):].partition("=")
                self._exports[source.qualified_name][exposed] = dotted

        for dependency in dependencies:
            self._resolve_dependency(dependency)

        # MRO探索の前提として、継承関係を先に解決する。
        ordered = sorted(
            references, key=lambda r: r.reference_kind != ReferenceKind.INHERITANCE
        )
        for reference in ordered:
            self._resolve_reference(reference)
            if reference.reference_kind == ReferenceKind.INHERITANCE:
                if reference.target_symbol_id:
                    self._bases[reference.source_symbol_id].append(
                        self._symbols_by_id[reference.target_symbol_id]
                    )
                else:
                    self._unresolved_bases.add(reference.source_symbol_id)

    # --- 共通 ---

    def _language_of(self, file_id: str) -> Language:
        source_file = self._files_by_id.get(file_id)
        return source_file.language if source_file else Language.UNKNOWN

    @staticmethod
    def _set(
        item: Reference | Dependency,
        status: ResolutionStatus,
        note: str = "",
        confidence: Confidence = Confidence.CONFIRMED,
    ) -> None:
        item.resolution_status = status
        item.note = note
        item.confidence = confidence

    # --- 依存関係 ---

    def _resolve_dependency(self, dependency: Dependency) -> None:
        dependency.target_file_id = None
        language = self._language_of(dependency.source_file_id)
        if dependency.resolution_status == ResolutionStatus.EXTERNAL and (
            dependency.dependency_kind == DependencyKind.INCLUDE
        ):
            return
        if dependency.target_key is None:
            if dependency.resolution_status == ResolutionStatus.EXTERNAL:
                return
            dependency.resolution_status = ResolutionStatus.UNRESOLVED
            return
        if language == Language.C:
            target = self._files_by_path.get(dependency.target_key)
            if target is None:
                self._set(
                    dependency,
                    ResolutionStatus.UNRESOLVED,
                    "解析対象ファイルに含まれていない（除外または未解析）",
                )
            else:
                dependency.target_file_id = target.file_id
                self._set(dependency, ResolutionStatus.RESOLVED)
        elif language == Language.PYTHON:
            self._resolve_python_dependency(dependency)

    def _resolve_python_dependency(self, dependency: Dependency) -> None:
        name = dependency.target_key or ""
        keep_inferred = dependency.confidence == Confidence.INFERRED
        base_note = dependency.note if keep_inferred else ""
        modules = self._modules_named(name)
        exact = bool(self._py_modules.get(name))
        if len(modules) == 1:
            dependency.target_file_id = modules[0].file_id
            confidence = (
                Confidence.INFERRED if keep_inferred or not exact else Confidence.CONFIRMED
            )
            note = base_note or ("" if exact else "ソースルートが不明なため、末尾一致で解決した")
            self._set(dependency, ResolutionStatus.RESOLVED, note, confidence)
        elif len(modules) > 1:
            self._set(
                dependency,
                ResolutionStatus.AMBIGUOUS,
                "同名のモジュールが複数あり、一意に決まらない",
            )
        elif name.split(".", 1)[0] in self._py_name_components:
            self._set(
                dependency,
                ResolutionStatus.UNRESOLVED,
                "プロジェクト内にモジュールが見つからない",
            )
        else:
            self._set(
                dependency,
                ResolutionStatus.EXTERNAL,
                "プロジェクト外（標準/外部ライブラリ）と考えられる",
            )

    def _modules_named(self, name: str) -> list[Symbol]:
        exact = self._py_modules.get(name)
        if exact:
            return exact
        return list(self._py_module_suffixes.get(name, []))

    # --- 参照 ---

    def _resolve_reference(self, reference: Reference) -> None:
        reference.target_symbol_id = None
        source = self._symbols_by_id.get(reference.source_symbol_id)
        if source is None:
            return
        if reference.target_key is None:
            if reference.resolution_status != ResolutionStatus.EXTERNAL:
                reference.resolution_status = ResolutionStatus.UNRESOLVED
            return
        language = self._language_of(source.file_id)
        if language == Language.C:
            self._resolve_c(reference)
        elif language == Language.PYTHON:
            self._resolve_python(reference)

    def _resolve_c(self, reference: Reference) -> None:
        candidates = self._by_usr.get(reference.target_key or "", [])
        kind = reference.reference_kind
        if kind in (ReferenceKind.CALL, ReferenceKind.FUNCTION_REF):
            definitions = [s for s in candidates if s.kind == SymbolKind.FUNCTION]
            declarations = [s for s in candidates if s.kind == SymbolKind.FUNCTION_DECLARATION]
            if len(definitions) == 1:
                self._resolved(reference, definitions[0])
            elif len(definitions) > 1:
                self._set(
                    reference,
                    ResolutionStatus.AMBIGUOUS,
                    "同一シンボルの定義が複数のファイルにある",
                )
            elif declarations:
                self._resolved(
                    reference, declarations[0], "プロジェクト内に定義がなく、宣言のみ確認できた"
                )
            else:
                self._external(reference)
            return
        if kind == ReferenceKind.VARIABLE_REF:
            matches = [
                s
                for s in candidates
                if s.kind in (SymbolKind.GLOBAL_VARIABLE, SymbolKind.STATIC_VARIABLE)
            ]
        else:
            matches = [
                s
                for s in candidates
                if s.kind
                in (SymbolKind.STRUCT, SymbolKind.UNION, SymbolKind.ENUM, SymbolKind.TYPEDEF)
            ]
        if len(matches) == 1:
            self._resolved(reference, matches[0])
        elif len(matches) > 1:
            self._set(
                reference,
                ResolutionStatus.AMBIGUOUS,
                "宣言と定義が複数あるなど、一意に決まらない",
            )
        else:
            self._external(reference)

    def _external(self, reference: Reference) -> None:
        self._set(
            reference,
            ResolutionStatus.EXTERNAL,
            "プロジェクト内に定義がない（システムヘッダー/外部ライブラリ等）",
        )

    def _resolved(
        self,
        reference: Reference,
        target: Symbol,
        note: str = "",
        confidence: Confidence = Confidence.CONFIRMED,
    ) -> None:
        reference.target_symbol_id = target.symbol_id
        decorators = [
            d for d in target.decorators if d.split("(", 1)[0].strip() not in _TRANSPARENT_DECORATORS
        ]
        if decorators and reference.reference_kind != ReferenceKind.INHERITANCE:
            confidence = Confidence.INFERRED
            note = note or f"デコレータ({', '.join(decorators)})により置き換えられている可能性がある"
        self._set(reference, ResolutionStatus.RESOLVED, note, confidence)

    # --- Python ---

    def _resolve_python(self, reference: Reference) -> None:
        key = reference.target_key or ""
        if key.startswith(KEY_EXPORT):
            _, _, dotted = key[len(KEY_EXPORT):].partition("=")
            self._resolve_python_name(reference, dotted, imported=True)
        elif key.startswith(KEY_STAR):
            self._resolve_python_star(reference, key[len(KEY_STAR):])
        elif key.startswith(KEY_TYPED):
            self._resolve_python_typed(reference, key[len(KEY_TYPED):])
        elif key.startswith(KEY_SELF) or key.startswith(KEY_SUPER):
            self._resolve_python_method(reference, key)
        elif key.startswith(KEY_DIRECT):
            self._resolve_python_name(reference, key[len(KEY_DIRECT):], imported=False)
        elif key.startswith(KEY_IMPORT):
            self._resolve_python_name(reference, key[len(KEY_IMPORT):], imported=True)

    def _python_lookup(self, dotted: str) -> tuple[list[Symbol], bool]:
        """修飾名でシンボルを探す。戻り値は (候補, 末尾一致で見つけたか)。"""

        exact = self._py_by_qname.get(dotted)
        if exact:
            return exact, False
        if self._py_suffix_index is None:
            index: dict[str, list[Symbol]] = defaultdict(list)
            for qn, group in self._py_by_qname.items():
                position = qn.find(".")
                while position != -1:
                    index[qn[position + 1:]].extend(group)
                    position = qn.find(".", position + 1)
            self._py_suffix_index = index
        return self._py_suffix_index.get(dotted, []), True

    def _resolve_python_name(
        self, reference: Reference, dotted: str, imported: bool, depth: int = 0
    ) -> None:
        outcome = self._lookup_name(dotted, imported, depth)
        if outcome.target is not None:
            self._resolved(reference, outcome.target, outcome.note, outcome.confidence)
        else:
            self._set(reference, outcome.status, outcome.note)

    def _lookup_name(self, dotted: str, imported: bool, depth: int = 0) -> "_Lookup":
        """修飾名をプロジェクト内のシンボルに解決する（再エクスポートもたどる）。"""

        candidates, by_suffix = self._python_lookup(dotted)
        if len(candidates) == 1:
            if by_suffix:
                return _Lookup(
                    ResolutionStatus.RESOLVED,
                    candidates[0],
                    "ソースルートが不明なため、末尾一致で解決した",
                    Confidence.INFERRED,
                )
            return _Lookup(ResolutionStatus.RESOLVED, candidates[0])
        if len(candidates) > 1:
            return _Lookup(ResolutionStatus.AMBIGUOUS, None, "同名の候補が複数あり、一意に決まらない")

        # 最長の接頭辞が解決できれば、その先が見つからない理由を示す。
        parts = dotted.split(".")
        for length in range(len(parts) - 1, 0, -1):
            prefix_candidates, _ = self._python_lookup(".".join(parts[:length]))
            if len(prefix_candidates) != 1:
                continue
            prefix = prefix_candidates[0]
            rest = ".".join(parts[length:])
            if prefix.kind == SymbolKind.MODULE and depth < _MAX_REEXPORT_DEPTH:
                # `__init__.py` などが `from .x import Name` で再エクスポートしている名前をたどる。
                first, _, tail = rest.partition(".")
                exported = self._exports.get(prefix.qualified_name, {}).get(first)
                if exported is not None:
                    followed = exported + ("." + tail if tail else "")
                    if followed != dotted:
                        outcome = self._lookup_name(followed, True, depth + 1)
                        if outcome.target is not None and not outcome.note:
                            return _Lookup(
                                outcome.status, outcome.target, "再エクスポート経由で解決した",
                                outcome.confidence,
                            )
                        return outcome
            if prefix.kind in (SymbolKind.GLOBAL_VARIABLE, SymbolKind.CLASS_VARIABLE):
                note = f"変数 {prefix.qualified_name} 経由であり、型を静的に確定できない"
            elif prefix.kind == SymbolKind.MODULE and imported:
                note = (
                    f"{prefix.qualified_name} に {rest} の定義が見つからない"
                    "（動的な再エクスポートの可能性）"
                )
            else:
                note = f"{prefix.qualified_name} に {rest} の定義が見つからない"
            return _Lookup(ResolutionStatus.UNRESOLVED, None, note)

        if imported and parts[0] not in self._py_name_components:
            return _Lookup(
                ResolutionStatus.EXTERNAL,
                None,
                "プロジェクト外（標準/外部ライブラリ）と考えられる",
            )
        return _Lookup(ResolutionStatus.UNRESOLVED, None, "プロジェクト内に定義が見つからない")

    def _resolve_python_method(self, reference: Reference, key: str) -> None:
        is_super = key.startswith(KEY_SUPER)
        body = key[len(KEY_SUPER if is_super else KEY_SELF):]
        class_qn, _, attribute = body.rpartition(":")
        classes = [s for s in self._py_by_qname.get(class_qn, []) if s.kind == SymbolKind.CLASS]
        if len(classes) != 1:
            self._set(reference, ResolutionStatus.UNRESOLVED, "呼び出し元のクラスを特定できない")
            return
        order = self._linearize(classes[0])
        if is_super:
            order = order[1:]
        for cls in order:
            found = [
                s
                for s in self._py_by_qname.get(f"{cls.qualified_name}.{attribute}", [])
                if s.kind == SymbolKind.METHOD
            ]
            if len(found) == 1:
                self._resolved(reference, found[0], _INHERITANCE_NOTE, Confidence.INFERRED)
                return
            if len(found) > 1:
                self._set(reference, ResolutionStatus.AMBIGUOUS, "同名のメソッドが複数定義されている")
                return
        unresolved_base = any(c.symbol_id in self._unresolved_bases for c in order)
        if unresolved_base or (is_super and not order):
            note = "継承元にプロジェクト外または未解決のクラスがあり、定義を確定できない"
        else:
            note = f"{class_qn} と継承元に {attribute} のメソッド定義が見つからない（インスタンス属性の可能性）"
        self._set(reference, ResolutionStatus.UNRESOLVED, note)

    def _resolve_python_star(self, reference: Reference, body: str) -> None:
        """`from M import *` で取り込まれた名前を、M の定義とimportから探して解決する。"""

        modules_text, _, dotted = body.partition("|")
        head, _, tail = dotted.partition(".")
        tail_part = "." + tail if tail else ""
        for module_name in modules_text.split(";"):
            modules = self._modules_named(module_name)
            if len(modules) != 1:
                continue
            module_qn = modules[0].qualified_name
            if self._py_by_qname.get(f"{module_qn}.{head}"):
                self._resolve_python_name(reference, f"{module_qn}.{dotted}", imported=False)
                return
            exported = self._exports.get(module_qn, {}).get(head)
            if exported is not None:
                self._resolve_python_name(reference, exported + tail_part, imported=True, depth=1)
                if reference.resolution_status == ResolutionStatus.RESOLVED and not reference.note:
                    reference.note = "star importで取り込まれた名前"
                return
        self._set(
            reference,
            ResolutionStatus.UNRESOLVED,
            "名前を静的に解決できない（star import元に定義が見つからない）",
        )

    def _resolve_python_typed(self, reference: Reference, body: str) -> None:
        """型注釈/単一代入で推定した型のメソッド呼び出しを解決する（常にINFERRED）。"""

        type_key, _, attribute = body.rpartition("|")
        imported = type_key.startswith(KEY_IMPORT)
        dotted = type_key[len(KEY_IMPORT if imported else KEY_DIRECT):]
        outcome = self._lookup_name(dotted, imported)
        target = outcome.target
        if target is not None and target.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD):
            self._set(
                reference,
                ResolutionStatus.UNRESOLVED,
                f"{target.qualified_name} は関数であり、戻り値の型を静的に確定できない",
            )
            return
        if target is None or target.kind != SymbolKind.CLASS:
            last = dotted.rsplit(".", 1)[-1]
            if outcome.status == ResolutionStatus.EXTERNAL and last[:1].islower() and last not in _LOWERCASE_CLASSES:
                # `json.loads(...)` のように小文字で始まる外部名は関数の可能性が高く、戻り値の型を決められない。
                self._set(
                    reference,
                    ResolutionStatus.UNRESOLVED,
                    f"{dotted} は関数の可能性があり、戻り値の型を静的に確定できない",
                )
            elif outcome.status == ResolutionStatus.EXTERNAL:
                self._set(
                    reference,
                    ResolutionStatus.EXTERNAL,
                    f"推定した型 {dotted} はプロジェクト外（標準/外部ライブラリ）と考えられる",
                )
            else:
                self._set(
                    reference,
                    ResolutionStatus.UNRESOLVED,
                    f"推定した型 {dotted} をプロジェクト内のクラスに特定できない",
                )
            return
        note = "型注釈または単一代入から推定した型に基づく（実際の型はサブクラスの可能性がある）"
        for cls in self._linearize(target):
            found = [
                s
                for s in self._py_by_qname.get(f"{cls.qualified_name}.{attribute}", [])
                if s.kind == SymbolKind.METHOD
            ]
            if len(found) == 1:
                self._resolved(reference, found[0], note, Confidence.INFERRED)
                return
            if len(found) > 1:
                self._set(reference, ResolutionStatus.AMBIGUOUS, "同名のメソッドが複数定義されている")
                return
        self._set(
            reference,
            ResolutionStatus.UNRESOLVED,
            f"推定した型 {target.qualified_name} と継承元に {attribute} のメソッド定義が見つからない",
        )

    def _linearize(self, cls: Symbol) -> list[Symbol]:
        """クラスと基底クラスを幅優先で並べる（厳密なMRO(C3)ではない近似）。"""

        order: list[Symbol] = []
        seen: set[str] = set()
        queue = [cls]
        while queue:
            current = queue.pop(0)
            if current.symbol_id in seen:
                continue
            seen.add(current.symbol_id)
            order.append(current)
            queue.extend(self._bases.get(current.symbol_id, []))
        return order

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from codeinsight.analysis.call_graph import strongly_connected_components
from codeinsight.application.external_service import ExternalReport
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import ReferenceKind, ResolutionStatus, SymbolKind

# 役割の手がかり（コンポーネントの名前から。名前による推定であり、確定ではない）。
# 数字は、上位（利用者に近い側）から下位（データ・外部に近い側）への一般的な層の順。
# domain・persistence・infrastructure の間の依存の向きは、アーキテクチャの流儀（階層型/ヘキサゴナル等）で
# 逆になるため順序を付けない。確実に言えるのは「下位が利用者側（presentation/application）に依存しない」こと。
ROLE_ORDER = {"presentation": 0, "application": 1, "domain": 2, "persistence": 2, "infrastructure": 2}
ROLE_LABELS = {
    "presentation": "プレゼンテーション/API層",
    "application": "アプリケーション/サービス層",
    "domain": "ドメイン/ビジネスロジック層",
    "persistence": "リポジトリ/永続化層",
    "infrastructure": "インフラ/外部連携層",
    "shared": "共通/ユーティリティ",
    "test": "テスト",
    "script": "スクリプト/ツール",
    "docs": "ドキュメント",
    "config": "設定",
}
_ROLE_KEYWORDS = {
    "presentation": {"ui", "gui", "cli", "api", "web", "views", "view", "routes", "route", "controllers", "controller",
                     "handlers", "handler", "endpoints", "rest", "frontend", "app", "commands", "pages", "widgets"},
    "application": {"service", "services", "usecase", "usecases", "application", "workflows", "workflow", "interactors"},
    "domain": {"domain", "model", "models", "entity", "entities", "business", "core", "schema", "schemas"},
    "persistence": {"repository", "repositories", "dao", "store", "stores", "storage", "persistence", "db", "database",
                    "orm", "cache"},
    "infrastructure": {"infra", "infrastructure", "adapters", "adapter", "clients", "client", "external", "gateway",
                       "integration", "integrations", "providers", "backends", "backend", "drivers"},
    "shared": {"util", "utils", "common", "helpers", "helper", "lib", "libs", "shared", "support", "tools_common"},
    "test": {"test", "tests", "testing", "spec", "specs"},
    "script": {"script", "scripts", "tools", "bin", "tooling"},
    "docs": {"doc", "docs", "documentation", "examples"},
    "config": {"config", "configs", "settings", "conf"},
}


@dataclass
class Component:
    name: str
    files: int = 0
    definitions: int = 0
    lines: int = 0
    role: str | None = None  # 名前からの推定
    imports: Counter = field(default_factory=Counter)  # 依存先コンポーネント -> import/include の数
    calls: Counter = field(default_factory=Counter)  # 依存先コンポーネント -> 解決済みの呼び出しの数
    depended_by: Counter = field(default_factory=Counter)
    externals: Counter = field(default_factory=Counter)  # 外部連携のカテゴリ -> 使用箇所の数
    level: int = 0

    @property
    def depends_on(self) -> set[str]:
        return set(self.imports) | set(self.calls)


@dataclass(frozen=True)
class LayerViolation:
    source: str
    target: str
    source_role: str
    target_role: str
    imports: int
    calls: int


@dataclass
class Architecture:
    depth: int
    components: dict[str, Component]
    layers: list[list[str]]  # 上位（他に依存する側）から下位（依存される側）へ
    cycles: list[list[str]]
    violations: list[LayerViolation]


class ArchitectureService:
    """ディレクトリ単位のコンポーネントと、その依存の向きから、層構造を求める。

    * コンポーネント間の依存は、解決済みのimport/include と、解決済みの呼び出しから数える（事実）。
    * 層の段数は、依存の向き（循環はひとまとめ）から機械的に求める（事実に基づく）。
    * 「プレゼンテーション層」等の役割は、名前からの推定にすぎず、逆向き依存の指摘も候補である。
    """

    def build(self, index: ProjectIndex, externals: ExternalReport | None = None, depth: int | None = None) -> Architecture:
        paths = {fid: f.relative_path for fid, f in index.files.items()}
        chosen = depth or self._auto_depth(list(paths.values()))
        component_of = {fid: _component(path, chosen) for fid, path in paths.items()}
        components: dict[str, Component] = {name: Component(name) for name in set(component_of.values())}
        for name, component in components.items():
            component.role = _role(name)

        lines: dict[str, int] = defaultdict(int)
        for symbol in index.symbols.values():
            lines[symbol.file_id] = max(lines[symbol.file_id], symbol.end_line)
            if symbol.kind in (SymbolKind.CLASS, SymbolKind.FUNCTION, SymbolKind.METHOD):
                components[component_of[symbol.file_id]].definitions += 1
        for fid in paths:
            component = components[component_of[fid]]
            component.files += 1
            component.lines += lines[fid]

        for dependency in index.dependencies:
            if dependency.resolution_status != ResolutionStatus.RESOLVED or not dependency.target_file_id:
                continue
            source, target = component_of.get(dependency.source_file_id), component_of.get(dependency.target_file_id)
            if source and target and source != target:
                components[source].imports[target] += 1
        for reference in index.references:
            if reference.reference_kind != ReferenceKind.CALL or reference.resolution_status != ResolutionStatus.RESOLVED:
                continue
            source_symbol = index.symbols.get(reference.source_symbol_id)
            target_symbol = index.symbols.get(reference.target_symbol_id or "")
            if source_symbol is None or target_symbol is None:
                continue
            source, target = component_of[source_symbol.file_id], component_of[target_symbol.file_id]
            if source != target:
                components[source].calls[target] += 1

        if externals is not None:
            file_of_symbol = {s.symbol_id: s.file_id for s in index.symbols.values()}
            file_by_path = {f.relative_path: fid for fid, f in index.files.items()}
            for use in externals.uses:
                fid = file_of_symbol.get(use.source_id or "") or file_by_path.get(use.path)
                if fid in component_of:
                    components[component_of[fid]].externals[use.category] += 1

        for name, component in components.items():
            for target in component.depends_on:
                components[target].depended_by[name] = component.imports[target] + component.calls[target]

        edges = {name: component.depends_on for name, component in components.items()}
        cycles = sorted(sorted(c) for c in strongly_connected_components(edges) if len(c) > 1)
        layers = self._layers(components, edges)
        return Architecture(chosen, components, layers, cycles, self._violations(components))

    @staticmethod
    def _auto_depth(paths: list[str]) -> int:
        """コンポーネント数が5以上になる最小の深さ（無ければ最大の深さ）。"""

        best = 1
        for depth in range(1, 6):
            names = {_component(p, depth) for p in paths}
            best = depth
            if len(names) >= 5:
                break
        return best

    @staticmethod
    def _layers(components: dict[str, Component], edges: dict[str, set[str]]) -> list[list[str]]:
        groups = strongly_connected_components({k: {t for t in v if t in components} for k, v in edges.items()})
        group_of = {name: i for i, group in enumerate(groups) for name in group}
        for name in components:
            group_of.setdefault(name, len(group_of) + len(groups))
        successors: dict[int, set[int]] = defaultdict(set)
        for name, targets in edges.items():
            for target in targets:
                if target in group_of and group_of[target] != group_of[name]:
                    successors[group_of[name]].add(group_of[target])
        levels: dict[int, int] = {}

        def level(group: int, trail: frozenset[int] = frozenset()) -> int:
            if group in levels:
                return levels[group]
            below = [level(s, trail | {group}) for s in successors.get(group, ()) if s not in trail]
            levels[group] = 1 + max(below) if below else 0
            return levels[group]

        for name in components:
            components[name].level = level(group_of[name])
        by_level: dict[int, list[str]] = defaultdict(list)
        for name, component in components.items():
            by_level[component.level].append(name)
        return [sorted(by_level[lv]) for lv in sorted(by_level, reverse=True)]

    @staticmethod
    def _violations(components: dict[str, Component]) -> list[LayerViolation]:
        found: list[LayerViolation] = []
        for name, component in components.items():
            if component.role not in ROLE_ORDER:
                continue
            for target in sorted(component.depends_on):
                other = components.get(target)
                if other is None or other.role not in ROLE_ORDER:
                    continue
                if ROLE_ORDER[component.role] > ROLE_ORDER[other.role]:
                    found.append(
                        LayerViolation(name, target, component.role, other.role, component.imports[target], component.calls[target])
                    )
        return found


def _component(path: str, depth: int) -> str:
    parent = PurePosixPath(path).parent.parts
    return "/".join(parent[:depth]) if parent else "(root)"


def _role(name: str) -> str | None:
    """コンポーネント名（パス）の末尾側から順に、役割のキーワードを探す。"""

    for part in reversed([p.lower() for p in name.split("/")]):
        stem = part.split(".")[0]
        for role, words in _ROLE_KEYWORDS.items():
            if stem in words:
                return role
    return None

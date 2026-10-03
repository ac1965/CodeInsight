"""TUIの状態とキー操作（curses に依存しない。端末なしでテストできる）。

構造の階層・ソース・呼び出し関係は、保存済みの解析結果から取り出す。解決できなかった関係は
「未解決」「曖昧」「外部」として、確定・推定と区別して示し、移動できない理由を伝える。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from codeinsight.application.navigation_service import NavigationService, SymbolNotFoundError
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.search_service import SymbolHit, search_symbols_in_index
from codeinsight.domain import Confidence, Project, Symbol
from codeinsight.presentation.labels import status_text
from codeinsight.presentation.structure_view import TreeNode, build_structure_tree

_KIND_MARK = {
    "function": "fn", "method": "fn", "class": "cls", "struct": "st", "union": "un", "enum": "en", "typedef": "td",
    "macro": "#d", "global_variable": "var", "static_variable": "var", "class_variable": "var", "function_declaration": "decl",
}
MAX_RELATIONS = 200
MAX_RESULTS = 60


def display_width(text: str) -> int:
    """端末での表示幅（全角文字は2桁）。"""

    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def clip(text: str, width: int) -> str:
    """表示幅 width に収まるように切り詰める（全角文字を途中で割らない）。"""

    if width <= 0:
        return ""
    text = text.replace("\t", "    ").replace("\n", " ")
    if display_width(text) <= width:
        return text
    out: list[str] = []
    used = 0
    for char in text:
        w = display_width(char)
        if used + w > width - 1:
            break
        out.append(char)
        used += w
    return "".join(out) + "…"


@dataclass(frozen=True)
class Row:
    node: TreeNode
    depth: int
    expandable: bool
    expanded: bool

    @property
    def text(self) -> str:
        node = self.node
        marker = ("▾ " if self.expanded else "▸ ") if self.expandable else "  "
        mark = _KIND_MARK.get(node.kind, "")
        label = f"{mark} {node.label}" if mark else node.label
        suffix = f"  [{node.status}]" if node.status else ""
        return f"{'  ' * self.depth}{marker}{label}{suffix}"


@dataclass(frozen=True)
class Relation:
    """呼び出し元・呼び出し先の1件。target が None なら、プロジェクト内の定義へは移動できない。"""

    direction: str  # caller / callee
    label: str
    kind: str
    status: str
    location: str
    target: Symbol | None
    note: str = ""


@dataclass(frozen=True)
class SourceLines:
    path: str
    lines: list[tuple[int, str]]
    highlight: tuple[int, int]  # 0,0 なら強調なし
    stale: bool
    focus_line: int


class TuiModel:
    def __init__(self, navigation: NavigationService, project: Project, index: ProjectIndex, stale_paths: set[str] | None = None) -> None:
        self.navigation = navigation
        self.project = project
        self.index = index
        self.stale_paths = stale_paths or set()
        self.root = build_structure_tree(index, project.name)
        self._parent: dict[int, TreeNode] = {}
        self._by_symbol: dict[str, TreeNode] = {}
        self._by_path: dict[str, TreeNode] = {}
        self._register(self.root)
        self.expanded: set[int] = {id(self.root)}
        self.cursor = 0
        self.history: list[str] = []  # 移動前のシンボルID（戻る用）
        self._source_cache: dict[str, list[tuple[int, str]]] = {}
        self.relation_cursor = 0
        self._relations_for: str | None = None
        self._relations: list[Relation] = []
        self.rows_cache: list[Row] | None = None

    # --- 階層 ---

    def _register(self, node: TreeNode) -> None:
        if node.symbol_id:
            self._by_symbol[node.symbol_id] = node
        elif node.kind == "file" and node.path:
            self._by_path[node.path] = node
        for child in node.children:
            self._parent[id(child)] = node
            self._register(child)

    @property
    def rows(self) -> list[Row]:
        if self.rows_cache is None:
            rows: list[Row] = []

            def walk(node: TreeNode, depth: int) -> None:
                expandable = bool(node.children)
                expanded = id(node) in self.expanded
                rows.append(Row(node, depth, expandable, expanded))
                if expandable and expanded:
                    for child in node.children:
                        walk(child, depth + 1)

            walk(self.root, 0)
            self.rows_cache = rows
        return self.rows_cache

    def _invalidate(self) -> None:
        self.rows_cache = None
        self.cursor = max(0, min(self.cursor, len(self.rows) - 1))

    @property
    def current(self) -> TreeNode:
        return self.rows[self.cursor].node

    def move(self, delta: int) -> None:
        self.cursor = max(0, min(self.cursor + delta, len(self.rows) - 1))

    def expand(self) -> None:
        node = self.current
        if node.children:
            self.expanded.add(id(node))
            self._invalidate()

    def collapse(self) -> None:
        """展開中なら閉じる。閉じている（または子が無い）なら、親へ移る。"""

        node = self.current
        if node.children and id(node) in self.expanded:
            self.expanded.discard(id(node))
            self._invalidate()
            return
        parent = self._parent.get(id(node))
        if parent is not None:
            self._select_node(parent)

    def toggle(self) -> None:
        if id(self.current) in self.expanded:
            self.collapse()
        else:
            self.expand()

    def _select_node(self, node: TreeNode) -> None:
        chain: list[TreeNode] = []
        walker: TreeNode | None = self._parent.get(id(node))
        while walker is not None:
            chain.append(walker)
            walker = self._parent.get(id(walker))
        self.expanded.update(id(n) for n in chain)  # 祖先を開いて、見える位置にする
        self.rows_cache = None
        for number, row in enumerate(self.rows):
            if row.node is node:
                self.cursor = number
                return

    # --- 現在のシンボル ---

    @property
    def current_symbol(self) -> Symbol | None:
        node = self.current
        return self.index.symbols.get(node.symbol_id) if node.symbol_id else None

    def go_to_symbol(self, symbol: Symbol, remember: bool = True) -> bool:
        node = self._by_symbol.get(symbol.symbol_id)
        if node is None:
            return False
        previous = self.current_symbol
        if remember and previous is not None and previous.symbol_id != symbol.symbol_id:
            self.history.append(previous.symbol_id)
        self._select_node(node)
        self.relation_cursor = 0
        return True

    def go_back(self) -> bool:
        while self.history:
            symbol = self.index.symbols.get(self.history.pop())
            if symbol is not None and self.go_to_symbol(symbol, remember=False):
                return True
        return False

    # --- 呼び出し関係 ---

    @property
    def relations(self) -> list[Relation]:
        symbol = self.current_symbol
        key = symbol.symbol_id if symbol else None
        if key != self._relations_for:
            self._relations_for = key
            self._relations = self._load_relations(symbol) if symbol else []
            self.relation_cursor = 0
        return self._relations

    def _load_relations(self, symbol: Symbol) -> list[Relation]:
        callers = [self._relation("caller", h.source.qualified_name, h, h.source) for h in self.navigation.callers(self.index, symbol)]
        callees = [
            self._relation("callee", h.target.qualified_name if h.target else h.reference.target_name, h, h.target)
            for h in self.navigation.callees(self.index, symbol)
        ]
        # 移動できる（解決済みの）関係を先に、外部ライブラリの呼び出しは最後にする
        order = {"解": 0, "曖": 1, "未": 2, "外": 3}
        for group in (callers, callees):
            group.sort(key=lambda r: order.get(r.status[:1], 4))
        return (callers[:MAX_RELATIONS]) + (callees[:MAX_RELATIONS])

    @staticmethod
    def _relation(direction: str, label: str, hit, target: Symbol | None) -> Relation:
        reference = hit.reference
        return Relation(
            direction, label, reference.reference_kind.value,
            status_text(reference.resolution_status, reference.confidence == Confidence.INFERRED),
            hit.location, target, reference.note or "",
        )

    def move_relation(self, delta: int) -> None:
        count = len(self.relations)
        if count:
            self.relation_cursor = max(0, min(self.relation_cursor + delta, count - 1))

    def follow_relation(self) -> str:
        """選択した関係の先へ移動する。移動できない場合は、理由を返す（空なら移動した）。"""

        relations = self.relations
        if not relations:
            return "この項目に呼び出し関係はありません"
        relation = relations[self.relation_cursor]
        if relation.target is None:
            reason = f"（{relation.note}）" if relation.note else ""
            return f"{relation.status}のため、プロジェクト内の定義へ移動できません{reason}"
        if not self.go_to_symbol(relation.target):
            return "このシンボルは階層に表示されていません"
        return ""

    # --- ソース ---

    def source(self) -> SourceLines | None:
        node = self.current
        if not node.path or node.kind in ("directory", "project"):
            return None
        path = node.path
        if path not in self._source_cache:
            try:
                view = self.navigation.show_source(self.project, self.index, path)
            except (SymbolNotFoundError, OSError):
                return SourceLines(path, [], (0, 0), True, 1)
            self._source_cache[path] = view.lines
        symbol = self.current_symbol
        highlight = (symbol.start_line, symbol.end_line) if symbol else (0, 0)
        return SourceLines(path, self._source_cache[path], highlight, path in self.stale_paths, symbol.start_line if symbol else 1)

    # --- 検索 ---

    def search(self, query: str) -> list[SymbolHit]:
        query = query.strip()
        if not query:
            return []
        return search_symbols_in_index(self.index, query, limit=MAX_RESULTS)


HELP_LINES = [
    "[構造]  ↑↓/jk 移動   →/l/Enter 開く   ←/h 閉じる・親へ   PgUp/PgDn 頁送り   g/G 先頭/末尾",
    "[関係]  Tab/c 呼び出し関係へ   ↑↓/jk 選択   Enter その定義へ移動   Esc/Tab 構造へ戻る",
    "[共通]  / 検索   b 戻る   [ ] ソースを頁送り   ? この説明   q 終了",
    "表示は保存済みの解析結果に基づく。(推定)は確定ではない。未解決・曖昧・外部へは移動できない。",
    "呼び出し関係は静的に確認できた関係で、実行順序や実際に通る経路を示すものではない。",
]


class TuiController:
    """キー入力（"up" "enter" "q" などの名前）を受けて、モデルと画面の状態を更新する。"""

    def __init__(self, model: TuiModel) -> None:
        self.model = model
        self.focus = "tree"  # tree / relations / search / results
        self.search_buffer = ""
        self.results: list[SymbolHit] = []
        self.result_cursor = 0
        self.message = ""
        self.show_help = False
        self.source_scroll = 0  # 強調する行からの相対的な送り量

    def handle(self, key: str) -> bool:
        """False を返したら終了する。"""

        self.message = ""
        if self.show_help:
            self.show_help = False
            return True
        if self.focus == "search":
            return self._search_key(key)
        if key == "q":
            return False
        if key == "?":
            self.show_help = True
        elif key == "b":
            if not self.model.go_back():
                self.message = "戻る先がありません"
            self.source_scroll = 0
        elif key == "/":
            self.focus, self.search_buffer = "search", ""
        elif key == "[":
            self.source_scroll -= 10
        elif key == "]":
            self.source_scroll += 10
        elif self.focus == "results":
            self._results_key(key)
        elif self.focus == "relations":
            self._relations_key(key)
        else:
            self._tree_key(key)
        return True

    def _tree_key(self, key: str) -> None:
        model = self.model
        moved = True
        if key in ("up", "k"):
            model.move(-1)
        elif key in ("down", "j"):
            model.move(1)
        elif key == "pgup":
            model.move(-10)
        elif key == "pgdn":
            model.move(10)
        elif key in ("home", "g"):
            model.move(-len(model.rows))
        elif key in ("end", "G"):
            model.move(len(model.rows))
        elif key in ("right", "l", "enter"):
            model.expand()
        elif key in ("left", "h"):
            model.collapse()
        elif key in ("tab", "c"):
            self.focus = "relations"
            moved = False
        else:
            moved = False
        if moved:
            self.source_scroll = 0

    def _relations_key(self, key: str) -> None:
        model = self.model
        if key in ("up", "k"):
            model.move_relation(-1)
        elif key in ("down", "j"):
            model.move_relation(1)
        elif key == "enter":
            self.message = model.follow_relation()
            self.source_scroll = 0
            if not self.message:
                self.focus = "tree"
        elif key in ("tab", "esc"):
            self.focus = "tree"

    def _search_key(self, key: str) -> bool:
        if key == "esc":
            self.focus = "tree"
        elif key == "enter":
            self.results = self.model.search(self.search_buffer)
            self.result_cursor = 0
            if self.results:
                self.focus = "results"
            else:
                self.message = f"「{self.search_buffer}」に一致するシンボルはありません（構文解析に基づく名前の検索）"
                self.focus = "tree"
        elif key == "backspace":
            self.search_buffer = self.search_buffer[:-1]
        elif len(key) == 1 and key.isprintable():
            self.search_buffer += key
        return True

    def _results_key(self, key: str) -> None:
        if key in ("up", "k"):
            self.result_cursor = max(0, self.result_cursor - 1)
        elif key in ("down", "j"):
            self.result_cursor = min(len(self.results) - 1, self.result_cursor + 1)
        elif key == "enter":
            hit = self.results[self.result_cursor]
            if self.model.go_to_symbol(hit.symbol):
                self.focus = "tree"
                self.source_scroll = 0
            else:
                self.message = "このシンボルは階層に表示されていません"
        elif key == "esc":
            self.focus = "tree"

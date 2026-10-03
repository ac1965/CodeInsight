from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.application.understand_service import Understanding, UnderstandService
from codeinsight.domain import Project, ResolutionStatus, Symbol, SymbolKind


class ContextError(Exception):
    """コンテキストを作れない（ソースが解析後に変更されている、対象が見つからない等）。"""


@dataclass(frozen=True)
class ContextBlock:
    """AIに渡す根拠の1ブロック。引用してよい位置は、path・start/end と spans に限る。"""

    block_id: str
    kind: str  # facts / source / declaration
    title: str
    text: str
    path: str | None = None
    start: int | None = None
    end: int | None = None
    spans: tuple[tuple[str, int, int], ...] = ()  # factsの中で位置として示した範囲


@dataclass
class Context:
    target_kind: str
    target: str
    blocks: list[ContextBlock] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # 切り詰め・省略など、AIにも利用者にも示す制約

    @property
    def paths(self) -> set[str]:
        found = {b.path for b in self.blocks if b.path}
        found |= {path for b in self.blocks for path, _, _ in b.spans}
        return found

    def render(self) -> str:
        parts = []
        for block in self.blocks:
            location = f" {block.path}:{block.start}-{block.end}" if block.path and block.start else ""
            parts.append(f"[{block.block_id}] {block.kind}: {block.title}{location}\n{block.text}")
        if self.notes:
            parts.append("[制約]\n" + "\n".join(f"- {n}" for n in self.notes))
        return "\n\n".join(parts)

    def digest(self) -> str:
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()

    def covers(self, path: str, start: int, end: int) -> bool:
        """引用 path:start-end が、渡した根拠のいずれかの範囲と重なるか。"""

        for block in self.blocks:
            if block.path == path and block.start is not None and block.end is not None:
                if start <= block.end and end >= block.start:
                    return True
            for span_path, span_start, span_end in block.spans:
                if span_path == path and start <= span_end and end >= span_start:
                    return True
        return False

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


class _Budget:
    def __init__(self, context: Context, max_chars: int) -> None:
        self._context = context
        self._remaining = max_chars
        self._counter = 0

    def next_id(self) -> str:
        self._counter += 1
        return f"C{self._counter}"

    def add_facts(self, title: str, lines: list[str], spans: list[tuple[str, int, int]]) -> None:
        text = "\n".join(lines)
        if len(text) > self._remaining:
            kept: list[str] = []
            used = 0
            for line in lines:
                if used + len(line) + 1 > self._remaining:
                    break
                kept.append(line)
                used += len(line) + 1
            self._context.notes.append(f"事実（{title}）は文字数の上限により一部を省略しました。")
            text = "\n".join(kept)
        self._remaining -= len(text)
        self._context.blocks.append(ContextBlock(self.next_id(), "facts", title, text, spans=tuple(spans)))

    def add_source(self, kind: str, title: str, path: str, lines: list[str], start: int, end: int, share: float = 1.0) -> None:
        """行番号つきのソースを追加する。予算を超える場合は、行単位で切り詰めて、その旨を記録する。"""

        start = _include_decorators(lines, start)
        allowed = max(200, int(self._remaining * share))
        rendered: list[str] = []
        used = 0
        last = start - 1
        for number in range(start, end + 1):
            line = f"{number:>5}| {lines[number - 1]}"
            if used + len(line) + 1 > allowed:
                self._context.notes.append(f"{path}:{start}-{end} のうち {number}行目以降は、文字数の上限により含めていません。")
                break
            rendered.append(line)
            used += len(line) + 1
            last = number
        if not rendered:
            self._context.notes.append(f"{path}:{start}-{end} は、文字数の上限により含めていません。")
            return
        self._remaining -= used
        self._context.blocks.append(
            ContextBlock(self.next_id(), kind, title, "\n".join(rendered), path, start, last)
        )

    @property
    def remaining(self) -> int:
        return self._remaining


class ContextBuilder:
    """AIに渡す根拠（解析結果の事実とソース）を、文字数の予算内で組み立てる（AGENTS.md §3.8）。

    * 事実は解析器の出力（名前・位置・件数・履歴）。ソースは行番号つきで渡す。
    * include_source=False では、生のソース行を一切含めず、事実（名前・位置・件数・docstringの先頭行）だけを渡す。
    * ソースが解析後に変更されている場合は、位置が対応しないため、コンテキストを作らない。
    """

    def __init__(self, navigation: NavigationService, max_chars: int = 14000, include_source: bool = True) -> None:
        self._navigation = navigation
        self._understand = UnderstandService(navigation)
        self._max_chars = max_chars
        self._include_source = include_source

    # --- シンボル（関数・クラス・メソッド） ---

    def for_symbol(self, project: Project, index: ProjectIndex, symbol: Symbol) -> Context:
        context = Context("symbol", symbol.qualified_name)
        budget = _Budget(context, self._max_chars)
        path = index.path_of(symbol.file_id)
        lines = self._read(project, index, path) if self._include_source else []
        card = self._understand.understand(project, index, symbol, depth=2)
        facts, spans = _symbol_facts(index, symbol, card, self._navigation)
        budget.add_facts(f"{symbol.qualified_name} の解析結果", facts, spans)
        if self._include_source:
            budget.add_source("source", symbol.qualified_name, path, lines, symbol.start_line, symbol.end_line, 0.55)
            self._related(project, index, symbol, card, budget)
        else:
            context.notes.append("ソースコード本体は含めていません（--no-source）。解析結果の事実のみです。")
        return context

    def _related(self, project: Project, index: ProjectIndex, symbol: Symbol, card: Understanding, budget: _Budget) -> None:
        """関連する関数の宣言（呼び出し先）と、呼び出し箇所の1行（呼び出し元）。"""

        seen: set[str] = set()
        for hit in self._navigation.callees(index, symbol):
            target = hit.target
            if target is None or target.symbol_id in seen or hit.reference.resolution_status != ResolutionStatus.RESOLVED:
                continue
            seen.add(target.symbol_id)
            if len(seen) > 5 or budget.remaining < 400:
                break
            target_path = index.path_of(target.file_id)
            try:
                target_lines = self._read(project, index, target_path)
            except ContextError:
                continue
            end = min(target.end_line, target.start_line + 7)  # 宣言とdocstringの冒頭だけ
            budget.add_source("declaration", f"呼び出し先 {target.qualified_name}", target_path, target_lines, target.start_line, end, 0.2)
        shown = 0
        for hit in card.callers:
            if shown >= 4 or budget.remaining < 300:
                break
            caller_path = hit.path
            try:
                caller_lines = self._read(project, index, caller_path)
            except ContextError:
                continue
            line = hit.reference.source_location.start_line
            budget.add_source("declaration", f"呼び出し元 {hit.source.qualified_name} の呼び出し箇所", caller_path, caller_lines, line, line, 0.1)
            shown += 1

    # --- ファイル ---

    def for_file(self, project: Project, index: ProjectIndex, relative_path: str) -> Context:
        source_file = index.file_by_path(relative_path)
        if source_file is None:
            raise ContextError(f"解析対象のファイルが見つかりません: {relative_path}")
        context = Context("file", relative_path)
        budget = _Budget(context, self._max_chars)
        lines = self._read(project, index, relative_path)
        symbols = sorted(
            (s for s in index.symbols.values() if s.file_id == source_file.file_id and s.kind != SymbolKind.LOCAL_VARIABLE),
            key=lambda s: s.start_line,
        )
        facts: list[str] = [f"ファイル: {relative_path}（{source_file.language.value}、{len(lines)}行）"]
        spans: list[tuple[str, int, int]] = [(relative_path, 1, max(len(lines), 1))]
        module = next((s for s in symbols if s.kind == SymbolKind.MODULE), None)
        if module and module.summary:
            facts.append(f"モジュールのdocstring（先頭行）: {module.summary}")
        facts.append("定義の一覧（種類・位置・docstringの先頭行）:")
        for symbol in symbols:
            if symbol.kind == SymbolKind.MODULE:
                continue
            summary = f" — {symbol.summary}" if symbol.summary else ""
            facts.append(f"- {symbol.kind.value} {symbol.qualified_name} [{relative_path}:{symbol.start_line}-{symbol.end_line}]{summary}")
        outgoing = [d for d in index.dependencies if d.source_file_id == source_file.file_id]
        if outgoing:
            facts.append("このファイルが依存するもの:")
            for dep in outgoing[:25]:
                target = index.path_of(dep.target_file_id) if dep.target_file_id else f"{dep.target_name}（{dep.resolution_status.value}）"
                facts.append(f"- {dep.dependency_kind.value} {target} [{relative_path}:{dep.evidence_location.start_line}]")
        incoming = sorted({index.path_of(d.source_file_id) for d in index.dependencies if d.target_file_id == source_file.file_id})
        if incoming:
            facts.append("このファイルに依存するファイル: " + ", ".join(incoming[:15]))
        budget.add_facts(f"{relative_path} の解析結果", facts, spans)
        if self._include_source:
            if len(lines) * 45 <= budget.remaining * 0.7:
                budget.add_source("source", relative_path, relative_path, lines, 1, len(lines), 0.9)
            else:
                context.notes.append("ファイルが大きいため、全文ではなく各定義の宣言部分（冒頭の数行）のみ含めています。")
                for symbol in symbols:
                    if symbol.kind in (SymbolKind.MODULE, SymbolKind.LOCAL_VARIABLE) or budget.remaining < 300:
                        continue
                    end = min(symbol.end_line, symbol.start_line + 3)
                    budget.add_source("declaration", symbol.qualified_name, relative_path, lines, symbol.start_line, end, 0.08)
        else:
            context.notes.append("ソースコード本体は含めていません（--no-source）。解析結果の事実のみです。")
        return context

    # --- 呼び出し経路 ---

    def for_path(self, project: Project, index: ProjectIndex, source: Symbol, target: Symbol, max_paths: int = 3) -> Context:
        paths = self._navigation.call_paths(index, source, target, 8, max_paths)
        if not paths:
            raise ContextError("静的に確認できる呼び出し経路が見つかりませんでした（関数ポインタ等の未解決の呼び出しは含みません）。")
        context = Context("path", f"{source.qualified_name} -> {target.qualified_name}")
        budget = _Budget(context, self._max_chars)
        facts: list[str] = []
        spans: list[tuple[str, int, int]] = []
        involved: dict[str, Symbol] = {source.symbol_id: source}
        for number, path in enumerate(paths, 1):
            facts.append(f"経路{number}:")
            current = source
            for hit in path:
                callee = hit.target
                if callee is None:
                    continue
                involved[callee.symbol_id] = callee
                facts.append(f"- {current.qualified_name} が {hit.path}:{hit.reference.source_location.start_line} で {callee.qualified_name} を呼ぶ")
                spans.append((hit.path, hit.reference.source_location.start_line, hit.reference.source_location.end_line))
                current = callee
        facts.append("※ 静的に確認できる呼び出しの連鎖であり、実行時にこの経路を通るとは限りません。")
        budget.add_facts("呼び出し経路（解析結果）", facts, spans)
        if self._include_source:
            share = 1.0 / max(len(involved), 1)
            for symbol in involved.values():
                symbol_path = index.path_of(symbol.file_id)
                lines = self._read(project, index, symbol_path)
                budget.add_source("source", symbol.qualified_name, symbol_path, lines, symbol.start_line, symbol.end_line, share)
        else:
            context.notes.append("ソースコード本体は含めていません（--no-source）。")
        return context

    # --- 質問 ---

    def for_question(self, project: Project, index: ProjectIndex, question: str, limit: int = 6) -> Context:
        candidates = retrieve_symbols(index, question, limit)
        if not candidates:
            raise ContextError("質問に関連するコードを、解析結果から特定できませんでした（AIには問い合わせていません）。")
        context = Context("question", question)
        budget = _Budget(context, self._max_chars)
        facts = ["質問に関連すると判断したシンボル（名前・docstring・位置の一致による機械的な検索。重要度順）:"]
        spans: list[tuple[str, int, int]] = []
        for symbol in candidates:
            path = index.path_of(symbol.file_id)
            summary = f" — {symbol.summary}" if symbol.summary else ""
            facts.append(f"- {symbol.kind.value} {symbol.qualified_name} [{path}:{symbol.start_line}-{symbol.end_line}]{summary}")
            spans.append((path, symbol.start_line, symbol.end_line))
            callers = self._navigation.callers(index, symbol)
            callees = [h for h in self._navigation.callees(index, symbol) if h.target is not None]
            facts.append(f"  呼び出し元 {len(callers)}件、呼び出し先（解決済み） {len(callees)}件")
        budget.add_facts("検索結果（解析結果）", facts, spans)
        if self._include_source:
            share = 1.0 / len(candidates)
            for symbol in candidates:
                path = index.path_of(symbol.file_id)
                lines = self._read(project, index, path)
                budget.add_source("source", symbol.qualified_name, path, lines, symbol.start_line, symbol.end_line, share)
        else:
            context.notes.append("ソースコード本体は含めていません（--no-source）。")
        return context

    # --- 共通 ---

    def _read(self, project: Project, index: ProjectIndex, relative_path: str) -> list[str]:
        source_file = index.file_by_path(relative_path)
        if source_file is None:
            raise ContextError(f"解析対象のファイルが見つかりません: {relative_path}")
        try:
            data = (project.root_path / relative_path).read_bytes()
        except OSError as exc:
            raise ContextError(f"ファイルを読み込めません: {relative_path}: {exc}") from exc
        if hashlib.sha256(data).hexdigest() != source_file.content_hash:
            raise ContextError(f"{relative_path} は解析後に変更されています。再解析（codeinsight analyze）してから実行してください。")
        return data.decode("utf-8", errors="replace").splitlines()


def _include_decorators(lines: list[str], start: int) -> int:
    """定義の直前にあるデコレータ行（`@...`）まで開始行を広げる。

    解析結果のシンボルの範囲は `def`/`class` 行から始まるが、デコレータは関数の振る舞い
    （キャッシュ・登録・置き換え）を決める。範囲外のまま渡すと、AIはそれを見られない。
    """

    while start > 1 and start - 2 < len(lines) and lines[start - 2].lstrip().startswith("@"):
        start -= 1
    return start


def _symbol_facts(index: ProjectIndex, symbol: Symbol, card: Understanding, navigation: NavigationService) -> tuple[list[str], list[tuple[str, int, int]]]:
    """読解カード（解析結果の事実）を、位置つきの箇条書きにする。"""

    path = card.path
    spans: list[tuple[str, int, int]] = [(path, symbol.start_line, symbol.end_line)]
    facts: list[str] = [f"対象: {symbol.kind.value} {symbol.qualified_name} [{path}:{symbol.start_line}-{symbol.end_line}]（{card.language.value}）"]
    if card.summary:
        facts.append(f"docstring（先頭行）: {card.summary}")
    if card.module_summary:
        facts.append(f"所属モジュールのdocstring（先頭行）: {card.module_summary}")
    for param in card.parameters:
        annotation = f": {param.annotation}" if param.annotation else ""
        default = f" = {param.default}" if param.default else ""
        facts.append(f"引数: {param.name}{annotation}{default}")
        passed = card.caller_arguments.get(param.name)
        if passed:
            facts.append(f"  呼び出し元が渡している値: {', '.join(passed[:4])}")
    if card.return_annotation:
        facts.append(f"戻り値の型注釈: {card.return_annotation}")
    for number, text in card.returns[:8]:
        facts.append(f"return文 [{path}:{number}]: {text}")
        spans.append((path, number, number))
    facts.append(f"呼び出し元（解決済み）: {card.caller_total}件")
    for hit in card.callers[:8]:
        line = hit.reference.source_location.start_line
        facts.append(f"- {hit.source.qualified_name} [{hit.path}:{line}]")
        spans.append((hit.path, line, hit.reference.source_location.end_line))
    resolved = [h for h in navigation.callees(index, symbol) if h.target is not None and h.reference.resolution_status == ResolutionStatus.RESOLVED]
    unresolved = [h for h in navigation.callees(index, symbol) if h.reference.resolution_status in (ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS)]
    facts.append(f"呼び出し先（解決済み）: {len(resolved)}件、静的に特定できない呼び出し: {len(unresolved)}件")
    for hit in resolved[:10]:
        target = hit.target
        assert target is not None
        target_path = index.path_of(target.file_id)
        line = hit.reference.source_location.start_line
        facts.append(f"- [{hit.path}:{line}] → {target.qualified_name} [{target_path}:{target.start_line}-{target.end_line}]")
        spans.append((hit.path, line, line))
        spans.append((target_path, target.start_line, target.end_line))
    for hit in unresolved[:5]:
        line = hit.reference.source_location.start_line
        facts.append(f"- [{hit.path}:{line}] {hit.reference.target_name}（未解決: {hit.reference.note}）")
        spans.append((hit.path, line, line))
    for item in card.config_reads:
        facts.append(f"設定値: {item.kind} {item.name} 既定値={item.default or 'なし'} [{item.path}:{item.line}]")
        spans.append((item.path, item.line, item.line))
    for text in [*card.state_changes, *card.parameter_mutations]:
        facts.append(f"変更: {text}")
    if card.effects:
        for use in card.effects.direct[:10]:
            facts.append(f"外部への操作の候補（直接）: [{use.category}・{use.operation}] {use.library} [{use.path}:{use.line}]")
            spans.append((use.path, use.line, use.line))
        for use, route in card.effects.reachable[:6]:
            facts.append(f"外部への操作の候補（呼び出し先経由）: [{use.category}・{use.operation}] {use.library} [{use.path}:{use.line}] 経路: {' → '.join(route)}")
            spans.append((use.path, use.line, use.line))
    if card.exceptions:
        for exc in card.exceptions.propagated[:8]:
            origin = index.path_of(exc.raised_in.file_id)
            facts.append(f"外へ出うる例外: {exc.exception}（raise [{origin}:{exc.raised_line}] in {exc.raised_in.qualified_name}）")
            spans.append((origin, exc.raised_line, exc.raised_line))
        for handler in card.exceptions.handlers:
            kind = "握りつぶし" if handler.swallowed else ("再送出あり" if handler.reraises else "処理あり")
            facts.append(f"例外処理 [{path}:{handler.line}]: except {', '.join(handler.types)}（{kind}）")
            spans.append((path, handler.line, handler.line))
    for hint in card.resilience:
        facts.append(f"リトライ/タイムアウト/待機の手がかり（推定） [{path}:{hint.line}]: {hint.kind} {hint.detail}")
        spans.append((path, hint.line, hint.line))
    if card.impact:
        production = [a for a in card.impact.affected if not a.is_test]
        facts.append(f"影響範囲: {len(production)}関数・{len({a.path for a in production})}ファイル・{len(card.impact.components)}コンポーネント")
    if card.test_names:
        facts.append("このコードを参照するテスト: " + ", ".join(card.test_names))
    for doc_path, number, text in card.doc_mentions:
        facts.append(f"文書での言及 [{doc_path}:{number}]: {text}")
    if card.history and card.history.commits:
        for commit in card.history.commits[:5]:
            facts.append(f"変更履歴: {commit.date} {commit.hash[:8]} {commit.subject}")
    for number, text in card.todo_comments:
        facts.append(f"未対応のコメント [{path}:{number}]: {text}")
        spans.append((path, number, number))
    for text in card.limitations:
        facts.append(f"解析上の制約: {text}")
    return facts, spans


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# 日本語は、文字種（漢字・カタカナ）ごとの連続を語とする。ひらがなは助詞・活用が多いため語として使わない。
_JAPANESE_WORD = re.compile(r"[一-龠々]{2,}|[ァ-ヶー]{2,}")
_STOP = frozenset({"the", "and", "for", "how", "what", "does", "this", "that", "with", "from", "into", "are", "not", "where", "when"})
_GENERIC_JAPANESE = frozenset({"処理", "説明", "仕組", "実装", "関数", "動作", "機能", "方法", "場合", "内容", "部分", "全体", "クラス", "メソッド", "コード", "ソース", "プログラム"})


def retrieve_symbols(index: ProjectIndex, question: str, limit: int = 4) -> list[Symbol]:
    """質問から、名前・docstring・パスの一致で、関連するシンボルを機械的に検索する（AIには頼らない）。"""

    latin = {t.lower() for t in _TOKEN.findall(question) if t.lower() not in _STOP}
    japanese = {t for t in _JAPANESE_WORD.findall(question) if t not in _GENERIC_JAPANESE}
    if not latin and not japanese:
        return []
    scored: list[tuple[int, Symbol]] = []
    for symbol in index.symbols.values():
        if symbol.kind in (SymbolKind.LOCAL_VARIABLE, SymbolKind.MODULE, SymbolKind.FUNCTION_DECLARATION, SymbolKind.MACRO):
            continue
        name = symbol.name.lower()
        qualified = symbol.qualified_name.lower()
        score = 0
        for token in latin:
            if name == token:
                score += 10
            elif token in name:
                score += 4
            elif token in qualified:
                score += 2
        summary = symbol.summary
        for token in japanese:
            if token in summary:
                score += 4
        for token in latin:
            if token in summary.lower():
                score += 1
        if score:
            if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS):
                score += 1
            scored.append((score, symbol))
    scored.sort(key=lambda item: (-item[0], item[1].qualified_name))
    return [s for _, s in scored[:limit]]

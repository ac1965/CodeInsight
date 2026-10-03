"""TUIの画面の組み立て（描画命令のリストを返す純粋関数。curses に依存しない）。"""

from __future__ import annotations

from dataclasses import dataclass

from codeinsight.presentation.labels import STATIC_NOTE
from codeinsight.presentation.tui_model import HELP_LINES, TuiController, clip, display_width

STYLES = ("normal", "header", "cursor", "highlight", "dim", "warn", "title")


@dataclass(frozen=True)
class Draw:
    y: int
    x: int
    text: str
    style: str = "normal"


def _pad(text: str, width: int) -> str:
    text = clip(text, width)
    return text + " " * max(0, width - display_width(text))


def compose(controller: TuiController, height: int, width: int) -> list[Draw]:
    """画面全体の描画命令を作る。小さすぎる端末では、その旨だけを示す。"""

    if height < 12 or width < 60:
        return [Draw(0, 0, clip("端末が小さすぎます（60桁×12行以上が必要です）", width), "warn")]
    model = controller.model
    ops: list[Draw] = []
    symbols = len(model.index.symbols)
    ops.append(Draw(0, 0, _pad(f" CodeInsight  {model.project.name}  ファイル {len(model.index.files)} / シンボル {symbols}   ? ヘルプ  / 検索  q 終了", width), "header"))

    left = max(30, int(width * 0.38))
    body_top, body_bottom = 1, height - 2  # 先頭に見出し、末尾に状態行と注記
    rel_height = max(6, (body_bottom - body_top) // 3)
    source_bottom = body_bottom - rel_height

    # 左: 構造
    focus_tree = controller.focus == "tree"
    ops.append(Draw(body_top, 0, _pad(" 構造" + ("  ◀" if focus_tree else ""), left), "title"))
    visible = body_bottom - body_top - 1
    top = max(0, min(model.cursor - visible // 2, max(0, len(model.rows) - visible)))
    for line, row in enumerate(model.rows[top: top + visible]):
        y = body_top + 1 + line
        selected = top + line == model.cursor
        ops.append(Draw(y, 0, _pad(row.text, left), "cursor" if selected else ("warn" if row.node.status else "normal")))

    # 右上: ソース
    x0 = left + 1
    right_width = width - x0
    source = model.source()
    title = " ソース"
    if source:
        title += f"  {source.path}" + ("  [解析後に変更されています。行番号がずれている可能性]" if source.stale else "")
    ops.append(Draw(body_top, x0, _pad(title, right_width), "warn" if source and source.stale else "title"))
    if source is None:
        ops.append(Draw(body_top + 2, x0, clip("ディレクトリ／プロジェクトです。ファイルまたはシンボルを選んでください", right_width), "dim"))
    else:
        rows_available = source_bottom - body_top - 1
        start_index = max(0, source.focus_line - 1 - 2 + controller.source_scroll)
        start_index = min(start_index, max(0, len(source.lines) - rows_available))
        for line, (number, text) in enumerate(source.lines[start_index: start_index + rows_available]):
            marked = source.highlight[0] <= number <= source.highlight[1] and source.highlight != (0, 0)
            ops.append(Draw(body_top + 1 + line, x0, _pad(f"{number:>5}| {text}", right_width), "highlight" if marked else "normal"))

    # 右下: 呼び出し関係 / 検索結果
    rel_top = source_bottom
    if controller.focus == "results":
        ops.append(Draw(rel_top, x0, _pad(f" 検索結果 {len(controller.results)}件（構文解析に基づく名前の検索）  ◀", right_width), "title"))
        shown = rel_height - 1
        first = max(0, min(controller.result_cursor - shown // 2, max(0, len(controller.results) - shown)))
        for line, hit in enumerate(controller.results[first: first + shown]):
            style = "cursor" if first + line == controller.result_cursor else "normal"
            ops.append(Draw(rel_top + 1 + line, x0, _pad(f" {hit.symbol.kind.value:<9} {hit.symbol.qualified_name}  {hit.location}", right_width), style))
    else:
        focus_rel = controller.focus == "relations"
        symbol = model.current_symbol
        heading = " 呼び出し関係" + (f"  {symbol.qualified_name}" if symbol else "") + ("  ◀" if focus_rel else "")
        ops.append(Draw(rel_top, x0, _pad(heading, right_width), "title"))
        relations = model.relations
        if symbol is None:
            ops.append(Draw(rel_top + 1, x0, clip(" シンボルを選ぶと、呼び出し元・呼び出し先を表示します", right_width), "dim"))
        elif not relations:
            ops.append(Draw(rel_top + 1, x0, clip(" 呼び出し関係は確認できませんでした", right_width), "dim"))
        shown = rel_height - 1
        first = max(0, min(model.relation_cursor - shown // 2, max(0, len(relations) - shown)))
        for line, relation in enumerate(relations[first: first + shown]):
            arrow = "←" if relation.direction == "caller" else "→"
            text = f" {arrow} {relation.label}  [{relation.status}]  {relation.location}"
            style = "cursor" if focus_rel and first + line == model.relation_cursor else (
                "normal" if relation.target is not None else "dim"
            )
            ops.append(Draw(rel_top + 1 + line, x0, _pad(text, right_width), style))

    # 状態行・注記
    if controller.focus == "search":
        ops.append(Draw(height - 2, 0, _pad(f" 検索: {controller.search_buffer}_   (Enterで検索 / Escで取消)", width), "title"))
    else:
        ops.append(Draw(height - 2, 0, _pad(" " + controller.message, width), "warn" if controller.message else "normal"))
    ops.append(Draw(height - 1, 0, _pad(" " + STATIC_NOTE, width), "dim"))

    if controller.show_help:
        box_width = min(width - 4, 100)
        for line, text in enumerate(["ヘルプ（何かキーで閉じる）", *HELP_LINES]):
            ops.append(Draw(3 + line, 2, _pad(" " + text, box_width), "title" if line == 0 else "cursor"))
    return ops

"""cursesによるTUI。状態は tui_model、画面の組み立ては tui_view に任せ、ここは入出力だけを行う。"""

from __future__ import annotations

import curses

from codeinsight.presentation.tui_model import TuiController, TuiModel, display_width
from codeinsight.presentation.tui_view import compose

_KEYS = {
    curses.KEY_UP: "up", curses.KEY_DOWN: "down", curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
    curses.KEY_NPAGE: "pgdn", curses.KEY_PPAGE: "pgup", curses.KEY_HOME: "home", curses.KEY_END: "end",
    curses.KEY_BACKSPACE: "backspace", 127: "backspace", 8: "backspace", 10: "enter", 13: "enter",
    curses.KEY_ENTER: "enter", 9: "tab", 27: "esc",
}


def _start_colors() -> dict[str, int]:
    attrs = {"normal": 0, "header": curses.A_REVERSE, "cursor": curses.A_REVERSE, "highlight": curses.A_BOLD,
             "dim": curses.A_DIM, "warn": curses.A_BOLD, "title": curses.A_BOLD | curses.A_UNDERLINE}
    return attrs


def _read_key(screen) -> str:
    code = screen.get_wch()
    if isinstance(code, str):
        return _KEYS.get(ord(code), code) if len(code) == 1 else code
    return _KEYS.get(code, "")


def _draw(screen, controller: TuiController, attrs: dict[str, int]) -> None:
    height, width = screen.getmaxyx()
    screen.erase()
    for op in compose(controller, height, width):
        if op.y >= height:
            continue
        text = op.text
        # 最終桁への書き込みで例外になる端末があるため、はみ出す分は切る
        while display_width(text) + op.x > width - 1 and text:
            text = text[:-1]
        try:
            screen.addstr(op.y, op.x, text, attrs.get(op.style, 0))
        except curses.error:
            pass
    screen.refresh()


def _main(screen, model: TuiModel) -> None:
    curses.curs_set(0)
    screen.keypad(True)
    attrs = _start_colors()
    controller = TuiController(model)
    while True:
        _draw(screen, controller, attrs)
        key = _read_key(screen)
        if key == "" or key == "KEY_RESIZE":
            continue
        if not controller.handle(key):
            return


def run(model: TuiModel) -> None:
    curses.wrapper(_main, model)

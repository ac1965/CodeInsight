"""C の収集器（runtime/ccollect.sh）が書いた gcov のテキストを、観測のJSON（codeinsight.observation/1）へ変換する。

純粋な文字列の変換で、何も実行しない。対象は gcov が報告する **関数の実行回数と、実行された行** だけ
（呼び出しの辺・引数・戻り値は、この収集器では得られない）。ファイルの位置は、作業領域の複製の中のパスを、対象の相対パスに戻す。
"""

from __future__ import annotations

import os
import re
from collections import defaultdict

SCHEMA = "codeinsight.observation/1"
_FUNCTION = re.compile(r"^function (?P<name>\S+) called (?P<count>\d+) returned")
_LINE = re.compile(r"^\s*(?P<count>[^:]+):\s*(?P<line>\d+):(?P<text>.*)$")
_NOT_EXECUTED = ("#####", "=====")


def _relative(source: str, gcov_dir: str, work: str) -> str | None:
    """gcov の `Source:` を、対象の相対パスにする。作業領域の外（システムヘッダーなど）は None。"""

    path = source if os.path.isabs(source) else os.path.join(gcov_dir, source)
    path = os.path.normpath(path)
    prefix = os.path.normpath(work) + os.sep
    if path.startswith(prefix):
        return path[len(prefix):]
    return None


def parse(text: str, exit_code: int | None, duration_seconds: float = 0.0) -> dict:
    work = ""
    functions: dict[tuple[str, str, int], int] = defaultdict(int)
    lines: dict[str, set[int]] = defaultdict(set)
    gcov_dir = ""
    path: str | None = None
    pending: tuple[str, int] | None = None  # 直前の `function NAME called N` の（名前, 回数）。次の番号つきの行が開始行
    for raw in text.splitlines():
        if raw.startswith("==WORK "):
            work = raw[len("==WORK "):].strip()
            continue
        if raw.startswith("==GCOV "):
            gcov_dir, path, pending = raw[len("==GCOV "):].strip(), None, None
            continue
        if raw.startswith("function "):
            matched = _FUNCTION.match(raw)
            pending = (matched["name"], int(matched["count"])) if matched else None
            continue
        entry = _LINE.match(raw)
        if entry is None:
            continue
        number, count = int(entry["line"]), entry["count"].strip()
        if number == 0:
            if entry["text"].startswith("Source:"):
                path = _relative(entry["text"][len("Source:"):].strip(), gcov_dir, work)
            continue
        if path is None:
            pending = None
            continue
        if pending is not None:
            functions[(path, pending[0], number)] += pending[1]
            pending = None
        if count not in ("-", *_NOT_EXECUTED) and count.rstrip("*").isdigit() and int(count.rstrip("*")) > 0:
            lines[path].add(number)
    return {
        "schema": SCHEMA, "language": "c", "mechanism": "gcov", "exit_code": exit_code, "duration_seconds": round(duration_seconds, 3),
        # 実行回数が 0 の関数は、観測として記録しない（「観測されなかった」と同じ扱い。呼ばれないことを意味しない）
        "functions": [{"file": f, "qualname": q, "line": n, "count": c} for (f, q, n), c in sorted(functions.items()) if c > 0],
        "calls": [], "lines": {p: sorted(v) for p, v in sorted(lines.items())}, "raised": [], "uncaught": None, "dropped_edges": 0,
    }

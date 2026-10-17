"""C の収集器（runtime/ccollect.sh）が書いた gcov のテキストを、観測のJSON（codeinsight.observation/1）へ変換する。

純粋な文字列の変換で、何も実行しない。対象は gcov が報告する **関数の実行回数と、実行された行** だけ
（呼び出しの辺・引数・戻り値は、この収集器では得られない）。ファイルの位置は、作業領域の複製の中のパスを、対象の相対パスに戻す。
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Any

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
    section = "gcov"  # gcov / calls / io（計装の記録。ccollect.sh が、gcov の後ろに足す）
    call_rows: list[list[str]] = []
    io_rows: list[list[str]] = []
    dropped = (0, 0)
    instrumented = False
    for raw in text.splitlines():
        if raw.startswith("==WORK "):
            work = raw[len("==WORK "):].strip()
            continue
        if raw.startswith("==CALLS"):
            section, instrumented = "calls", True
            continue
        if raw.startswith("==IO"):
            section = "io"
            continue
        if raw.startswith("==DROPPED"):
            parts = raw.split("\t")
            dropped = (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0, int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0)
            section = "done"
            continue
        if section == "calls":
            call_rows.append(raw.split("\t"))
            continue
        if section == "io":
            io_rows.append(raw.split("\t"))
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
    def located(location: str) -> tuple[str, int] | None:
        """addr2line の `パス:行`（作業領域の中のパス）を、(対象の相対パス, 行) にする。作業領域の外・不明は None。"""

        file, _, number = location.rpartition(":")
        number = number.split(" ")[0]
        relative = _relative(file, work, work) if work and file else None
        return (relative, int(number)) if relative and number.isdigit() else None

    calls: list[dict[str, Any]] = []
    for row in call_rows:
        if len(row) < 5 or not row[0].isdigit():
            continue
        callee, caller = located(row[2]), located(row[4])
        if callee is None or caller is None:  # ライブラリ・作業領域の外の関数は、プロジェクトの辺ではない
            continue
        calls.append({"caller": {"file": caller[0], "qualname": row[3], "line": caller[1]}, "callee": {"file": callee[0], "qualname": row[1], "line": callee[1]}, "count": int(row[0])})
    merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}  # 同じ関数の組は、呼び出し箇所が複数あっても1本にまとめる（回数は合算。行は最小）
    for call in calls:
        key = (call["caller"]["file"], call["caller"]["qualname"], call["callee"]["file"], call["callee"]["qualname"])
        if key in merged:
            merged[key]["count"] += call["count"]
            merged[key]["caller"]["line"] = min(merged[key]["caller"]["line"], call["caller"]["line"])
        else:
            merged[key] = call
    calls = list(merged.values())
    io = []
    for row in io_rows:
        if len(row) < 5 or not row[0].isdigit():
            continue
        caller = located(row[3])
        io.append({"kind": row[1], "caller": {"file": caller[0], "qualname": row[2], "line": caller[1]} if caller else None, "detail": row[4], "count": int(row[0])})
    return {
        "schema": SCHEMA, "language": "c", "mechanism": "gcov+instrument" if instrumented else "gcov", "exit_code": exit_code, "duration_seconds": round(duration_seconds, 3),
        # 実行回数が 0 の関数は、観測として記録しない（「観測されなかった」と同じ扱い。呼ばれないことを意味しない）
        "functions": [{"file": f, "qualname": q, "line": n, "count": c} for (f, q, n), c in sorted(functions.items()) if c > 0],
        "calls": sorted(calls, key=lambda c: (c["caller"]["file"], c["caller"]["qualname"], c["callee"]["qualname"])), "io": io,
        "lines": {p: sorted(v) for p, v in sorted(lines.items())}, "raised": [], "uncaught": None, "dropped_edges": dropped[0], "dropped_io": dropped[1],
    }

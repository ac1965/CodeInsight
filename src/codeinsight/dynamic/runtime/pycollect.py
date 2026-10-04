"""Pythonの実行を観測する収集器。**隔離された環境（コンテナ）の中で動く**ことを前提にした、標準ライブラリだけのスクリプト。

使い方: `python pycollect.py --root <対象のルート> --out <出力のJSON> -- <スクリプト [引数...]> | -m <モジュール [引数...]>`

対象のプログラムを実行し、対象のルートの中のファイルについて、次を記録する（**値は記録しない**。名前・位置・件数だけ）:
* 実行された関数（ファイル・修飾名・開始行）と、関数の呼び出し（呼び出し元・呼び出し先・回数）
* 実行された行（ファイルごと）
* 送出された例外の型・位置・回数（捕捉されたものを含む）と、捕捉されずに終了させた例外
* 終了コード

Python 3.12 以降は `sys.monitoring`、それ以前は `sys.settrace` を使う（環境変数 CODEINSIGHT_COLLECTOR_MECHANISM=settrace で、後者を強制できる）。
観測は「この実行で起きたこと」の証拠であり、観測されなかったことは、起きないことを意味しない。
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

MAX_EDGES = 50_000
MAX_FAILURES = 500
SCHEMA = "codeinsight.observation/1"


class Recorder:
    def __init__(self, root: Path) -> None:
        self.root = str(root.resolve()) + os.sep
        self._names: dict[str, str | None] = {}
        self.functions: dict[tuple[str, str, int], int] = Counter()  # (相対パス, 修飾名, 開始行) -> 実行された回数
        self.calls: Counter[tuple[tuple[str, str, int], tuple[str, str, int]]] = Counter()
        self.lines: dict[str, set[int]] = {}
        self.raised: Counter[tuple[str, str, int, str]] = Counter()  # (型, 相対パス, 行, 関数) -> 回数
        self.dropped_edges = 0

    def relative(self, filename: str) -> str | None:
        cached = self._names.get(filename, ...)
        if cached is not ...:
            return cached  # type: ignore[return-value]
        result: str | None = None
        if filename and not filename.startswith("<"):
            absolute = os.path.realpath(filename)
            if absolute.startswith(self.root):
                result = absolute[len(self.root):].replace(os.sep, "/")
        self._names[filename] = result
        return result

    def function_key(self, code) -> tuple[str, str, int] | None:
        path = self.relative(code.co_filename)
        if path is None:
            return None
        qualname = getattr(code, "co_qualname", code.co_name).replace(".<locals>.", ".")
        return (path, qualname, code.co_firstlineno)

    def on_call(self, callee_code, caller_code) -> None:
        callee = self.function_key(callee_code)
        if callee is None:
            return
        self.functions[callee] += 1
        caller = self.function_key(caller_code) if caller_code is not None else None
        if caller is not None:
            edge = (caller, callee)
            if edge in self.calls or len(self.calls) < MAX_EDGES:
                self.calls[edge] += 1
            else:
                self.dropped_edges += 1

    def on_line(self, code, line: int) -> None:
        path = self.relative(code.co_filename)
        if path is not None:
            self.lines.setdefault(path, set()).add(line)

    def on_raise(self, code, line: int, exception: BaseException) -> None:
        path = self.relative(code.co_filename)
        if path is not None:
            name = getattr(code, "co_qualname", code.co_name).replace(".<locals>.", ".")
            self.raised[(type(exception).__name__, path, line, name)] += 1


def start_monitoring(recorder: Recorder) -> str:
    monitoring = sys.monitoring  # type: ignore[attr-defined]
    tool = monitoring.COVERAGE_ID
    monitoring.use_tool_id(tool, "codeinsight")
    events = monitoring.events

    def on_start(code, offset) -> None:
        # 呼び出しの先頭で、新しい関数のフレーム（深さ1）の呼び出し元が、深さ2
        frame = sys._getframe(2)
        recorder.on_call(code, frame.f_code)

    def on_line(code, line):
        recorder.on_line(code, line)
        return monitoring.DISABLE  # 同じ場所の行は、2回目以降は通知を受けない（速度のため）

    def on_raise(code, offset, exception) -> None:
        line = code.co_firstlineno
        for start, _, number in code.co_lines():
            if start <= offset:
                line = number if number is not None else line
        recorder.on_raise(code, line, exception)

    monitoring.register_callback(tool, events.PY_START, on_start)
    monitoring.register_callback(tool, events.LINE, on_line)
    monitoring.register_callback(tool, events.RAISE, on_raise)
    monitoring.set_events(tool, events.PY_START | events.LINE | events.RAISE)
    return "sys.monitoring"


def start_settrace(recorder: Recorder) -> str:
    def local_trace(frame, event, arg):
        if event == "line":
            recorder.on_line(frame.f_code, frame.f_lineno)
        elif event == "exception":
            recorder.on_raise(frame.f_code, frame.f_lineno, arg[1])
        return local_trace

    def global_trace(frame, event, arg):
        if event != "call":
            return None
        back = frame.f_back
        recorder.on_call(frame.f_code, back.f_code if back is not None else None)
        return local_trace if recorder.relative(frame.f_code.co_filename) is not None else None

    sys.settrace(global_trace)
    return "sys.settrace"


def stop_tracing(mechanism: str) -> None:
    if mechanism == "sys.monitoring":
        monitoring = sys.monitoring  # type: ignore[attr-defined]
        monitoring.set_events(monitoring.COVERAGE_ID, 0)
        monitoring.free_tool_id(monitoring.COVERAGE_ID)
    else:
        sys.settrace(None)


def run_target(command: list[str], root: Path) -> int:
    """対象を、Pythonの `python <script>` / `python -m <module>` と同じように実行し、終了コードを返す。"""

    if not command:
        raise SystemExit("実行する対象（スクリプトまたは -m モジュール）が指定されていません")
    os.chdir(root)
    if command[0] == "-m":
        if len(command) < 2:
            raise SystemExit("-m の後に、モジュール名を指定してください")
        sys.path.insert(0, str(root))
        sys.argv = [command[1], *command[2:]]
        runpy.run_module(command[1], run_name="__main__", alter_sys=True)
    elif command[0].startswith("-"):
        raise SystemExit(f"対応していないオプションです: {command[0]}（スクリプトのパスか、-m モジュール のみ）")
    else:
        script = Path(command[0])
        script = script if script.is_absolute() else root / script
        sys.path.insert(0, str(script.parent))
        sys.argv = [str(script), *command[1:]]
        runpy.run_path(str(script), run_name="__main__")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    root = Path(args.root)
    recorder = Recorder(root)
    forced = os.environ.get("CODEINSIGHT_COLLECTOR_MECHANISM")
    mechanism = start_monitoring(recorder) if hasattr(sys, "monitoring") and forced != "settrace" else start_settrace(recorder)
    started = time.monotonic()
    exit_code = 0
    uncaught: dict | None = None
    try:
        run_target(command, root)
    except SystemExit as exc:
        code = exc.code
        exit_code = code if isinstance(code, int) else (0 if code is None else 1)
    except BaseException as exc:  # noqa: BLE001 - 対象が捕捉しなかった例外を記録する（値・メッセージは記録しない）
        exit_code = 1
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if recorder.relative(f.filename) is not None]
        last = frames[-1] if frames else None
        uncaught = {"type": type(exc).__name__, "file": recorder.relative(last.filename) if last else None, "line": last.lineno if last else None, "function": last.name if last else None}
    finally:
        stop_tracing(mechanism)
    duration = time.monotonic() - started
    result = {
        "schema": SCHEMA, "python": sys.version.split()[0], "mechanism": mechanism, "exit_code": exit_code, "duration_seconds": round(duration, 3),
        "functions": [{"file": f, "qualname": q, "line": n, "count": c} for (f, q, n), c in sorted(recorder.functions.items())],
        "calls": [
            {"caller": {"file": a[0], "qualname": a[1], "line": a[2]}, "callee": {"file": b[0], "qualname": b[1], "line": b[2]}, "count": c}
            for (a, b), c in sorted(recorder.calls.items())
        ],
        "lines": {path: sorted(numbers) for path, numbers in sorted(recorder.lines.items())},
        "raised": [{"type": t, "file": f, "line": n, "function": q, "count": c} for (t, f, n, q), c in sorted(recorder.raised.items())[:MAX_FAILURES]],
        "uncaught": uncaught, "dropped_edges": recorder.dropped_edges,
    }
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""libclang の共有ライブラリの選択。

pip で入る libclang（`libclang` パッケージ）は 18.1.1 が最新で、C23 以降の構文（`__builtin_c23_va_start` など）を
解析できない。新しい LLVM / Xcode が計算機にある場合、環境変数 `CODEINSIGHT_LIBCLANG` でその共有ライブラリを
指定すると、同じ Python バインディングのまま、新しい構文を解析できる（読み取り専用の構文解析。対象はコンパイルも実行もしない）。

自動では選ばない（版の違いで結果が変わるため、利用者が明示する）。指定が無効なときは、同梱のものに戻して警告する。
`candidates()` は、この計算機で見つかる候補の一覧（`doctor` 等で案内する用）。
"""

from __future__ import annotations

import glob
import os
import platform
import warnings
from pathlib import Path

import clang.cindex as cindex

ENV = "CODEINSIGHT_LIBCLANG"
_state: dict[str, str] = {}


def candidates() -> list[str]:
    """この計算機にある、同梱以外の libclang の候補（存在するもののみ）。"""

    patterns = {
        "Darwin": [
            "/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/lib/libclang.dylib",
            "/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
            "/opt/homebrew/opt/llvm*/lib/libclang.dylib",
            "/usr/local/opt/llvm*/lib/libclang.dylib",
        ],
    }.get(platform.system(), ["/usr/lib/llvm-*/lib/libclang*.so*", "/usr/lib64/libclang*.so*", "/usr/lib/x86_64-linux-gnu/libclang*.so*"])
    found: list[str] = []
    for pattern in patterns:
        found.extend(sorted(glob.glob(pattern)))
    return [p for p in found if Path(p).is_file()]


def configure() -> str:
    """環境変数が指定されていれば、その libclang を使う設定をする（最初の解析器の作成前に1回だけ）。説明文を返す。"""

    if "description" in _state:
        return _state["description"]
    requested = os.environ.get(ENV, "").strip()
    description = "同梱の libclang（pip の libclang パッケージ）"
    if requested:
        path = Path(requested).expanduser()
        if not path.is_file():
            warnings.warn(f"{ENV}={requested} が見つからないため、同梱の libclang を使います。", stacklevel=2)
        else:
            try:
                cindex.Config.set_library_file(str(path))
                _ = cindex.conf.lib  # ここで読み込み。失敗すれば例外
                description = f"{path}（{ENV} で指定）"
            except Exception as exc:  # noqa: BLE001 - 読み込めない共有ライブラリは同梱に戻す
                warnings.warn(f"{ENV}={requested} を読み込めないため（{type(exc).__name__}）、同梱の libclang を使います。", stacklevel=2)
                cindex.Config.loaded = False
                cindex.Config.library_file = None
    _state["description"] = description
    return description


def requested_library() -> str:
    """指紋用（読み込まずに、指定の文字列だけを返す）。"""

    return os.environ.get(ENV, "").strip()

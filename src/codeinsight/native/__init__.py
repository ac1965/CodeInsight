"""重い処理（ファイル走査・内容ハッシュ・グラフ探索）を速くするための、Goの補助プログラム（`cifast`）の呼び出し口。

補助プログラムは任意で、無い・作れない・失敗した場合は、`None` を返す（呼び出し側が Python の実装で処理する。結果は同じ）。
環境変数 `CODEINSIGHT_NO_NATIVE=1` で、使わない指定ができる。
"""

from codeinsight.native.helper import NativeHelper, native_helper

__all__ = ["NativeHelper", "native_helper"]

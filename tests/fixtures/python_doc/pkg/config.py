"""設定値を提供するモジュール。

詳細な説明はここ以降に書く。
"""

DEFAULT_TIMEOUT = 30
RETRIES = 3


class Settings:
    """利用者ごとの設定。"""

    timeout: int = DEFAULT_TIMEOUT

    def effective(self) -> int:
        """有効なタイムアウトを返す。"""
        return self.timeout

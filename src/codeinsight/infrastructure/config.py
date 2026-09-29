from __future__ import annotations

import os
from pathlib import Path


def default_data_dir() -> Path:
    """CodeInsightの解析結果を保存する既定のデータディレクトリ。

    対象リポジトリを変更しないため、対象リポジトリの外部
    （ユーザーのホームディレクトリ配下）に保存する。
    環境変数 CODEINSIGHT_DATA_DIR で上書き可能。
    """

    override = os.environ.get("CODEINSIGHT_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".codeinsight"


def default_db_path() -> Path:
    """複数プロジェクトを横断して管理する既定のSQLiteデータベースパス。"""

    return default_data_dir() / "codeinsight.db"

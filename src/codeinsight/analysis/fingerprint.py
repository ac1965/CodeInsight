from __future__ import annotations

import hashlib
from pathlib import Path


def analyzer_fingerprint() -> str:
    """解析ロジック（analysisパッケージのソース）の指紋。

    保存済みの解析結果が「現在の解析ロジックで得られたもの」かを判定するために用いる。
    解析器やリゾルバーのコードを変更すると値が変わり、変更のないファイルでも
    次回の解析で再解析される（古い抽出結果を最新の事実として残さない）。
    """

    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:8]

from __future__ import annotations

import re

_TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")


def is_test_path(relative_path: str) -> bool:
    """テストファイルのパスか（tests/ 配下、test_*.py、*_test.py、conftest.py）。名前による判定。"""

    return _TEST_PATH.search(relative_path) is not None

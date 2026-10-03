"""コマンドラインインターフェース。コマンドは責務ごとのモジュールに分かれている。"""

from codeinsight.cli.common import CliError, safe
from codeinsight.cli.parser import build_parser, main

__all__ = ["CliError", "build_parser", "main", "safe"]

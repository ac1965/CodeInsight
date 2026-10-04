from __future__ import annotations

from pathlib import Path

from codeinsight.analysis import CAnalyzer, CppAnalyzer, ElispAnalyzer, PythonAnalyzer, SymbolExtractor
from codeinsight.domain import Language


def build_symbol_extractor(compile_commands_dir: Path | None = None) -> SymbolExtractor:
    """標準の解析アダプター（C/Python/Emacs Lisp）を束ねたSymbolExtractorを組み立てる。

    CLI・将来のGUIなど、複数のプレゼンテーションから共通で使う組み立て処理。
    """

    return SymbolExtractor(
        {
            Language.C: CAnalyzer(compile_commands_dir=compile_commands_dir),
            Language.CPP: CppAnalyzer(compile_commands_dir=compile_commands_dir),
            Language.PYTHON: PythonAnalyzer(),
            Language.ELISP: ElispAnalyzer(),
        }
    )

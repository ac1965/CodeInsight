from __future__ import annotations

from pathlib import Path

from codeinsight.analysis.language_adapter import FileAnalysis, LanguageAdapter
from codeinsight.domain import Language


class UnsupportedLanguageError(Exception):
    """対応言語アダプターが登録されていない場合に送出する。"""


class SymbolExtractor:
    """言語ごとの解析アダプターを束ね、言語識別に応じて呼び分ける調整役。

    決定論的な解析（各アダプター）とその呼び分けのみを担当し、
    解析結果に対する解釈や要約はAI側（Phase4）の責務とする。
    """

    def __init__(self, adapters: dict[Language, LanguageAdapter]) -> None:
        self._adapters = adapters

    def extract(self, language: Language, file_id: str, absolute_path: Path) -> FileAnalysis:
        adapter = self._adapters.get(language)
        if adapter is None:
            raise UnsupportedLanguageError(f"未対応の言語です: {language}")
        return adapter.analyze_file(file_id, absolute_path)

    def supports(self, language: Language) -> bool:
        return language in self._adapters

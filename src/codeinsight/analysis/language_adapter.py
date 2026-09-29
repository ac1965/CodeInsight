from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from codeinsight.domain import Language, Symbol


@dataclass
class FileAnalysis:
    """1ファイルに対する解析アダプターの出力。

    errors が空でない場合、そのファイルの解析は失敗として扱い、
    正常に解析できたものとして symbols を確定情報にはしない
    （AGENTS.md 10.6: 解析失敗を正常終了として扱わない）。
    """

    symbols: list[Symbol] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.errors


class LanguageAdapter(Protocol):
    """言語別解析アダプターの共通インターフェース。

    シンボル抽出などの決定論的な解析はここに実装し、AIには一切委ねない
    （AGENTS.md 1.1「静的解析とAIの責務の分離」）。
    """

    language: Language

    def analyze_file(self, file_id: str, absolute_path: Path) -> FileAnalysis:
        """1ファイルを解析し、確認できたシンボルとエラー・警告を返す。"""
        ...

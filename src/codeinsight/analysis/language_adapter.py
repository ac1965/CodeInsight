from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from codeinsight.domain import Dependency, Language, Reference, Symbol


@dataclass(frozen=True)
class SourceUnit:
    """解析器に渡す1ファイル分の入力。

    content は解析時点で一度だけ読み込んだバイト列であり、ハッシュ計算と
    構文解析の双方に同じ内容を用いる（読み込み間の変更による不整合を防ぐ）。
    """

    file_id: str
    absolute_path: Path
    relative_path: str
    content: bytes

    @classmethod
    def from_path(
        cls, file_id: str, absolute_path: Path, relative_path: str | None = None
    ) -> SourceUnit:
        return cls(
            file_id=file_id,
            absolute_path=absolute_path,
            relative_path=relative_path or absolute_path.name,
            content=absolute_path.read_bytes(),
        )


@dataclass
class FileAnalysis:
    """1ファイルに対する解析アダプターの出力。

    errors が空でない場合、そのファイルの解析は失敗として扱い、
    正常に解析できたものとして内容を確定情報にはしない
    （AGENTS.md 10.6: 解析失敗を正常終了として扱わない）。

    references / dependencies の参照先は、この段階ではファイル内で分かる範囲の
    照合キーのみを持つ。プロジェクト横断の解決は ReferenceResolver が担当する。
    """

    symbols: list[Symbol] = field(default_factory=list)
    references: list[Reference] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.errors


class LanguageAdapter(Protocol):
    """言語別解析アダプターの共通インターフェース。

    シンボル・参照の抽出などの決定論的な解析はここに実装し、AIには一切委ねない
    （AGENTS.md 1.1「静的解析とAIの責務の分離」）。
    """

    language: Language

    def analyze_file(self, unit: SourceUnit) -> FileAnalysis:
        """1ファイルを解析し、確認できた事実とエラー・警告を返す。"""
        ...

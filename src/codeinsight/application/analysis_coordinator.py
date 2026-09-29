from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from codeinsight.analysis.language import detect_language
from codeinsight.analysis.symbol_extractor import SymbolExtractor, UnsupportedLanguageError
from codeinsight.domain import (
    AnalysisFileStatus,
    AnalysisResult,
    AnalysisStatus,
    Language,
    Project,
    SourceFile,
)
from codeinsight.infrastructure.analysis_repository import AnalysisRepository
from codeinsight.infrastructure.file_scanner import FileScanner

_ANALYZER_VERSION = "0.1.0"


class AnalysisCoordinator:
    """プロジェクトの走査・解析・保存という一連のユースケースを実行する。

    ファイルのcontent_hashが前回解析時から変わっていなければ再解析を
    スキップする（3.10: 必要な範囲だけの再解析の基礎）。解析エラーは
    握りつぶさず AnalysisResult.errors に記録する（禁止事項10.6）。
    """

    def __init__(
        self,
        repository: AnalysisRepository,
        symbol_extractor: SymbolExtractor,
        file_scanner: FileScanner | None = None,
    ) -> None:
        self._repository = repository
        self._symbol_extractor = symbol_extractor
        self._file_scanner = file_scanner or FileScanner()

    def analyze_project(self, project: Project) -> AnalysisResult:
        scanned_files = self._file_scanner.scan(project)
        warnings: list[str] = []
        errors: list[str] = []
        analyzed_count = 0

        for scanned in scanned_files:
            language = detect_language(scanned.absolute_path)
            if language == Language.UNKNOWN or not self._symbol_extractor.supports(language):
                continue

            existing = self._repository.get_source_file_by_path(
                project.project_id, scanned.relative_path
            )
            file_id = existing.file_id if existing else str(uuid.uuid4())
            content_hash = self._hash_file(scanned.absolute_path)

            if (
                existing is not None
                and existing.content_hash == content_hash
                and existing.analysis_status == AnalysisFileStatus.ANALYZED
            ):
                analyzed_count += 1
                continue

            try:
                file_analysis = self._symbol_extractor.extract(
                    language, file_id, scanned.absolute_path
                )
            except UnsupportedLanguageError as exc:
                errors.append(f"{scanned.relative_path}: {exc}")
                continue

            for warning in file_analysis.warnings:
                warnings.append(f"{scanned.relative_path}: {warning}")

            status = (
                AnalysisFileStatus.ANALYZED
                if file_analysis.succeeded
                else AnalysisFileStatus.FAILED
            )
            if file_analysis.succeeded:
                analyzed_count += 1
            else:
                for error in file_analysis.errors:
                    errors.append(f"{scanned.relative_path}: {error}")

            # symbolsテーブルはsource_filesへの外部キーを持つため、
            # 先にsource_fileを保存してから洗い替えを行う。
            source_file = SourceFile(
                file_id=file_id,
                project_id=project.project_id,
                relative_path=scanned.relative_path,
                language=language,
                content_hash=content_hash,
                last_analyzed_at=datetime.now(timezone.utc),
                analysis_status=status,
            )
            self._repository.save_source_file(source_file)

            if file_analysis.succeeded:
                self._repository.replace_symbols_for_file(file_id, file_analysis.symbols)

        if errors:
            overall_status = (
                AnalysisStatus.PARTIAL if analyzed_count > 0 else AnalysisStatus.FAILED
            )
        else:
            overall_status = AnalysisStatus.SUCCESS

        result = AnalysisResult(
            analysis_id=str(uuid.uuid4()),
            project_id=project.project_id,
            analyzer_version=_ANALYZER_VERSION,
            analysis_timestamp=datetime.now(timezone.utc),
            status=overall_status,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )
        self._repository.save_analysis_result(result)
        return result

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        digest.update(path.read_bytes())
        return digest.hexdigest()

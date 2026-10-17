from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from codeinsight import __version__
from codeinsight.analysis.fingerprint import analyzer_fingerprint
from codeinsight.analysis.language import detect_language
from codeinsight.analysis.language_adapter import SourceUnit
from codeinsight.analysis.reference_resolver import ReferenceResolver
from codeinsight.analysis.symbol_extractor import SymbolExtractor, UnsupportedLanguageError
from codeinsight.domain import (
    AnalysisFileStatus,
    AnalysisResult,
    AnalysisStatus,
    DependencyKind,
    Language,
    Project,
    SourceFile,
)
from codeinsight.infrastructure.analysis_repository import AnalysisRepository
from codeinsight.infrastructure.file_scanner import FileScanner, ScannedFile
from codeinsight.infrastructure.git_repository import GitRepository

# パッケージのバージョンに、解析ロジックの指紋を付けて「解析器のバージョン」とする。
ANALYZER_VERSION = f"{__version__}+{analyzer_fingerprint()}"


@dataclass(frozen=True)
class AnalysisProgress:
    """解析の進捗通知（長時間処理の進捗表示用）。"""

    done: int
    total: int
    relative_path: str


ProgressCallback = Callable[[AnalysisProgress], None]


@dataclass
class AnalysisStats:
    """直近の解析で、どのファイルを解析し直したか（差分解析の内訳）。保存しない。"""

    changed: int = 0  # 新規・内容の変更・前回失敗・解析器の変更で、解析し直したファイル
    dependents: int = 0  # 自身は変わっていないが、include しているヘッダーが変わった（削除された）ため、解析し直したファイル
    skipped: int = 0  # 変更なしで、前回の結果を使ったファイル
    removed: int = 0
    build_config_changed: bool = False  # compile_commands.json の内容が前回から変わったため、C/C++ をすべて解析し直した
    rebuilt_for_config: int = 0
    dependent_paths: list[str] = field(default_factory=list)


def include_dependents(dependencies, seeds: set[str]) -> set[str]:
    """`seeds`（変更・削除されたファイル）を、直接・間接に include しているファイルの file_id。

    Cのファイルの解析結果は、include しているヘッダーのマクロ・型・条件付きコンパイルに左右される。ファイル自身の内容が同じでも、
    ヘッダーが変われば結果が変わりうるため、解析し直す対象にする。推移的にたどる（A が B を、B が C を include していれば、C の変更は A に及ぶ）。
    """

    includers: dict[str, set[str]] = {}
    for dependency in dependencies:
        if dependency.dependency_kind == DependencyKind.INCLUDE and dependency.target_file_id:
            includers.setdefault(dependency.target_file_id, set()).add(dependency.source_file_id)
    found: set[str] = set()
    queue = list(seeds)
    while queue:
        for includer in includers.get(queue.pop(), ()):
            if includer not in found and includer not in seeds:
                found.add(includer)
                queue.append(includer)
    return found


def derive_file_id(project_id: str, relative_path: str) -> str:
    """プロジェクトと相対パスから決定的にfile_idを作る（再登録しても同じIDになる）。"""

    digest = hashlib.sha256(f"{project_id}\x00{relative_path}".encode())
    return digest.hexdigest()[:32]


class AnalysisCoordinator:
    """プロジェクトの走査・解析・解決・保存という一連のユースケースを実行する。

    * ファイルの内容ハッシュと解析器バージョンが前回から変わっていなければ
      再解析をスキップする（3.10: 必要な範囲だけの再解析の基礎）。
    * 1ファイルの失敗（読み込み不能・構文エラー・解析器の例外）は、そのファイルの
      エラーとして記録して処理を継続する（禁止事項10.6）。失敗したファイルの古い
      解析結果は残さない。
    * 存在しなくなったファイルの解析結果は削除する。
    * 全体を1トランザクションで保存し、途中で異常終了しても中途半端な状態を残さない。
    * 最後にプロジェクト全体で参照・依存関係を解決する（ReferenceResolver）。
    """

    def __init__(
        self,
        repository: AnalysisRepository,
        symbol_extractor: SymbolExtractor,
        file_scanner: FileScanner | None = None,
        git_repository: GitRepository | None = None,
        resolver: ReferenceResolver | None = None,
    ) -> None:
        self._repository = repository
        self._symbol_extractor = symbol_extractor
        self._file_scanner = file_scanner or FileScanner()
        self._git_repository = git_repository or GitRepository()
        self._resolver = resolver or ReferenceResolver()
        self.last_stats = AnalysisStats()

    def analyze_project(
        self, project: Project, progress: ProgressCallback | None = None
    ) -> AnalysisResult:
        project.repository_revision = self._git_repository.current_revision(project.root_path)
        candidates = self._candidates(project)
        warnings_by_message: dict[str, list[str]] = {}
        errors: list[str] = []
        analyzed_count = 0

        with self._repository.transaction():
            self._repository.save_project(project)
            existing_files = {
                f.relative_path: f for f in self._repository.list_source_files(project.project_id)
            }
            current_paths = {scanned.relative_path for scanned, _ in candidates}
            stats = AnalysisStats(removed=sum(1 for path in existing_files if path not in current_paths))
            hashes = {scanned.relative_path: _file_hash(scanned.absolute_path) for scanned, _ in candidates}
            force = self._dependents_to_reanalyze(project, existing_files, hashes, current_paths)
            build_hash = _compile_commands_hash(project)
            previous_hash = project.configuration.compile_commands_hash
            config_force: set[str] = set()
            if (previous_hash or "") != build_hash:
                # ビルド設定（-D・-I・規格）が変わると、ファイル自身が同じでも C/C++ の解析結果が変わりうる
                stats.build_config_changed = True
                config_force = {s.relative_path for s, lang in candidates if lang in (Language.C, Language.CPP)}
                force |= config_force
            self._repository.delete_source_files(
                f.file_id for path, f in existing_files.items() if path not in current_paths
            )

            for index, (scanned, language) in enumerate(candidates, start=1):
                path = scanned.relative_path
                was_current = self._is_current(existing_files.get(path), hashes.get(path))
                succeeded = self._analyze_one(
                    project,
                    scanned,
                    language,
                    existing_files.get(scanned.relative_path),
                    warnings_by_message,
                    errors,
                    force=path in force,
                    known_hash=hashes.get(path),
                )
                if was_current and path not in force:
                    stats.skipped += 1
                elif was_current and path in config_force:
                    stats.rebuilt_for_config += 1
                elif was_current:
                    stats.dependents += 1
                    stats.dependent_paths.append(path)
                else:
                    stats.changed += 1
                if succeeded:
                    analyzed_count += 1
                if progress is not None:
                    progress(AnalysisProgress(index, len(candidates), scanned.relative_path))

            self._resolve_project(project)
            self.last_stats = stats
            if build_hash != (project.configuration.compile_commands_hash or ""):
                project.configuration = replace(project.configuration, compile_commands_hash=build_hash)
                self._repository.save_project(project)

            if errors:
                overall_status = (
                    AnalysisStatus.PARTIAL if analyzed_count > 0 else AnalysisStatus.FAILED
                )
            else:
                overall_status = AnalysisStatus.SUCCESS

            result = AnalysisResult(
                analysis_id=str(uuid.uuid4()),
                project_id=project.project_id,
                analyzer_version=ANALYZER_VERSION,
                analysis_timestamp=datetime.now(UTC),
                status=overall_status,
                warnings=tuple(_format_warnings(warnings_by_message)),
                errors=tuple(errors),
                repository_revision=project.repository_revision,
            )
            self._repository.save_analysis_result(result)
        return result

    def _candidates(self, project: Project) -> list[tuple[ScannedFile, Language]]:
        candidates: list[tuple[ScannedFile, Language]] = []
        for scanned in self._file_scanner.scan(project):
            language = detect_language(scanned.absolute_path)
            if language == Language.UNKNOWN or not self._symbol_extractor.supports(language):
                continue
            candidates.append((scanned, language))
        return candidates

    def _analyze_one(
        self,
        project: Project,
        scanned: ScannedFile,
        language: Language,
        existing: SourceFile | None,
        warnings_by_message: dict[str, list[str]],
        errors: list[str],
        force: bool = False,
        known_hash: str | None = None,
    ) -> bool:
        """1ファイルを解析して保存する。解析に成功した（またはスキップした）場合はTrue。

        `force` は、ファイル自身は変わっていないが、include しているヘッダーが変わったため、解析し直す指定。
        """

        file_id = existing.file_id if existing else derive_file_id(
            project.project_id, scanned.relative_path
        )
        if not force and self._is_current(existing, known_hash):
            return True  # 変更なし（ファイルを読み直さない）

        try:
            content = scanned.absolute_path.read_bytes()
        except OSError as exc:
            errors.append(f"{scanned.relative_path}: ファイルを読み込めません: {exc}")
            self._save_failed(project, scanned, language, file_id, "")
            return False

        content_hash = hashlib.sha256(content).hexdigest()
        if not force and self._is_current(existing, content_hash):
            return True

        unit = SourceUnit(file_id, scanned.absolute_path, scanned.relative_path, content)
        try:
            file_analysis = self._symbol_extractor.extract(language, unit)
        except UnsupportedLanguageError as exc:
            errors.append(f"{scanned.relative_path}: {exc}")
            return False
        except Exception as exc:  # 解析器の予期しない失敗を、他ファイルへ波及させない
            errors.append(
                f"{scanned.relative_path}: 解析中に予期しないエラーが発生しました: "
                f"{type(exc).__name__}: {exc}"
            )
            self._save_failed(project, scanned, language, file_id, content_hash)
            return False

        for warning in file_analysis.warnings:
            warnings_by_message.setdefault(warning, []).append(scanned.relative_path)

        # symbols等はsource_filesへの外部キーを持つため、先にsource_fileを保存する。
        succeeded = file_analysis.succeeded
        self._repository.save_source_file(
            SourceFile(
                file_id=file_id,
                project_id=project.project_id,
                relative_path=scanned.relative_path,
                language=language,
                content_hash=content_hash,
                last_analyzed_at=datetime.now(UTC),
                analysis_status=(
                    AnalysisFileStatus.ANALYZED if succeeded else AnalysisFileStatus.FAILED
                ),
                analyzer_version=ANALYZER_VERSION,
            )
        )
        if succeeded:
            self._repository.replace_file_facts(
                file_id,
                file_analysis.symbols,
                file_analysis.references,
                file_analysis.dependencies,
            )
        else:
            # 失敗したファイルの古いシンボルを、最新の事実として残さない。
            self._repository.clear_file_facts(file_id)
            for error in file_analysis.errors:
                errors.append(f"{scanned.relative_path}: {error}")
        return succeeded

    @staticmethod
    def _is_current(existing: SourceFile | None, content_hash: str | None) -> bool:
        """前回の解析結果が、今のファイルの内容と解析器に対して有効か。"""

        return (
            existing is not None
            and content_hash is not None
            and existing.content_hash == content_hash
            and existing.analysis_status == AnalysisFileStatus.ANALYZED
            and existing.analyzer_version == ANALYZER_VERSION
        )

    def _dependents_to_reanalyze(
        self, project: Project, existing_files: dict[str, SourceFile], hashes: dict[str, str | None], current_paths: set[str]
    ) -> set[str]:
        """変更・削除されたファイルを include しているファイルのうち、自身は変わっていないもの（相対パス）。"""

        by_id = {f.file_id: path for path, f in existing_files.items()}
        seeds = {
            f.file_id for path, f in existing_files.items()
            if path not in current_paths or (path in hashes and not self._is_current(f, hashes[path]))
        }
        if not seeds:
            return set()
        dependents = include_dependents(self._repository.list_dependencies_for_project(project.project_id), seeds)
        return {by_id[file_id] for file_id in dependents if by_id.get(file_id) in current_paths}

    def _save_failed(
        self,
        project: Project,
        scanned: ScannedFile,
        language: Language,
        file_id: str,
        content_hash: str,
    ) -> None:
        self._repository.save_source_file(
            SourceFile(
                file_id=file_id,
                project_id=project.project_id,
                relative_path=scanned.relative_path,
                language=language,
                content_hash=content_hash,
                last_analyzed_at=datetime.now(UTC),
                analysis_status=AnalysisFileStatus.FAILED,
                analyzer_version=ANALYZER_VERSION,
            )
        )
        self._repository.clear_file_facts(file_id)

    def _resolve_project(self, project: Project) -> None:
        files = self._repository.list_source_files(project.project_id)
        symbols = self._repository.list_symbols_for_project(project.project_id)
        references = self._repository.list_references_for_project(project.project_id)
        dependencies = self._repository.list_dependencies_for_project(project.project_id)
        before_references = {r.reference_id: _resolution_state(r) for r in references}
        before_dependencies = {d.dependency_id: _resolution_state(d) for d in dependencies}
        self._resolver.resolve(files, symbols, references, dependencies)
        # 変わらなかった行は書き込まない（変更のない再解析で全行を更新しない）
        self._repository.update_resolutions(
            [r for r in references if _resolution_state(r) != before_references[r.reference_id]],
            [d for d in dependencies if _resolution_state(d) != before_dependencies[d.dependency_id]],
        )


def _compile_commands_hash(project: Project) -> str:
    """解析に使う compile_commands.json の内容のハッシュ（無ければ空文字）。"""

    directory = project.configuration.compile_commands_dir
    if not directory:
        return ""
    try:
        return hashlib.sha256((Path(directory) / "compile_commands.json").read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _file_hash(path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _resolution_state(item: object) -> tuple:
    target = getattr(item, "target_symbol_id", None) or getattr(item, "target_file_id", None)
    return (target, item.resolution_status, item.confidence, item.note)  # type: ignore[attr-defined]


def _format_warnings(warnings_by_message: dict[str, list[str]]) -> list[str]:
    """同じ警告が多数のファイルで出る場合は、1件にまとめてノイズを減らす。"""

    formatted: list[str] = []
    for message, paths in warnings_by_message.items():
        if len(paths) == 1:
            formatted.append(f"{paths[0]}: {message}")
        else:
            shown = ", ".join(paths[:3])
            more = f" ほか{len(paths) - 3}件" if len(paths) > 3 else ""
            formatted.append(f"{message} (対象 {len(paths)} ファイル: {shown}{more})")
    return formatted

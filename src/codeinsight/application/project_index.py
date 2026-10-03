from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from codeinsight.domain import Dependency, Reference, SourceFile, Symbol
from codeinsight.infrastructure.analysis_repository import AnalysisRepository


@dataclass
class ProjectIndex:
    """1プロジェクト分の解析結果を、検索・ナビゲーション用にメモリへ読み込んだもの。

    各サービスが同じ読み込み・索引化の処理を重複して持たないための共通部品。
    保存済みの解析結果のスナップショットであり、ソースが変更されても更新されない
    （古さは FreshnessService で判定する）。

    件数が多い参照・依存関係は、初めて使われるまで読み込まない（定義の検索や構造表示
    のように、参照を使わないコマンドで読み込みのコストを払わないため）。リポジトリを閉じた
    後に使う場合は、先に materialize() を呼ぶこと。
    """

    project_id: str
    files: dict[str, SourceFile]
    symbols: dict[str, Symbol]
    _load_references: Callable[[], list[Reference]] = field(repr=False)
    _load_dependencies: Callable[[], list[Dependency]] = field(repr=False)
    _paths: dict[str, SourceFile] = field(default_factory=dict, repr=False)
    _references: list[Reference] | None = field(default=None, repr=False)
    _dependencies: list[Dependency] | None = field(default=None, repr=False)

    @classmethod
    def load(cls, repository: AnalysisRepository, project_id: str) -> ProjectIndex:
        files = {f.file_id: f for f in repository.list_source_files(project_id)}
        return cls(
            project_id=project_id,
            files=files,
            symbols={s.symbol_id: s for s in repository.list_symbols_for_project(project_id)},
            _load_references=lambda: repository.list_references_for_project(project_id),
            _load_dependencies=lambda: [
                d for d in repository.list_dependencies_for_project(project_id) if d.is_visible
            ],
            _paths={f.relative_path: f for f in files.values()},
        )

    def materialize(self) -> ProjectIndex:
        """参照・依存関係を今すぐ読み込む。リポジトリを閉じた後も索引を使う場合に呼ぶ。"""

        _ = self.references, self.dependencies
        return self

    @property
    def references(self) -> list[Reference]:
        if self._references is None:
            self._references = self._load_references()
        return self._references

    @property
    def dependencies(self) -> list[Dependency]:
        if self._dependencies is None:
            self._dependencies = self._load_dependencies()
        return self._dependencies

    def excluding(self, patterns: Iterable[str]) -> ProjectIndex:
        """パスが除外パターン（fnmatch形式、例: ``tests/*``）に一致するファイルを取り除いた索引。

        表示を絞り込むための操作で、保存済みの解析結果は変更しない。
        """

        globs = list(patterns)
        if not globs:
            return self
        return self._without(
            {
                fid
                for fid, f in self.files.items()
                if any(fnmatch.fnmatch(f.relative_path, g) for g in globs)
            }
        )

    def under(self, prefix: str) -> ProjectIndex:
        """指定のファイルまたはディレクトリ（相対パス）の配下だけに絞った索引。"""

        prefix = prefix.rstrip("/")
        return self._without(
            {
                fid
                for fid, f in self.files.items()
                if f.relative_path != prefix and not f.relative_path.startswith(prefix + "/")
            }
        )

    def _without(self, dropped: set[str]) -> ProjectIndex:
        """指定ファイルのシンボルを取り除く。それらのファイル内の参照・依存関係、およびそれらの
        シンボルを指す参照・依存関係も取り除く。"""

        files = {fid: f for fid, f in self.files.items() if fid not in dropped}
        symbols = {sid: s for sid, s in self.symbols.items() if s.file_id not in dropped}
        return ProjectIndex(
            project_id=self.project_id,
            files=files,
            symbols=symbols,
            _load_references=lambda: [
                r
                for r in self.references
                if r.source_location.file_id not in dropped
                and (r.target_symbol_id is None or r.target_symbol_id in symbols)
            ],
            _load_dependencies=lambda: [
                d
                for d in self.dependencies
                if d.source_file_id not in dropped
                and (d.target_file_id is None or d.target_file_id in files)
            ],
            _paths={f.relative_path: f for f in files.values()},
        )

    def path_of(self, file_id: str) -> str:
        source_file = self.files.get(file_id)
        return source_file.relative_path if source_file else "<不明>"

    def file_by_path(self, relative_path: str) -> SourceFile | None:
        return self._paths.get(relative_path)

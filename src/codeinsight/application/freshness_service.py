from __future__ import annotations

import hashlib

from codeinsight.domain import AnalysisFileStatus, FileFreshness, Project, SourceFile


class FreshnessService:
    """保存済み解析結果が現在のソースコードと一致するかを判定する（3.10）。

    解析時に記録した内容ハッシュと現在のファイルのハッシュを比較するだけで、
    ソースの変更や解析結果の更新は行わない。
    """

    def check_file(self, project: Project, source_file: SourceFile) -> FileFreshness:
        if source_file.analysis_status != AnalysisFileStatus.ANALYZED:
            return FileFreshness.UNANALYZED
        path = project.root_path / source_file.relative_path
        try:
            current = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return FileFreshness.MISSING
        if current == source_file.content_hash:
            return FileFreshness.FRESH
        return FileFreshness.STALE

    def check_project(
        self, project: Project, files: list[SourceFile]
    ) -> dict[str, FileFreshness]:
        return {f.file_id: self.check_file(project, f) for f in files}

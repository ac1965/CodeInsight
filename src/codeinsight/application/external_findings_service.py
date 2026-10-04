"""外部ツール（SARIF）の指摘の取り込みと、現在のソースとの対応づけ。

指摘は、CodeInsight自身の解析結果（事実）とは別に管理する。取り込みは、利用者が用意したSARIFファイルを
読むだけで、外部ツールの実行も、対象リポジトリの変更もしない。
"""

from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from codeinsight.domain import ExternalFinding, Project
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.infrastructure.sarif import SarifError, parse_sarif, to_relative_path


@dataclass
class ImportReport:
    imported: int = 0
    tools: list[str] = field(default_factory=list)
    without_location: int = 0  # 位置を持たない
    outside_project: int = 0  # プロジェクトのルートの外、または解釈できないURI
    missing_files: int = 0  # ルート内に存在しないファイル
    by_level: dict[str, int] = field(default_factory=dict)


class ExternalFindingsService:
    def __init__(self, repository: AnalysisRepository) -> None:
        self._repository = repository

    def import_sarif(self, project: Project, sarif_path: Path) -> ImportReport:
        try:
            data = sarif_path.read_bytes()
        except OSError as exc:
            raise SarifError(f"ファイルを読み込めません: {exc}") from exc
        document = parse_sarif(data)
        digest = hashlib.sha256(data).hexdigest()
        report = ImportReport(tools=document.tools, without_location=document.without_location)
        now = datetime.now()
        hashes: dict[str, str] = {}
        findings: list[ExternalFinding] = []
        for result in document.results:
            relative = to_relative_path(result.uri, project.root_path)
            if relative is None:
                report.outside_project += 1
                continue
            if relative not in hashes:
                try:
                    hashes[relative] = hashlib.sha256((project.root_path / relative).read_bytes()).hexdigest()
                except OSError:
                    hashes[relative] = ""
            if not hashes[relative]:
                report.missing_files += 1
                continue
            findings.append(
                ExternalFinding(
                    uuid.uuid4().hex, project.project_id, result.tool, result.tool_version, result.rule_id, result.level, result.message,
                    relative, result.start_line, result.end_line, hashes[relative], now, sarif_path.name[:120], digest,
                )
            )
        self._repository.replace_external_findings(project.project_id, digest, findings)
        report.imported = len(findings)
        report.by_level = dict(Counter(f.level for f in findings))
        return report

    def with_staleness(self, project: Project) -> list[tuple[ExternalFinding, bool]]:
        """(指摘, 古いか)。古い = 取り込み時点から、対象ファイルの内容が変わった（または無くなった）。"""

        current: dict[str, str] = {}
        result = []
        for finding in self._repository.list_external_findings(project.project_id):
            if finding.path not in current:
                try:
                    current[finding.path] = hashlib.sha256((project.root_path / finding.path).read_bytes()).hexdigest()
                except OSError:
                    current[finding.path] = ""
            result.append((finding, current[finding.path] != finding.content_hash))
        return result

    def clear(self, project: Project, tool: str | None = None) -> int:
        return self._repository.delete_external_findings(project.project_id, tool)

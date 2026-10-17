"""C/C++ の設定値・入口と境界の走査（`ConfigService`・`BoundaryService` から使う）。

対象のC/C++ファイルのうち、関係する名前が現れるものだけを構文解析する（それ以外は読み取りだけ）。解析後に変更されたファイル・
構文解析できないファイルは、調べられなかったものとして報告する。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from codeinsight.analysis import c_config_scan as scan
from codeinsight.application.c_flow_service import C_FAMILY, CFlowService
from codeinsight.application.flow_service import FlowAnalysisError
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Project

_RELEVANT = re.compile(
    rb"\b(getenv|secure_getenv|getenv_s|setenv|putenv|unsetenv|getopt|getopt_long|getopt_long_only|argv|fopen|freopen|open|openat|access|stat|"
    rb"signal|sigaction|sa_handler|sa_sigaction|atexit|on_exit|at_quick_exit|pthread_atfork|pthread_create|thrd_create|socket|bind|listen|accept|accept4|connect|fork|vfork)\b"
)


@dataclass
class CFileScan:
    path: str
    result: scan.CScanResult
    constants: list[scan.CConfigHit]

    def owner(self, line: int) -> str:
        inside = [(end - start, name) for name, (start, end) in self.result.functions.items() if start <= line <= end]
        return min(inside)[1] if inside else ""


class CScanService:
    def __init__(self) -> None:
        self._flow = CFlowService(None)

    def scan(self, project: Project, index: ProjectIndex, only_paths: set[str] | None = None, contains: str | None = None) -> tuple[list[CFileScan], list[str]]:
        scans: list[CFileScan] = []
        skipped: list[str] = []
        needle = contains.encode("utf-8") if contains else None
        for file_id, source_file in sorted(index.files.items(), key=lambda item: item[1].relative_path):
            if source_file.language not in C_FAMILY or (only_paths is not None and source_file.relative_path not in only_paths):
                continue
            try:
                data = (project.root_path / source_file.relative_path).read_bytes()
            except OSError:
                skipped.append(f"{source_file.relative_path}（読み込めない）")
                continue
            if needle is not None and needle not in data:
                continue
            constants = scan.scan_constants(data)
            result = scan.CScanResult()
            if _RELEVANT.search(data):
                try:
                    translation_unit, source, absolute = self._flow._parse_file(project, source_file, file_id)
                    result = scan.scan_c_file(translation_unit, absolute, source)
                except (FlowAnalysisError, ValueError) as exc:
                    skipped.append(f"{source_file.relative_path}（{str(exc).split('。')[0][:60]}）")
            if constants or result.config or result.boundaries:
                scans.append(CFileScan(source_file.relative_path, result, constants))
        return scans, skipped

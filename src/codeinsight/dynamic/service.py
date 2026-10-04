"""動的解析のユースケース。`plan` は何も実行しない。`run` は、許可を確認したうえで、コンテナの中で実行して観測を保存する。

観測は、静的解析の事実とは別に保存する（DYNAMIC_ANALYSIS.md）。実行の前後で、対象のファイルのハッシュを比べ、
変わっていれば TARGET_MODIFIED として記録する（対象を変更しない原則）。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Project
from codeinsight.domain.observation import DynamicRun, Observation, RunStatus
from codeinsight.dynamic import executor
from codeinsight.dynamic.collectors import CollectorSpec, collectors_for
from codeinsight.dynamic.observations import SymbolMatcher, to_observations
from codeinsight.dynamic.permission import DynamicPermission
from codeinsight.dynamic.sandbox import BACKENDS, SandboxPolicy
from codeinsight.infrastructure.analysis_repository import AnalysisRepository

COLLECTOR_NAME = "python-monitoring"
COLLECTOR_VERSION = "1"

Executor = Callable[[executor.PermittedRun], executor.ExecutionResult]


@dataclass
class DynamicPlan:
    """実行したらどうなるかの計画。**これを作るときも、何も実行しない。**"""

    project: str
    permission: dict
    permitted: bool
    denied_reasons: list[str]
    sandbox: dict
    sandbox_available: bool | None  # 隔離の仕組みが、この計算機で利用できるか（None: 隔離なし/不明）
    collectors: list[CollectorSpec] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class DynamicRunOutcome:
    run: DynamicRun
    observations: list[Observation]
    stdout_tail: str = ""
    stderr_tail: str = ""


def hash_files(root: Path, paths: list[str]) -> dict[str, str]:
    """対象のファイルの現在のハッシュ（読めなければ空文字）。実行の前後の比較と、古い観測の判定に使う。"""

    hashes: dict[str, str] = {}
    for rel in paths:
        try:
            hashes[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        except OSError:
            hashes[rel] = ""
    return hashes


class DynamicAnalysisService:
    def plan(self, project: Project, permission: DynamicPermission, languages: set[str]) -> DynamicPlan:
        policy = SandboxPolicy.from_permission(permission)
        backend = BACKENDS.get(permission.sandbox_backend)
        reasons = permission.violations()
        notes = ["この計画は表示のみで、対象のプログラムは実行していません。"]
        if permission.sandbox_backend == "none":
            notes.append("隔離なしの実行は、対象の環境に影響する恐れがあります（設計では既定で不可。現在の実行機能は対応していません）。")
        elif permission.sandbox_backend != "container":
            notes.append("現在の実行機能が対応しているのは、コンテナ（container）だけです。")
        if permission.allow_network:
            notes.append("ネットワークが許可されています。実行が外部へ通信する可能性があります。")
        if any(c.needs_rebuild for c in collectors_for(languages)):
            notes.append("Cの収集器には再コンパイルを伴うものがあります（未実装。対象の外の作業領域で行い、ビルドの許可とは別に実行の許可も必要）。")
        notes.append("実行できる収集器は、Python（python-monitoring）だけです。")
        return DynamicPlan(
            project.name, permission.summary(), not reasons, reasons, policy.describe(),
            backend.available() if backend else None, collectors_for(languages), notes,
        )

    def run(
        self,
        project: Project,
        permission: DynamicPermission,
        index: ProjectIndex,
        repository: AnalysisRepository | None = None,
        image: str = executor.DEFAULT_IMAGE,
        run_executor: Executor | None = None,
    ) -> DynamicRunOutcome:
        """許可を確認し（揃っていなければ例外）、コンテナで実行して、観測を保存する。`run_executor` はテスト用の差し替え。"""

        permitted = executor.PermittedRun.create(permission, project.root_path, image)  # 許可の確認はここで行う
        python_paths = sorted(f.relative_path for f in index.files.values() if f.language.value == "python")
        root = project.root_path.resolve()
        before = hash_files(root, python_paths)
        started = datetime.now()
        result = (run_executor or executor.execute)(permitted)
        after = hash_files(root, python_paths)
        run_id = uuid.uuid4().hex[:16]
        notes: list[str] = []
        if after != before:
            status = RunStatus.TARGET_MODIFIED
            notes.append("実行の前後で、対象のファイルの内容が変わっていました（対象を変更しない原則に反します。観測は信頼しないでください）。")
        elif result.timed_out:
            status = RunStatus.TIMEOUT
            notes.append(f"時間の上限（{permission.timeout_seconds:.0f}秒）を超えたため、コンテナを止めました。観測は得られていません。")
        elif result.observation is None:
            status = RunStatus.FAILED
            notes.append("収集器の出力が得られませんでした（対象が収集器の起動前に失敗した、または収集器が異常終了した）。")
        else:
            status = RunStatus.COMPLETED if result.observation.get("exit_code") == 0 else RunStatus.FAILED
        observations: list[Observation] = []
        if result.observation is not None and status != RunStatus.TARGET_MODIFIED:
            observations = to_observations(run_id, result.observation, SymbolMatcher(index))
            if result.observation.get("dropped_edges"):
                notes.append(f"呼び出しの辺が上限を超えたため、{result.observation['dropped_edges']}件を記録していません。")
        exit_code = result.observation.get("exit_code") if result.observation else result.returncode
        mechanism = result.observation.get("mechanism", "") if result.observation else ""
        run = DynamicRun(
            run_id, project.project_id, started, permission.command, permission.summary(),
            SandboxPolicy.from_permission(permission).describe() | {"image": image, "docker": result.docker_argv}, COLLECTOR_NAME,
            f"{COLLECTOR_VERSION}/{mechanism}" if mechanism else COLLECTOR_VERSION, status, project.repository_revision, before,
            exit_code, round(result.duration_seconds, 3), notes,
        )
        if repository is not None:
            repository.save_dynamic_run(run, observations)
        return DynamicRunOutcome(run, observations, result.stdout_tail, result.stderr_tail)

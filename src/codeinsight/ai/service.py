from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from codeinsight.ai.citations import CitationValidator, ValidationReport
from codeinsight.ai.config import AIConfig
from codeinsight.ai.context import Context, ContextBuilder
from codeinsight.ai.prompt import PromptBuilder
from codeinsight.ai.provider import AIProvider, Completion, Message, OpenAICompatibleProvider
from codeinsight.application.analysis_coordinator import ANALYZER_VERSION
from codeinsight.application.freshness_service import FreshnessService
from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Explanation, FileFreshness, Project, Symbol
from codeinsight.infrastructure.analysis_repository import AnalysisRepository


@dataclass
class ExplanationResult:
    context: Context
    messages: list[Message]
    completion: Completion | None = None
    report: ValidationReport | None = None
    explanation: Explanation | None = None  # 保存した解説（--no-save では None）
    dry_run: bool = False


class ExplanationService:
    """AI解説の生成・検証・保存（AGENTS.md §3.8・§3.9）。

    * 解析結果（事実）を入力として解説を生成し、解析結果のテーブルとは別に保存する。
    * 送信前に、送信の許可（と、送信先が外部の場合は追加の許可）を確認する。許可が無ければ何も送信しない。
    * 回答の引用を検証し、根拠を確認できない部分を、検証済みの事実として扱わない。
    """

    def __init__(
        self,
        repository: AnalysisRepository,
        navigation: NavigationService,
        config: AIConfig,
        provider: AIProvider | None = None,
    ) -> None:
        self._repository = repository
        self._navigation = navigation
        self._config = config
        self._provider = provider

    def _builder(self) -> ContextBuilder:
        return ContextBuilder(self._navigation, self._config.max_context_chars, self._config.include_source)

    # --- 解説の種類 ---

    def explain_symbol(self, project: Project, index: ProjectIndex, symbol: Symbol, *, dry_run: bool = False, save: bool = True) -> ExplanationResult:
        context = self._builder().for_symbol(project, index, symbol)
        return self._run(project, index, context, None, dry_run, save)

    def explain_file(self, project: Project, index: ProjectIndex, relative_path: str, *, dry_run: bool = False, save: bool = True) -> ExplanationResult:
        context = self._builder().for_file(project, index, relative_path)
        return self._run(project, index, context, None, dry_run, save)

    def explain_path(self, project: Project, index: ProjectIndex, source: Symbol, target: Symbol, *, dry_run: bool = False, save: bool = True) -> ExplanationResult:
        context = self._builder().for_path(project, index, source, target)
        return self._run(project, index, context, None, dry_run, save)

    def ask(self, project: Project, index: ProjectIndex, question: str, *, dry_run: bool = False, save: bool = True) -> ExplanationResult:
        context = self._builder().for_question(project, index, question)
        return self._run(project, index, context, question, dry_run, save)

    # --- 共通の流れ ---

    def _run(self, project: Project, index: ProjectIndex, context: Context, question: str | None, dry_run: bool, save: bool) -> ExplanationResult:
        messages = PromptBuilder().build(context, question)
        if dry_run:
            return ExplanationResult(context, messages, dry_run=True)

        self._config.check_consent(require_model=self._provider is None)
        provider = self._provider or OpenAICompatibleProvider(
            self._config.base_url, self._config.model or "", self._config.api_key, self._config.timeout
        )
        completion = provider.complete(messages, temperature=self._config.temperature, max_tokens=self._config.max_tokens)
        report = CitationValidator(project, index, context).validate(completion.text)

        explanation = Explanation(
            explanation_id=uuid.uuid4().hex,
            project_id=project.project_id,
            target_kind=context.target_kind,
            target=context.target if context.target_kind != "question" else (question or context.target),
            created_at=datetime.now(timezone.utc),
            provider=provider.name,
            model=completion.model or provider.model,
            prompt_hash=PromptBuilder.digest(messages),
            context_hash=context.digest(),
            source_hashes={
                path: index.file_by_path(path).content_hash  # type: ignore[union-attr]
                for path in sorted(context.paths)
                if index.file_by_path(path) is not None
            },
            text=completion.text,
            validation=report.to_dict(),
            status=report.status,
            repository_revision=project.repository_revision,
            analyzer_version=ANALYZER_VERSION,
        )
        if save:
            self._repository.save_explanation(explanation)
        return ExplanationResult(context, messages, completion, report, explanation)

    # --- 保存済みの解説 ---

    def list(self, project: Project, target_kind: str | None = None) -> list[Explanation]:
        return self._repository.list_explanations(project.project_id, target_kind)

    def get(self, project: Project, id_prefix: str) -> Explanation | None:
        return self._repository.get_explanation(project.project_id, id_prefix)

    @staticmethod
    def stale_paths(project: Project, index: ProjectIndex, explanation: Explanation) -> list[str]:
        """解説の根拠にしたファイルのうち、生成時から変更された（または無くなった）もの。"""

        freshness = FreshnessService()
        stale = []
        for path, content_hash in explanation.source_hashes.items():
            source_file = index.file_by_path(path)
            if source_file is None or source_file.content_hash != content_hash:
                stale.append(path)  # 再解析で内容が変わった、または解析対象から外れた
            elif freshness.check_file(project, source_file) != FileFreshness.FRESH:
                stale.append(path)  # 解析後に、ディスク上のファイルが変更された
        return stale

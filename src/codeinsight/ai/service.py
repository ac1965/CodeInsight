from __future__ import annotations

import builtins
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime

from codeinsight.ai.citations import CitationValidator, ValidationReport
from codeinsight.ai.config import AIConfig
from codeinsight.ai.context import Context, ContextBuilder, ContextError
from codeinsight.ai.prompt import PromptBuilder
from codeinsight.ai.provider import AIProvider, AIProviderError, Completion, Message, OpenAICompatibleProvider
from codeinsight.application.analysis_coordinator import ANALYZER_VERSION
from codeinsight.application.freshness_service import FreshnessService
from codeinsight.application.navigation_service import NavigationService
from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Explanation, FileFreshness, Project, Symbol
from codeinsight.infrastructure.analysis_repository import AnalysisRepository


@dataclass
class BatchItem:
    """一括生成の1件の結果。status: created（新規に生成）/ reused（保存済みを再利用）/ failed（失敗）/ skipped（打ち切りで未処理）。"""

    symbol: Symbol
    status: str
    result: ExplanationResult | None = None
    error: str = ""
    attempts: int = 0
    seconds: float = 0.0


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
        provider = self._make_provider()
        completion = provider.complete(messages, temperature=self._config.temperature, max_tokens=self._config.max_tokens)
        return self._record(project, index, context, question, messages, provider, completion, save)

    def _make_provider(self) -> AIProvider:
        return self._provider or OpenAICompatibleProvider(
            self._config.base_url, self._config.model or "", self._config.api_key, self._config.timeout
        )

    def _record(
        self, project: Project, index: ProjectIndex, context: Context, question: str | None, messages: list[Message],
        provider: AIProvider, completion: Completion, save: bool,
    ) -> ExplanationResult:
        """回答を検証し、解説として（解析結果とは別に）保存する。DBへの書き込みを伴うため、呼び出しは1つのスレッドから行う。"""

        report = CitationValidator(project, index, context).validate(completion.text)
        explanation = Explanation(
            explanation_id=uuid.uuid4().hex,
            project_id=project.project_id,
            target_kind=context.target_kind,
            target=context.target if context.target_kind != "question" else (question or context.target),
            created_at=datetime.now(UTC),
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

    # --- 複数の関数を並列に ---

    def explain_symbols(
        self,
        project: Project,
        index: ProjectIndex,
        symbols: list[Symbol],
        *,
        workers: int = 2,
        reuse: bool = True,
        save: bool = True,
        retries: int = 3,
        backoff: float = 2.0,
        sleep=time.sleep,
        on_event=None,
    ) -> list[BatchItem]:
        """複数の関数の解説を、AIへの問い合わせだけ並列にして生成する。

        * 根拠の組み立て・検証・保存は、1つのスレッドで順に行う（SQLiteの競合を避ける。AIの待ち時間だけを並列にする）。
        * 同じ対象・モデル・入力（プロンプトのハッシュ）の解説が保存済みで、根拠のファイルも変わっていなければ、
          AIへ送信せず再利用する（再実行・中断からの再開で、同じ内容への重複した問い合わせを避ける）。再検証は毎回行う。
        * 一時的な失敗（429・5xx・時間切れ）は、指数バックオフで再試行する。認証・権限・接続できないなど、再試行しても
          回復しない失敗が起きたら、残りの要求を打ち切り、「未処理」として記録する（失敗を握りつぶさない）。
        """

        if workers < 1:
            raise ValueError("workers は 1 以上にしてください")
        self._config.check_consent(require_model=self._provider is None)  # 許可が無ければ、何も送信しない
        provider = self._make_provider()
        notify = on_event or (lambda item, done, total: None)
        total = len(symbols)
        items: list[BatchItem | None] = [None] * total
        pending: list[tuple[int, Symbol, Context, list[Message]]] = []
        done = 0

        for position, symbol in enumerate(symbols):  # 準備（決定論的。AIは使わない）と、再利用の判定
            try:
                context = self._builder().for_symbol(project, index, symbol)
            except (ContextError, ValueError) as exc:
                items[position] = BatchItem(symbol, "failed", error=str(exc))
                done += 1
                notify(items[position], done, total)
                continue
            messages = PromptBuilder().build(context, None)
            cached = None
            if reuse:
                cached = self._repository.find_explanation(
                    project.project_id, context.target_kind, context.target, self._config.model or provider.model, PromptBuilder.digest(messages)
                )
                if cached is not None and self.stale_paths(project, index, cached):
                    cached = None  # 根拠のファイルが変わっている
            if cached is not None:
                report = CitationValidator(project, index, context).validate(cached.text)
                result = ExplanationResult(context, messages, Completion(cached.text, cached.model), report, cached)
                items[position] = BatchItem(symbol, "reused", result)
                done += 1
                notify(items[position], done, total)
            else:
                pending.append((position, symbol, context, messages))

        abort = threading.Event()
        abort_reason: list[str] = []

        def ask(messages: list[Message]) -> tuple[Completion | None, str, int, float]:
            """AIへ問い合わせる（再試行つき）。戻り値: (回答, エラー, 試行回数, 秒)。"""

            started = time.monotonic()
            for attempt in range(1, retries + 1):
                if abort.is_set():
                    return None, "", attempt - 1, 0.0
                try:
                    completion = provider.complete(messages, temperature=self._config.temperature, max_tokens=self._config.max_tokens)
                    return completion, "", attempt, time.monotonic() - started
                except Exception as exc:  # noqa: BLE001  予期しない失敗でも、他の要求を巻き込まず、失敗として記録する
                    if not isinstance(exc, AIProviderError):
                        return None, f"予期しないエラー: {exc.__class__.__name__}: {exc}", attempt, time.monotonic() - started
                    if exc.fatal:
                        abort.set()
                        abort_reason.append(str(exc))
                        return None, str(exc), attempt, time.monotonic() - started
                    if not exc.retryable or attempt == retries:
                        return None, str(exc), attempt, time.monotonic() - started
                    sleep(backoff * (2 ** (attempt - 1)))
            return None, "", retries, time.monotonic() - started

        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            futures = {executor.submit(ask, messages): (position, symbol, context, messages) for position, symbol, context, messages in pending}
            for future in as_completed(futures):
                position, symbol, context, messages = futures[future]
                completion, error, attempts, seconds = future.result()
                if completion is None:
                    status = "skipped" if (abort.is_set() and not error) else "failed"
                    items[position] = BatchItem(symbol, status, error=error or (abort_reason[0] if abort_reason else ""), attempts=attempts, seconds=seconds)
                else:
                    result = self._record(project, index, context, None, messages, provider, completion, save)
                    items[position] = BatchItem(symbol, "created", result, attempts=attempts, seconds=seconds)
                done += 1
                notify(items[position], done, total)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)  # 中断（Ctrl-C）でも、未着手の要求は取り消す
        return [item for item in items if item is not None]

    # --- 保存済みの解説 ---

    def list(self, project: Project, target_kind: str | None = None) -> list[Explanation]:
        return self._repository.list_explanations(project.project_id, target_kind)

    def get(self, project: Project, id_prefix: str) -> Explanation | None:
        return self._repository.get_explanation(project.project_id, id_prefix)

    @staticmethod
    def stale_paths(project: Project, index: ProjectIndex, explanation: Explanation) -> builtins.list[str]:
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

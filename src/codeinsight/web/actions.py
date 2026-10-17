"""操作モード（`serve --allow-actions`）の操作。AGENTS.md §3.14。

提供する操作は4つだけ: 再解析・AIの推測・動的解析の計画（表示のみ）・動的解析の実行（隔離・確認つき）。
対象リポジトリの変更、隔離なしの実行、ネットワークの許可、AIの推測以外の外部通信は、提供しない（入力に、その指定が無い）。

* 同時に実行できる操作は1つ。長い操作は別のスレッドで実行し、状態は `status()` で取得する。
* 操作は、専用のDB接続で行う（ビューアーの読み取りの接続とは別）。
* 履歴は、時刻・種類・パラメータ（秘密を含まない）・結果の要約を、メモリに残す（動的解析の実行・AIの推測・解析の結果は、DBに保存される）。
* 動的解析の実行は、同じ内容の計画を先に表示していること（計画の確認コード）を要求する。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from codeinsight.ai.config import AIConfig, ConsentError, load_ai_config
from codeinsight.ai.context import ContextError
from codeinsight.ai.provider import AIProviderError
from codeinsight.ai.service import ExplanationService
from codeinsight.application import FlowAnalysisError, NavigationService
from codeinsight.application.analysis_coordinator import AnalysisCoordinator, AnalysisProgress
from codeinsight.application.navigation_service import AmbiguousSymbolError, SymbolNotFoundError
from codeinsight.bootstrap import build_symbol_extractor
from codeinsight.domain import Project
from codeinsight.dynamic.executor import ExecutorError
from codeinsight.dynamic.permission import DynamicPermission, DynamicPermissionError
from codeinsight.dynamic.service import DynamicAnalysisService
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.web.api import ApiError

HISTORY_LIMIT = 100
MAX_COMMAND_ARGS = 32
MAX_ARG_LENGTH = 500
MAX_TIMEOUT = 600.0
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,199}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
KINDS = ("analyze", "infer", "dynamic-plan", "dynamic-run")
ACTION_LABELS = {"analyze": "再解析", "infer": "AIの推測", "dynamic-plan": "動的解析の計画", "dynamic-run": "動的解析の実行"}


class Cancelled(Exception):
    pass


def _reject_unknown(body: dict, allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ApiError(400, "受け付けない指定があります: " + ", ".join(k[:40] for k in unknown) + "（操作モードは、決められた指定だけを受け付けます）")


def _bool(body: dict, key: str) -> bool:
    value = body.get(key, False)
    if not isinstance(value, bool):
        raise ApiError(400, f"{key} は true / false で指定してください")
    return value


class ActionRunner:
    """操作の実行器。`handle(kind, body)` が操作を受け付け、`status()` が現在の状態と履歴を返す。"""

    def __init__(self, db_path: Path, project_id: str, on_finished: Callable[[], None] | None = None) -> None:
        self._db_path = db_path
        self._project_id = project_id
        self._on_finished = on_finished or (lambda: None)
        self._guard = threading.Lock()  # 同時に実行できる操作は1つ
        self._state = threading.Lock()  # 状態・履歴の更新
        self._current: dict | None = None
        self._history: list[dict] = []
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.run_executor: Any = None  # テスト用の差し替え（動的解析の実行器）。通常は None
        self.ai_provider: Any = None  # テスト用の差し替え（AIのプロバイダー）。通常は None

    # --- 状態 ---

    def ai_destination(self) -> dict:
        config = load_ai_config({})
        return {"base_url": config.base_url, "model": config.model or "", "local": config.is_local, "include_source": config.include_source}

    def status(self) -> dict:
        with self._state:
            current = dict(self._current) if self._current else None
            history = [dict(h) for h in self._history]
        return {
            "enabled": True, "busy": bool(current and current["state"] == "running"), "current": current, "history": history[::-1],
            "ai": self.ai_destination(),
            "note": "操作モードです。再解析・AIの推測・動的解析の計画と実行だけができます。対象リポジトリは変更せず、動的解析はコンテナ（ネットワーク遮断）の中で、確認のうえ実行します。",
        }

    def _record(self, kind: str, params: dict) -> dict:
        entry = {
            "id": uuid.uuid4().hex[:12], "kind": kind, "label": ACTION_LABELS[kind], "params": params, "state": "running",
            "started_at": datetime.now().isoformat(timespec="seconds"), "finished_at": None,
            "progress": {"done": 0, "total": 0, "message": ""}, "result": None, "error": None,
        }
        with self._state:
            self._current = entry
            self._history.append(entry)
            del self._history[:-HISTORY_LIMIT]
        return entry

    def _finish(self, entry: dict, state: str, result: dict | None = None, error: str | None = None) -> None:
        with self._state:
            entry.update(state=state, result=result, error=error, finished_at=datetime.now().isoformat(timespec="seconds"))

    # --- 受け付け ---

    def handle(self, kind: str, body: dict) -> dict:
        if kind == "cancel":
            _reject_unknown(body, set())
            if not self._thread or not self._thread.is_alive():
                raise ApiError(409, "実行中の操作がありません")
            self._cancel.set()
            return {"cancel_requested": True, "note": "取り消せるのは、AIの推測の一括です（次の1件の前に止まります）。再解析・動的解析の実行は、取り消せません。"}
        if kind not in KINDS:
            raise ApiError(404, "存在しない操作です")
        validators = {"analyze": self._validate_analyze, "infer": self._validate_infer, "dynamic-plan": self._validate_dynamic, "dynamic-run": self._validate_dynamic_run}
        params = validators[kind](body)
        if not self._guard.acquire(blocking=False):
            raise ApiError(409, "別の操作を実行中です。状態を確認して、終わってから実行してください")
        entry = self._record(kind, self._public(kind, params))
        if kind == "dynamic-plan":  # 表示のみ。短いので、その場で
            try:
                self._finish(entry, "done", self._plan(params))
            except ApiError as exc:
                self._finish(entry, "failed", error=exc.message)
                raise
            finally:
                self._guard.release()
            return {"operation": entry}
        self._cancel.clear()
        self._thread = threading.Thread(target=self._work, args=(kind, params, entry), daemon=True)
        self._thread.start()
        return {"operation": entry}

    @staticmethod
    def _public(kind: str, params: dict) -> dict:
        """履歴に残すパラメータ（秘密を含まない。環境変数は、名前だけ）。"""

        return {k: v for k, v in params.items() if k != "confirm_digest"}

    # --- 入力の検査 ---

    @staticmethod
    def _validate_analyze(body: dict) -> dict:
        _reject_unknown(body, set())
        return {}

    @staticmethod
    def _validate_infer(body: dict) -> dict:
        _reject_unknown(body, {"symbol", "all", "limit", "force", "dry_run"})
        everything = _bool(body, "all")
        symbol = body.get("symbol")
        if everything == (symbol is not None):
            raise ApiError(400, "関数名（symbol）か、一括（all）のどちらか一方を指定してください")
        params: dict = {"dry_run": _bool(body, "dry_run"), "force": _bool(body, "force")}
        if everything:
            limit = body.get("limit", 10)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
                raise ApiError(400, "limit は 1〜50 の整数です")
            params.update(all=True, limit=limit)
        else:
            if not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 300 or "\x00" in symbol:
                raise ApiError(400, "symbol は、300文字以内の関数名です")
            params.update(all=False, symbol=symbol.strip())
        return params

    @staticmethod
    def _validate_dynamic(body: dict) -> dict:
        _reject_unknown(body, {"command", "collector", "timeout", "env", "image", "confirm_digest"})
        command = body.get("command")
        if not isinstance(command, list) or not command or len(command) > MAX_COMMAND_ARGS or not all(isinstance(c, str) and 0 < len(c) <= MAX_ARG_LENGTH and "\x00" not in c for c in command):
            raise ApiError(400, f"command は、1〜{MAX_COMMAND_ARGS}個の文字列（1つの引数が1要素。シェルを介しません。コマンドは推測して選びません）です")
        collector = body.get("collector", "python")
        if collector not in ("python", "c"):
            raise ApiError(400, "collector は python か c です")
        timeout = body.get("timeout", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 0 < timeout <= MAX_TIMEOUT:
            raise ApiError(400, f"timeout は 0 より大きく {int(MAX_TIMEOUT)} 秒以下です")
        env = body.get("env", [])
        if not isinstance(env, list) or len(env) > 16 or not all(isinstance(e, str) and _ENV_NAME.match(e) for e in env):
            raise ApiError(400, "env は、環境変数の名前（最大16個）の一覧です。値は渡せません")
        image = body.get("image")
        if image is not None and (not isinstance(image, str) or not _IMAGE.match(image)):
            raise ApiError(400, "image の形式が不正です")
        params = {"command": list(command), "collector": collector, "timeout": float(timeout), "env": sorted(set(env)), "image": image}
        digest = body.get("confirm_digest")
        if digest is not None:
            params["confirm_digest"] = digest
        return params

    def _validate_dynamic_run(self, body: dict) -> dict:
        params = self._validate_dynamic(body)
        digest = params.get("confirm_digest")
        if not isinstance(digest, str) or digest != self.plan_digest(params):
            raise ApiError(400, "実行する内容の計画が、確認されていません。先に「計画を表示」で、同じ内容の計画を確認してください（確認コードが一致しません）")
        return params

    @staticmethod
    def plan_digest(params: dict) -> str:
        """確認した計画の識別子（コマンド・収集器・時間・環境変数・イメージ）。実行は、同じ内容の計画を確認していることを要求する。"""

        canonical = json.dumps({k: params.get(k) for k in ("command", "collector", "timeout", "env", "image")}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _permission(params: dict) -> DynamicPermission:
        # ネットワークの許可・隔離なしの実行は、操作モードでは指定できない（入力に無く、ここでも固定する）
        return DynamicPermission(
            allow_run=True, command=tuple(params["command"]), allow_network=False, env_allowlist=tuple(params["env"]),
            sandbox_backend="container", allow_unsandboxed=False, timeout_seconds=params["timeout"], keep_workdir=False, collector=params["collector"],
        )

    # --- 実体 ---

    def _open(self) -> tuple[AnalysisRepository, Project]:
        repository = AnalysisRepository(self._db_path)
        projects = [p for p in repository.list_projects() if p.project_id == self._project_id]
        if not projects:
            repository.close()
            raise ApiError(404, "プロジェクトが見つかりません")
        return repository, projects[0]

    def _plan(self, params: dict) -> dict:
        repository, project = self._open()
        try:
            index = NavigationService(repository).load_index(project)
            languages = {f.language.value for f in index.files.values()}
            permission = self._permission(params)
            plan = DynamicAnalysisService().plan(project, permission, languages)
        finally:
            repository.close()
        runnable = plan.permitted and plan.sandbox_available is not False
        reasons = list(plan.denied_reasons) + ([] if plan.sandbox_available is not False else ["コンテナ（Docker）をこの計算機で利用できません"])
        return {
            "plan_digest": self.plan_digest(params), "runnable": runnable, "denied_reasons": reasons, "executed": False,
            "confirmation": {
                "command": params["command"], "collector": params["collector"], "image": params["image"] or "（収集器の既定のイメージ）",
                "network": "遮断（操作モードでは許可できません）", "unsandboxed": "不可（操作モードでは許可できません）", "target": plan.sandbox.get("target_mount"),
                "workdir": plan.sandbox.get("workdir"), "timeout_seconds": params["timeout"], "memory_mb": plan.sandbox.get("memory_mb"), "env": params["env"],
            },
            "collectors": [{"name": c.name, "language": c.language, "mechanism": c.mechanism, "needs_rebuild": c.needs_rebuild, "implemented": c.implemented} for c in plan.collectors],
            "notes": plan.notes,
        }

    def _work(self, kind: str, params: dict, entry: dict) -> None:
        try:
            result = {"analyze": self._analyze, "infer": self._infer, "dynamic-run": self._dynamic_run}[kind](params, entry)
            self._finish(entry, "done", result)
        except Cancelled:
            self._finish(entry, "cancelled", error="取り消しました")
        except ApiError as exc:
            self._finish(entry, "failed", error=exc.message)
        except (ConsentError, AIProviderError, ContextError, FlowAnalysisError, ExecutorError) as exc:
            self._finish(entry, "failed", error=str(exc)[:500])
        except DynamicPermissionError as exc:
            self._finish(entry, "failed", error="許可が揃っていません: " + " / ".join(exc.reasons)[:500])
        except Exception:  # noqa: BLE001 - 内部の詳細（パス・スタックトレース）は、履歴に残さない
            self._finish(entry, "failed", error="内部エラーが発生しました")
        finally:
            try:
                self._on_finished()
            finally:
                self._guard.release()

    def _progress(self, entry: dict, done: int, total: int, message: str = "") -> None:
        with self._state:
            entry["progress"] = {"done": done, "total": total, "message": message[:100]}

    def _analyze(self, params: dict, entry: dict) -> dict:
        repository, project = self._open()
        try:
            configured = project.configuration.compile_commands_dir
            coordinator = AnalysisCoordinator(repository, build_symbol_extractor(Path(configured) if configured else None))

            def progress(event: AnalysisProgress) -> None:
                self._progress(entry, event.done, event.total, event.relative_path)

            result = coordinator.analyze_project(project, progress=progress)
            stats = coordinator.last_stats
            return {
                "status": result.status.value, "warnings": len(result.warnings), "errors": len(result.errors),
                "reanalysis": {"changed": stats.changed, "dependents": stats.dependents, "skipped": stats.skipped, "removed": stats.removed},
                "note": "対象リポジトリは変更していません。",
            }
        finally:
            repository.close()

    def _infer(self, params: dict, entry: dict) -> dict:
        repository, project = self._open()
        try:
            navigation = NavigationService(repository)
            index = navigation.load_index(project)
            config: AIConfig = load_ai_config({})
            service = ExplanationService(repository, navigation, config, provider=self.ai_provider) if self.ai_provider else ExplanationService(repository, navigation, config)
            destination = {"base_url": config.base_url, "model": config.model or "", "local": config.is_local}
            if params["all"]:
                todo, _pending, skipped = service.pending_inference(project, index, params["force"])
                todo = todo[: params["limit"]]
            else:
                try:
                    todo, skipped = [navigation.resolve_symbol(index, params["symbol"]).symbol], 0
                except SymbolNotFoundError as exc:
                    raise ApiError(404, f"シンボルが見つかりません: {params['symbol'][:100]}") from exc
                except AmbiguousSymbolError as exc:
                    raise ApiError(409, "複数のシンボルに一致します。修飾名で指定してください: " + ", ".join(h.symbol.qualified_name for h in exc.candidates[:5])) from exc
            items: list[dict] = []
            for number, symbol in enumerate(todo, 1):
                if self._cancel.is_set():
                    raise Cancelled
                self._progress(entry, number - 1, len(todo), symbol.qualified_name)
                try:
                    result = service.infer_targets(project, index, symbol, dry_run=params["dry_run"], save=True)
                except (ConsentError, ContextError, AIProviderError, FlowAnalysisError) as exc:
                    items.append({"function": symbol.qualified_name, "error": str(exc)[:300]})
                    continue
                if result.dry_run:
                    items.append({"function": symbol.qualified_name, "dry_run": True, "files": sorted(result.context.paths), "characters": sum(len(m.content) for m in result.messages),
                                  "prompt": [{"role": m.role, "content": m.content} for m in result.messages]})
                elif result.explanation is not None and result.report is not None:
                    items.append({"function": symbol.qualified_name, "id": result.explanation.explanation_id, "status": result.explanation.status.value})
            self._progress(entry, len(todo), len(todo))
            if items and all("error" in i for i in items):  # 全件が失敗なら、完了として扱わない
                raise ApiError(502, items[0]["error"])
            return {
                "destination": destination, "dry_run": params["dry_run"], "targets": len(todo), "skipped_saved": skipped, "items": items,
                "note": "AIの推測は、解析結果ではありません。検証済み以外は、事実として扱わないでください。送信先は上の通りです（許可の確認はしません）。",
            }
        finally:
            repository.close()

    def _dynamic_run(self, params: dict, entry: dict) -> dict:
        repository, project = self._open()
        try:
            index = NavigationService(repository).load_index(project)
            permission = self._permission(params)
            self._progress(entry, 0, 1, "コンテナで実行しています（取り消せません）")
            started = time.monotonic()
            outcome = DynamicAnalysisService().run(project, permission, index, repository, image=params["image"], run_executor=self.run_executor)
            run = outcome.run
            self._progress(entry, 1, 1)
            return {
                "run_id": run.run_id, "status": run.status.value, "exit_code": run.exit_code, "duration_seconds": round(time.monotonic() - started, 1),
                "collector": run.collector, "observations": len(outcome.observations), "notes": run.notes[:5],
                "note": "観測は、この1回の実行で起きたことの記録です。「動的解析」の画面で、静的解析との差分を確認できます。",
            }
        finally:
            repository.close()


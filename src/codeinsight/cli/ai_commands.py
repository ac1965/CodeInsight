from __future__ import annotations

import argparse
import sys

from codeinsight.ai.citations import CitationStatus, ValidationReport
from codeinsight.ai.config import AIConfig, ConsentError, config_file_warnings, load_ai_config
from codeinsight.ai.context import ContextError
from codeinsight.ai.provider import AIProviderError, OpenAICompatibleProvider
from codeinsight.ai.service import ExplanationResult, ExplanationService
from codeinsight.application import FlowAnalysisError, NavigationService
from codeinsight.cli.common import CliError, emit_json, prepare_read, resolve_symbol_arg, safe
from codeinsight.domain import Explanation, ExplanationStatus

STATUS_LABEL = {
    ExplanationStatus.VERIFIED: "検証済み（引用はすべて確認できた）",
    ExplanationStatus.PARTIAL: "一部未確認（根拠の示されていない記述を含む）",
    ExplanationStatus.UNVERIFIED: "未検証（引用の誤り・存在しない名前・根拠なし）",
}
_CITATION_LABEL = {
    CitationStatus.NOT_FOUND: "解析対象に存在しないファイル",
    CitationStatus.OUT_OF_RANGE: "行番号がファイルの範囲外",
    CitationStatus.OUT_OF_CONTEXT: "AIに渡した根拠の範囲外（渡していない内容を根拠にしている）",
    CitationStatus.STALE: "解析後にファイルが変更されている",
}


def _add_ai_options(sub: argparse.ArgumentParser) -> None:
    group = sub.add_argument_group("AI（既定ではソースコードを送信しません）")
    group.add_argument("--allow-send", action="store_true", help="ソースコード・解析結果をAIへ送信することを許可する（環境変数 CODEINSIGHT_AI_ALLOW_SEND=1 でも可）")
    group.add_argument("--allow-remote", action="store_true", help="送信先がこの計算機の外でもよい場合に限り、追加で指定する")
    group.add_argument("--dry-run", action="store_true", help="AIへ送信せず、送信される内容（プロンプト）だけを表示する")
    group.add_argument("--no-source", action="store_true", help="生のソース行を送らず、解析結果の事実（名前・位置・件数）だけを送る")
    group.add_argument("--no-save", action="store_true", help="解説を保存しない")
    group.add_argument("--ai-base-url", help="OpenAI互換APIのURL（既定: http://localhost:11434/v1。Ollama）")
    group.add_argument("--ai-model", help="モデル名（例: qwen3-coder）。環境変数 CODEINSIGHT_AI_MODEL でも可")
    group.add_argument("--max-context", type=int, help="AIへ渡す根拠の最大文字数")
    group.add_argument("--max-tokens", type=int, help="AIの応答の最大トークン数")
    group.add_argument("--timeout", type=float, help="AIの応答待ちの秒数")


def _config(args: argparse.Namespace) -> AIConfig:
    return load_ai_config(
        {
            "base_url": getattr(args, "ai_base_url", None),
            "model": getattr(args, "ai_model", None),
            "allow_send": True if getattr(args, "allow_send", False) else None,
            "allow_remote": True if getattr(args, "allow_remote", False) else None,
            "include_source": False if getattr(args, "no_source", False) else None,
            "max_context_chars": getattr(args, "max_context", None),
            "max_tokens": getattr(args, "max_tokens", None),
            "timeout": getattr(args, "timeout", None),
        }
    )


def _service(args: argparse.Namespace):
    repository, project, index, stale = prepare_read(args)
    navigation = NavigationService(repository)
    return repository, project, index, navigation, ExplanationService(repository, navigation, _config(args)), stale


def _guard(call):
    """AI解説の失敗を、利用者向けのメッセージに変換する。非AI機能には影響しない。"""

    try:
        return call()
    except ConsentError as exc:
        raise CliError(str(exc), 3) from exc
    except AIProviderError as exc:
        raise CliError(str(exc), 4) from exc
    except (ContextError, FlowAnalysisError) as exc:
        raise CliError(str(exc), 1) from exc


# --- 表示 ---


def render_annotated(text: str, report: ValidationReport) -> str:
    """回答に、検証結果の印（⚠未確認・✗引用に問題）を付ける。検証済みの事実と区別するため。"""

    verdicts = {v.line_no: v.kind for v in report.lines}
    rendered = []
    for number, line in enumerate(text.splitlines(), 1):
        kind = verdicts.get(number)
        if kind == "unsupported":
            rendered.append(f"⚠未確認 {line}")
        elif kind == "bad_citation":
            rendered.append(f"✗引用に問題 {line}")
        else:
            rendered.append(line)
    return "\n".join(rendered)


def _print_validation(report: ValidationReport) -> None:
    total = len(report.citations)
    verified = total - len(report.bad_citations)
    print(f"\n──── 検証結果: {STATUS_LABEL[report.status]}")
    print(f"  引用 {total}件のうち検証できたもの {verified}件 / 根拠のある記述 {report.count('evidenced')}行 / 推論と明示された記述 {report.count('inference')}行 / 根拠の無い記述 {report.count('unsupported')}行")
    for citation in report.bad_citations:
        print(f"  ✗ 行{citation.line_no} {safe(citation.raw)}: {_CITATION_LABEL[citation.status]}")
    for number, name in report.unknown_identifiers:
        print(f"  ✗ 行{number} `{safe(name)}`: 解析結果にも渡した根拠にも見当たらない名前（AIが作った可能性）")
    if report.missing_sections:
        print(f"  ・ 想定の見出しが無い: {', '.join(report.missing_sections)}")
    print("  ※ 検証できるのは「示された根拠が存在し、渡した範囲内であること」までです。根拠が主張を実際に裏付けているかは、利用者が確認してください。")


def _print_result(args: argparse.Namespace, result: ExplanationResult, config: AIConfig) -> int:
    if result.dry_run:
        system, user = result.messages[0], result.messages[1]
        print("［dry-run］AIへは何も送信していません。--allow-send を付けると、次の内容が送信されます。")
        print(f"  送信先: {config.base_url}（{'この計算機の内' if config.is_local else 'この計算機の外'}）  モデル: {config.model or '未指定'}")
        print(f"  含めるファイル: {', '.join(sorted(result.context.paths)) or 'なし'}")
        print(f"  ソースコード: {'含める' if config.include_source else '含めない（解析結果の事実のみ）'}  文字数: {len(system.content) + len(user.content)}")
        for note in result.context.notes:
            print(f"  ・{safe(note)}")
        print("\n--- system ---\n" + system.content + "\n\n--- user ---\n" + user.content)
        return 0
    assert result.completion is not None and result.report is not None and result.explanation is not None
    explanation = result.explanation
    if getattr(args, "format", "text") == "json":
        emit_json({
            "explanation_id": explanation.explanation_id, "model": explanation.model, "provider": explanation.provider,
            "status": explanation.status.value, "text": explanation.text, "validation": explanation.validation,
            "saved": not getattr(args, "no_save", False), "ai_generated": True,
        })
        return 0
    print(f"━━ AI解説（解析結果ではありません） モデル: {safe(explanation.model)} / 対象: {safe(explanation.target)}")
    print(f"   解説ID: {explanation.explanation_id[:8]}{'（保存済み）' if not getattr(args, 'no_save', False) else '（保存していません）'}")
    print()
    print(safe(render_annotated(result.completion.text, result.report)))
    _print_validation(result.report)
    for note in result.context.notes:
        print(f"  ・渡した根拠の制約: {safe(note)}", file=sys.stderr)
    print("※ これはAIが解析結果から生成した解説です。確定した事実ではありません。", file=sys.stderr)
    return 0


# --- コマンド ---


def cmd_explain(args: argparse.Namespace) -> int:
    repository, project, index, navigation, service, _ = _service(args)
    symbol = resolve_symbol_arg(args, navigation, index, args.name, project).symbol
    result = _guard(lambda: service.explain_symbol(project, index, symbol, dry_run=args.dry_run, save=not args.no_save))
    return _print_result(args, result, _config(args))


def cmd_explain_file(args: argparse.Namespace) -> int:
    repository, project, index, navigation, service, _ = _service(args)
    result = _guard(lambda: service.explain_file(project, index, args.path, dry_run=args.dry_run, save=not args.no_save))
    return _print_result(args, result, _config(args))


def cmd_explain_path(args: argparse.Namespace) -> int:
    repository, project, index, navigation, service, _ = _service(args)
    source = resolve_symbol_arg(args, navigation, index, args.source, project).symbol
    target = resolve_symbol_arg(args, navigation, index, args.target, project).symbol
    result = _guard(lambda: service.explain_path(project, index, source, target, dry_run=args.dry_run, save=not args.no_save))
    return _print_result(args, result, _config(args))


def cmd_ask(args: argparse.Namespace) -> int:
    repository, project, index, navigation, service, _ = _service(args)
    question = " ".join(args.question)
    result = _guard(lambda: service.ask(project, index, question, dry_run=args.dry_run, save=not args.no_save))
    return _print_result(args, result, _config(args))


def cmd_explanations(args: argparse.Namespace) -> int:
    repository, project, index, stale = prepare_read(args)
    service = ExplanationService(repository, NavigationService(repository), _config(args))
    if args.id:
        explanation = service.get(project, args.id)
        if explanation is None:
            raise CliError(f"解説が見つかりません（IDが一意に定まりません）: {safe(args.id)}", 2)
        return _show_explanation(args, service, project, index, explanation)
    items = service.list(project, args.kind)[: args.limit]
    if args.format == "json":
        emit_json([{"id": e.explanation_id, "kind": e.target_kind, "target": e.target, "status": e.status.value,
                         "model": e.model, "created_at": e.created_at.isoformat(),
                         "stale": bool(service.stale_paths(project, index, e))} for e in items])
        return 0
    print("保存済みのAI解説（解析結果とは別に管理。AIが生成したもので、確定した事実ではありません）")
    for e in items:
        marker = "  [古い: 対象ファイルが変更されています]" if service.stale_paths(project, index, e) else ""
        print(f"  {e.explanation_id[:8]}  {e.created_at:%Y-%m-%d %H:%M}  {e.target_kind:<8} {STATUS_LABEL[e.status].split('（')[0]:<8} {safe(e.model)}  {safe(e.target)}{marker}")
    if not items:
        print("  保存済みの解説はありません")
    return 0


def _show_explanation(args, service: ExplanationService, project, index, explanation: Explanation) -> int:
    stale = service.stale_paths(project, index, explanation)
    if args.format == "json":
        emit_json({"id": explanation.explanation_id, "target": explanation.target, "model": explanation.model,
                        "status": explanation.status.value, "text": explanation.text, "validation": explanation.validation,
                        "stale_paths": stale, "source_hashes": explanation.source_hashes, "ai_generated": True})
        return 0
    print(f"━━ AI解説（解析結果ではありません） {explanation.explanation_id[:8]}  {explanation.created_at:%Y-%m-%d %H:%M}")
    print(f"   対象: {safe(explanation.target_kind)} {safe(explanation.target)} / モデル: {safe(explanation.model)} / リビジョン: {explanation.repository_revision or '-'}")
    if stale:
        print(f"   ⚠ 古い解説です。根拠にしたファイルが変更されています: {', '.join(safe(p) for p in stale)}")
    print()
    print(safe(explanation.text))
    validation = explanation.validation
    print(f"\n──── 検証結果（生成時）: {STATUS_LABEL[explanation.status]}")
    print(f"  引用 {len(validation.get('citations', []))}件 / 根拠の無い記述 {len(validation.get('unsupported_lines', []))}行 / 存在を確認できない名前 {len(validation.get('unknown_identifiers', []))}件")
    return 0


def cmd_ai_status(args: argparse.Namespace) -> int:
    config = _config(args)
    warnings = config_file_warnings()
    for warning in warnings:
        print(f"警告: {warning}", file=sys.stderr)
    if args.format == "json":
        status: dict[str, object] = {"config": config.redacted(), "reachable": False, "models": [], "warnings": warnings}
    else:
        print("AIの設定（APIキーは表示しません）")
        for key, value in config.redacted().items():
            print(f"  {key}: {value}")
    try:
        models = OpenAICompatibleProvider(config.base_url, config.model or "", config.api_key, min(config.timeout, 10.0)).check()
    except AIProviderError as exc:
        if args.format == "json":
            status["error"] = str(exc)
            emit_json(status)
        else:
            print(f"\n接続できません: {exc}")
        return 0
    if args.format == "json":
        status.update({"reachable": True, "models": models, "model_available": config.model in models if config.model else None})
        emit_json(status)
        return 0
    print(f"\n接続できました。利用できるモデル: {', '.join(models) or '（なし）'}")
    if config.model and config.model not in models:
        print(f"  ⚠ 指定のモデル {config.model} は、一覧にありません。")
    print("ソースコードは送信していません（接続の確認のみ）。")
    return 0


def register(add) -> None:
    """build_parser から呼ばれ、AI関連のコマンドを登録する。"""

    explain = add("explain", "関数・クラスの処理をAIで解説する（解析結果とソースを根拠に渡し、引用を検証する）", cmd_explain, ("text", "json"))
    explain.add_argument("name", help="名前または修飾名")
    explain.add_argument("--file")
    _add_ai_options(explain)

    explain_file = add("explain-file", "ファイルの役割と構成をAIで解説する", cmd_explain_file, ("text", "json"))
    explain_file.add_argument("path", help="解析対象ファイルの相対パス")
    _add_ai_options(explain_file)

    explain_path = add("explain-path", "2つの関数の間の呼び出し経路をAIで解説する", cmd_explain_path, ("text", "json"))
    explain_path.add_argument("source")
    explain_path.add_argument("target")
    explain_path.add_argument("--file")
    _add_ai_options(explain_path)

    ask = add("ask", "コードに関する質問に、関連するコードを検索してAIが答える（根拠の引用を検証する）", cmd_ask, ("text", "json"))
    ask.add_argument("question", nargs="+", help="質問（日本語可）")
    _add_ai_options(ask)

    explanations = add("explanations", "保存済みのAI解説を一覧・表示する（解析結果とは別に管理）", cmd_explanations, ("text", "json"))
    explanations.add_argument("id", nargs="?", help="解説ID（先頭の数文字でも可）")
    explanations.add_argument("--kind", choices=("symbol", "file", "path", "question"))
    explanations.add_argument("--limit", type=int, default=20)

    status = add("ai-status", "AIの設定と接続を確認する（ソースコードは送信しない）", cmd_ai_status, ("text", "json"), exclude=False)
    status.add_argument("--ai-base-url")
    status.add_argument("--ai-model")
    status.add_argument("--timeout", type=float)

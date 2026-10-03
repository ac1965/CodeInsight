from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from pathlib import Path

from codeinsight.ai.citations import CitationStatus, ValidationReport
from codeinsight.ai.config import AIConfig, ConsentError, config_file_warnings, load_ai_config
from codeinsight.ai.context import ContextError
from codeinsight.ai.evaluation import EvalError, EvalResult, evaluate, load_cases
from codeinsight.ai.provider import AIProviderError, OpenAICompatibleProvider
from codeinsight.ai.service import ExplanationResult, ExplanationService
from codeinsight.application import FlowAnalysisError, NavigationService
from codeinsight.application.navigation_service import AmbiguousSymbolError, SymbolNotFoundError
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


def _file_base(name: str) -> str:
    """`修飾名@行` を、出力ファイル名の基にする（`a.b@12` → `a.b_L12`。英数字・`._-` 以外は `_`）。"""

    return re.sub(r"[^A-Za-z0-9._-]", "_", re.sub(r"@(\d+)$", r"_L\1", name))


def _rendered(result: ExplanationResult, config: AIConfig) -> str:
    """`explain` と同じ表示を、文字列として得る（一括生成で、ファイルに書くため）。"""

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _print_result(argparse.Namespace(format="text", no_save=False), result, config)
    return buffer.getvalue()


def cmd_explain_many(args: argparse.Namespace) -> int:
    """一覧の関数のAI解説を、並列に生成して、ファイルに書き出す（再実行・中断からの再開ができる）。"""

    repository, project, index, navigation, service, _ = _service(args)
    config = _config(args)
    requested: list[tuple[str, str | None]] = [(n, None) for n in args.names]
    if args.from_file:
        for line in Path(args.from_file).read_text(encoding="utf-8").splitlines():
            if line.strip():
                name, _, file_path = line.partition("\t")
                requested.append((name.strip(), file_path.strip() or None))
    if not requested:
        raise CliError("解説する関数が指定されていません（名前、または --from-file）。", 2)
    out_dir = Path(args.out_dir) if args.out_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    symbols, names, unresolved = [], [], []
    for name, path in requested:
        try:
            symbol = navigation.resolve_symbol(index, name, file=path).symbol
        except (SymbolNotFoundError, AmbiguousSymbolError) as exc:
            unresolved.append((name, f"シンボルを特定できません: {exc}"))
            continue
        symbols.append(symbol)
        names.append(name)

    def progress(item, done: int, total: int) -> None:
        label = {"created": "生成", "reused": "再利用", "failed": "失敗", "skipped": "未処理"}[item.status]
        extra = f"（{item.seconds:.0f}秒" + (f"、再試行 {item.attempts - 1}回" if item.attempts > 1 else "") + "）" if item.status == "created" else ""
        detail = f": {safe(item.error)[:120]}" if item.error and item.status == "failed" else ""
        print(f"[{done}/{total}] {label} {safe(item.symbol.qualified_name)}{extra}{detail}", flush=True)

    print(f"AI解説を作成します（{len(symbols)}件、並列 {args.workers}、送信先: {config.base_url}（{'この計算機の内' if config.is_local else 'この計算機の外'}）、保存済みの再利用: {'しない' if args.no_reuse else 'する'}）", flush=True)
    items = _guard(lambda: service.explain_symbols(
        project, index, symbols, workers=args.workers, reuse=not args.no_reuse, save=not args.no_save, retries=args.retries, on_event=progress,
    ))
    written = 0
    for name, item in zip(names, items, strict=True):  # 結果は、入力と同じ順序で返る
        if out_dir and item.result is not None:
            note = "（保存済みの解説を再利用しました。根拠の入力とモデルが同じため。検証は再実行しています）\n" if item.status == "reused" else ""
            (out_dir / f"{_file_base(name)}.md").write_text(note + _rendered(item.result, config), encoding="utf-8")
            written += 1
    counts = {s: sum(1 for i in items if i.status == s) for s in ("created", "reused", "failed", "skipped")}
    failed = counts["failed"] + len(unresolved)
    print(f"AI解説: {counts['created']} 件生成、{counts['reused']} 件再利用、{failed} 件失敗、{counts['skipped']} 件未処理"
          + (f"（{written}件を {out_dir} に書き出し）" if out_dir else ""))
    for name, reason in unresolved:
        print(f"  ! {safe(name)}: {reason}", file=sys.stderr)
    for item in items:
        if item.status in ("failed", "skipped") and item.error:
            print(f"  ! {safe(item.symbol.qualified_name)}: {safe(item.error)}", file=sys.stderr)
    if counts["skipped"]:
        print("  ※ 認証・権限・接続の失敗など、再試行しても回復しない失敗があったため、残りを打ち切りました。原因を解消して、同じコマンドを再実行してください（生成済みの解説は再利用されます）。", file=sys.stderr)
    return 1 if (failed or counts["skipped"]) else 0


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


def cmd_ai_eval(args: argparse.Namespace) -> int:
    """評価ケースを実行し、AI解説を機械的な指標で採点する（モデル・プロンプトの比較用）。"""

    from pathlib import Path

    try:
        cases = load_cases(Path(args.cases))
    except EvalError as exc:
        raise CliError(str(exc), 1) from exc
    if args.only:
        cases = [c for c in cases if c.case_id in set(args.only)]
        if not cases:
            raise CliError(f"指定のidのケースがありません: {', '.join(args.only)}", 2)
    if args.list:
        for case in cases:
            print(f"{case.case_id:<26} {case.kind:<8} {Path(case.project).name:<18} {case.note}")
        return 0

    config = _config(args)
    _guard(config.check_consent)  # 送信の許可・モデルの確認（許可が無ければ、何も送信しない）
    marks = {"pass": "✓", "fail": "✗", "error": "!"}

    def show(result: EvalResult) -> None:
        if args.format == "json":
            return
        if result.status == "error":
            print(f"! {result.case.case_id:<26} エラー: {safe(result.error)[:110]}")
            return
        recall = "-" if result.evidence_recall is None else f"{result.evidence_recall:.0%}"
        terms = "-" if result.term_recall is None else f"{result.term_recall:.0%}"
        print(
            f"{marks[result.status]} {result.case.case_id:<26} 検証:{STATUS_LABEL[ExplanationStatus(result.validation_status)].split('（')[0]:<7} "
            f"引用 {result.citations_valid}/{result.citations_total}  根拠再現 {recall:>4}  語再現 {terms:>4}  "
            f"作り話の罠 {len(result.forbidden_hits)}  未確認行 {result.unsupported_lines}  {result.seconds:.0f}秒"
        )
        for reason in result.reasons:
            print(f"    - {safe(reason)}")

    summary = _guard(lambda: evaluate(cases, config, repeat=args.repeat, on_result=show))
    data = summary.to_dict()
    if args.report:
        Path(args.report).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.format == "json":
        emit_json(data)
    else:
        def pct(value):
            return "-" if value is None else f"{value:.0%}"

        print(f"\n── 集計（{len(summary.results)}件、エラー {data['errors']}件、モデル: {safe(config.model or '-')}）")
        print(f"  合格率 {pct(summary.pass_rate)} / 引用の妥当性 {pct(summary.citation_validity)} / 根拠の再現率(平均) {pct(summary.mean_evidence_recall)} / 語の再現率(平均) {pct(summary.mean_term_recall)}")
        print(f"  作り話の罠への該当 {summary.forbidden_total}件 / 存在を確認できない名前 {summary.unknown_identifier_total}件 / 検証状態 {summary.status_counts}")
        print("  ※ 機械的な指標です。語の再現や根拠の引用が、内容の正しさを保証するものではありません。")
    if summary.pass_rate is not None and summary.pass_rate < args.min_pass_rate:
        return 5
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

    ai_eval = add("ai-eval", "評価ケース(eval/ai_cases.toml)を実行し、AI解説を機械的に採点する（モデル・プロンプトの比較用）", cmd_ai_eval, ("text", "json"), exclude=False)
    ai_eval.add_argument("--cases", default="eval/ai_cases.toml", help="評価ケースのTOML")
    ai_eval.add_argument("--only", action="append", help="実行するケースのid（複数指定可）")
    ai_eval.add_argument("--repeat", type=int, default=1, help="各ケースを繰り返す回数（出力の揺れを見る）")
    ai_eval.add_argument("--report", help="結果をJSONで保存するパス")
    ai_eval.add_argument("--list", action="store_true", help="ケースの一覧だけ表示する（AIは使わない）")
    ai_eval.add_argument("--min-pass-rate", type=float, default=0.0, help="合格率がこれを下回ったら、終了コード5にする")
    _add_ai_options(ai_eval)

    many = add("explain-many", "複数の関数のAI解説を、並列に生成して書き出す（保存済みは再利用。中断・再実行から再開できる）", cmd_explain_many, ("text",))
    many.add_argument("names", nargs="*", help="関数の名前または修飾名（`名前@行番号` も可）")
    many.add_argument("--from-file", help="名前の一覧（1行に `名前` または `名前<TAB>ファイル`）")
    many.add_argument("--out-dir", help="各解説を書き出すディレクトリ（`<名前>.md`）")
    many.add_argument("--workers", type=int, default=2, help="AIへの並列の問い合わせ数（ローカルLLMは、同時処理が遅いことがあるため、既定は2）")
    many.add_argument("--retries", type=int, default=3, help="一時的な失敗（429・5xx・時間切れ）の再試行を含む試行回数")
    many.add_argument("--no-reuse", action="store_true", help="保存済みの解説を再利用せず、すべて新しく生成する")
    _add_ai_options(many)

    status = add("ai-status", "AIの設定と接続を確認する（ソースコードは送信しない）", cmd_ai_status, ("text", "json"), exclude=False)
    status.add_argument("--ai-base-url")
    status.add_argument("--ai-model")
    status.add_argument("--timeout", type=float)

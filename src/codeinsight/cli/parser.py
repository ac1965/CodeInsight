"""コマンドの登録（argparse）とエントリポイント。"""

from __future__ import annotations

import argparse
import os
import sys

from codeinsight.application import MatchMode
from codeinsight.application.boundary_service import KIND_LABELS as BOUNDARY_LABELS
from codeinsight.application.config_service import KIND_LABELS as CONFIG_LABELS
from codeinsight.application.external_service import CATEGORY_LABELS
from codeinsight.application.graph_builder import Traversal
from codeinsight.application.risk_service import RULES as RISK_RULES
from codeinsight.cli.common import OPENED, CliError
from codeinsight.cli.explore import (
    cmd_analyze,
    cmd_def,
    cmd_deps,
    cmd_describe,
    cmd_overview,
    cmd_path,
    cmd_search,
    cmd_show,
    cmd_status,
    cmd_symbols,
    cmd_trace,
    cmd_tree,
    cmd_tui,
    cmd_unresolved,
    reference_command,
)
from codeinsight.cli.graph import cmd_graph
from codeinsight.cli.project import cmd_architecture, cmd_boundaries, cmd_config, cmd_docs_check, cmd_effects, cmd_environment, cmd_externals, cmd_history, cmd_impact, cmd_tests, cmd_unused
from codeinsight.cli.reading import cmd_dataflow, cmd_exceptions, cmd_flow, cmd_risks, cmd_state, cmd_understand
from codeinsight.domain import SymbolKind

# --- パーサー ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codeinsight",
        description="C言語・Pythonを中心としたコードリーディング支援ソフトウェア",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser, formats: tuple[str, ...] = ("text", "json")) -> None:
        sub.add_argument("--db", help="解析結果DBのパス（既定: ~/.codeinsight/codeinsight.db）")
        sub.add_argument("--project", help="プロジェクトのID・名前・ルートパス（登録が1件なら省略可）")
        sub.add_argument("--format", choices=formats, default=formats[0], help="出力形式")

    def add(
        name: str,
        help_text: str,
        func,
        formats: tuple[str, ...] = ("text", "json"),
        exclude: bool = True,
    ):
        sub = subparsers.add_parser(name, help=help_text)
        common(sub, formats)
        if exclude:
            sub.add_argument(
                "--exclude",
                action="append",
                metavar="GLOB",
                help="結果から除くファイルのパターン（例: 'tests/*'。複数指定可）",
            )
        sub.set_defaults(func=func)
        return sub

    analyze = subparsers.add_parser("analyze", help="プロジェクトを走査・解析し、結果を保存する")
    analyze.add_argument("path", help="解析対象ディレクトリ")
    analyze.add_argument("--db", help="解析結果DBのパス（既定: ~/.codeinsight/codeinsight.db）")
    analyze.add_argument("--compile-commands", help="compile_commands.jsonを含むディレクトリ（既定: 解析対象ルート）")
    analyze.add_argument("--format", choices=("text", "json"), default="text")
    analyze.set_defaults(func=cmd_analyze)

    add("status", "解析状況と、ソース変更による古さを表示する", cmd_status, exclude=False)

    symbols = add("symbols", "保存済みのシンボルを一覧表示する", cmd_symbols)
    symbols.add_argument("--file", help="相対パスで絞り込む")
    symbols.add_argument("--kind", action="append", choices=[k.value for k in SymbolKind], help="種類で絞り込む")

    tree = add("tree", "ディレクトリ・ファイル・シンボルの階層を表示する", cmd_tree)
    tree.add_argument("path", nargs="?", help="表示を絞るディレクトリまたはファイルの相対パス（前方一致）")
    tree.add_argument("--locals", action="store_true", help="ローカル変数も表示する")
    tree.add_argument("--no-variables", action="store_true", help="変数（グローバル/クラス/static）を表示しない")
    tree.add_argument("--depth", type=int, help="ファイルの下に表示するシンボルの階層数（1ならトップレベルのみ）")

    search = add("search", "シンボル・ファイル名・テキストを検索する", cmd_search)
    search.add_argument("query")
    search.add_argument("--files", action="store_true", help="ファイル名を検索する")
    search.add_argument("--text", action="store_true", help="テキスト全文検索（構文解析ではなく文字列一致）")
    search.add_argument("--regex", action="store_true", help="--text で正規表現を使う")
    search.add_argument("-i", "--ignore-case", action="store_true", help="--text で大文字小文字を区別しない")
    search.add_argument("--match", choices=[m.value for m in MatchMode], default="substring")
    search.add_argument("--kind", action="append", choices=[k.value for k in SymbolKind])
    search.add_argument("--file", help="シンボル検索を指定ファイルに限定する")
    search.add_argument("--limit", type=int)

    definition = add("def", "シンボルの定義箇所を表示する", cmd_def)
    definition.add_argument("name", help="名前または修飾名")
    definition.add_argument("--file")

    for name, help_text, mode in (
        ("refs", "シンボルを参照している箇所を表示する", "refs"),
        ("callers", "呼び出し元の一覧を表示する", "callers"),
        ("callees", "呼び出し先の一覧を表示する（未解決・外部を含む）", "callees"),
    ):
        sub = add(name, help_text, lambda a, m=mode: reference_command(a, m))
        sub.add_argument("name", help="名前または修飾名")
        sub.add_argument("--file")
        if mode == "callees":
            sub.add_argument("--hide-external", action="store_true", help="外部（標準/外部ライブラリ等）の呼び出しを除く")

    trace = add("trace", "呼び出し階層を表示する（再帰・未解決を明示）", cmd_trace, ("text",))
    trace.add_argument("name")
    trace.add_argument("--direction", choices=("callees", "callers"), default="callees")
    trace.add_argument("--depth", type=int, default=3)
    trace.add_argument("--external", action="store_true", help="外部（標準/外部ライブラリ等）の呼び出しも表示する")
    trace.add_argument("--file")

    path = add("path", "2つの関数の間の呼び出し経路を検索する", cmd_path)
    path.add_argument("source")
    path.add_argument("target")
    path.add_argument("--max-depth", type=int, default=8)
    path.add_argument("--limit", type=int, default=20)
    path.add_argument("--file")

    deps = add("deps", "ファイル間の依存関係(include/import)を表示する", cmd_deps)
    deps.add_argument("file", nargs="?", help="相対パス（省略時は全体）")
    deps.add_argument("--dependents", action="store_true", help="指定ファイルに依存している側を表示する")
    deps.add_argument("--cycles", action="store_true", help="循環する依存関係を表示する")
    deps.add_argument("--external", action="store_true", help="プロジェクト外への依存も表示する")

    show = add("show", "ソースコードを行番号付きで表示する（ファイル位置またはシンボル名）", cmd_show, ("text",))
    show.add_argument("location", help="FILE[:LINE[-END]] またはシンボル名（定義全体を表示）")
    show.add_argument("--context", type=int, default=3)
    show.add_argument("--file", help="シンボル名が複数に一致する場合の絞り込み")

    describe = add("describe", "シンボルの詳細（宣言・要約・メンバ・呼び出し/参照の件数）を表示する", cmd_describe)
    describe.add_argument("name", help="名前または修飾名")
    describe.add_argument("--file")
    describe.add_argument("--members", type=int, default=30, help="表示するメンバ数の上限")

    add("tui", "端末で構造・ソース・呼び出し関係を行き来する（対話的。保存済みの解析結果を読むだけ）", cmd_tui, ("text",))

    overview = add("overview", "リポジトリの全体像（言語・主要モジュール・エントリポイント・中心となる関数）", cmd_overview)
    overview.add_argument("--top", type=int, default=10, help="各ランキングの表示件数")

    flow = add("flow", "関数の制御構造（分岐・ループ・例外処理・return）と、リトライ/タイムアウトの手がかりを表示する", cmd_flow)
    flow.add_argument("name", help="関数・メソッドの名前または修飾名")
    flow.add_argument("--file")

    dataflow = add("dataflow", "変数の定義・使用と、値の行き先（代入・呼び出し引数・戻り値・状態）をたどる", cmd_dataflow, ("text",))
    dataflow.add_argument("name", help="関数・メソッドの名前または修飾名")
    dataflow.add_argument("variable", nargs="?", help="変数名（省略時は変数の一覧）")
    dataflow.add_argument("--depth", type=int, default=2, help="呼び出し先・代入先をたどる深さ")
    dataflow.add_argument("--upstream", action="store_true", help="引数に渡される実引数を、呼び出し元から調べる")
    dataflow.add_argument("--file")

    state = add("state", "クラスの属性（self.<属性>）・モジュール変数の書き込み/変更/読み取りを表示する", cmd_state)
    state.add_argument("name", help="クラスまたはモジュールの名前または修飾名")
    state.add_argument("--file")

    exceptions = add("exceptions", "関数から出うる例外（明示的なraiseと解決済みの呼び出しをたどる）と握りつぶしを表示する", cmd_exceptions)
    exceptions.add_argument("name", help="関数・メソッドの名前または修飾名")
    exceptions.add_argument("--depth", type=int, default=4)
    exceptions.add_argument("--file")

    risks = add("risks", "潜在的な問題の手がかり（例外の握りつぶし・eval・shell=True等）を探す", cmd_risks)
    risks.add_argument("--rule", action="append", choices=sorted(RISK_RULES), help="規則で絞り込む")
    risks.add_argument("--min-severity", choices=("low", "medium", "high"), default="low")
    risks.add_argument("--limit", type=int, default=10, help="規則ごとの表示件数")

    externals = add("externals", "外部システム・外部ライブラリとの接続（ネットワーク・DB・ファイル・プロセス等）を分類して表示する", cmd_externals)
    externals.add_argument("--category", action="append", choices=list(CATEGORY_LABELS), help="カテゴリで絞り込む")
    externals.add_argument("--limit", type=int, default=8, help="カテゴリごとの表示件数")

    effects = add("effects", "関数の副作用の候補（外部への入出力）を、直接と呼び出し先を介したものに分けて表示する", cmd_effects, ("text",))
    effects.add_argument("name", help="関数・メソッドの名前または修飾名")
    effects.add_argument("--depth", type=int, default=3)
    effects.add_argument("--file")

    architecture = add("architecture", "コンポーネント構成・層構造・循環・外部連携を表示する（役割は名前による推定）", cmd_architecture)
    architecture.add_argument("--depth", type=int, help="コンポーネントとするディレクトリの深さ（省略時は自動）")

    config = add("config", "設定値（環境変数・コマンドライン引数・設定ファイル・定数）の定義箇所と使われる箇所を表示する", cmd_config)
    config.add_argument("--kind", action="append", choices=list(CONFIG_LABELS), help="種類で絞り込む")
    config.add_argument("--name", help="名前の部分一致で絞り込む")
    config.add_argument("--limit", type=int, default=5, help="1件あたりの使用箇所の表示数")

    boundaries = add("boundaries", "入口と境界（エントリポイント・CLI・HTTP・イベント・スレッド・非同期・キャッシュ）を表示する", cmd_boundaries)
    boundaries.add_argument("--kind", action="append", choices=list(BOUNDARY_LABELS), help="種類で絞り込む")
    boundaries.add_argument("--limit", type=int, default=20, help="種類ごとの表示件数")

    understand = add("understand", "関数の読解カード: 8つの問い（なぜ存在する/誰が呼ぶ/入力/変更/戻り値/影響/失敗時/履歴）に事実で答える", cmd_understand, ("text",))
    understand.add_argument("name", help="関数・メソッドの名前または修飾名")
    understand.add_argument("--depth", type=int, default=3, help="呼び出しをたどる深さ")
    understand.add_argument("--limit", type=int, default=8, help="各項目の表示件数")
    understand.add_argument("--file")

    history = add("history", "変更履歴（シンボル指定）、または変更頻度・同時変更の多いファイル（指定なし）を表示する（Git・読み取り専用）", cmd_history)
    history.add_argument("name", nargs="?", help="シンボル名（省略時はリポジトリ全体の傾向）")
    history.add_argument("--limit", type=int, default=10)
    history.add_argument("--max-commits", type=int, default=2000, help="全体の傾向で調べるコミット数")
    history.add_argument("--file")

    tests = add("tests", "シンボルに届くテスト、または届くテストがないコード(--untested)を表示する", cmd_tests)
    tests.add_argument("name", nargs="?", help="シンボル名")
    tests.add_argument("--untested", action="store_true", help="どのテストからも静的に届かない関数・クラスを一覧する")
    tests.add_argument("--depth", type=int, default=3)
    tests.add_argument("--min-lines", type=int, default=1, help="--untested で対象にする最小の行数")
    tests.add_argument("--limit", type=int, default=30)
    tests.add_argument("--file")

    impact = add("impact", "シンボルを変更したときの影響範囲（利用者側への波及・入口・テスト）を表示する", cmd_impact)
    impact.add_argument("name", help="名前または修飾名")
    impact.add_argument("--depth", type=int, default=4)
    impact.add_argument("--limit", type=int, default=20)
    impact.add_argument("--file")

    unused = add("unused", "どこからも参照されていないシンボルの候補を、確度つきで表示する", cmd_unused)
    unused.add_argument("--min-confidence", choices=("high", "medium", "low"), default="medium", help="表示する最低の確度")
    unused.add_argument("--limit", type=int, default=20)

    environment = add("environment", "実行環境の前提（Pythonのバージョン・依存・OS分岐・外部コマンド）を表示する", cmd_environment)
    environment.add_argument("--limit", type=int, default=15)

    docs_check = add("docs-check", "文書の識別子・オプション・環境変数と、実装の差を探す（仕様と実装のずれの手がかり）", cmd_docs_check)
    docs_check.add_argument("--limit", type=int, default=20)

    from codeinsight.cli import ai_commands

    ai_commands.register(add)
    from codeinsight.cli import dynamic_commands, report

    dynamic_commands.register(add)
    report.register(subparsers)

    unresolved = add("unresolved", "静的に確定できなかった参照・依存関係を理由別に表示する", cmd_unresolved)
    unresolved.add_argument("--limit", type=int, default=10, help="理由ごとに表示する件数（既定: 10）")
    unresolved.add_argument("--all", action="store_true", help="全件を表示する")

    graph = add("graph", "グラフを出力する（Mermaid / DOT / JSON / 自己完結HTML）", cmd_graph, ("mermaid", "dot", "json", "html"))
    graph.add_argument("kind", choices=("call", "deps", "inherit", "flow", "arch"), help="呼び出し / ファイル依存 / 継承 / 関数の制御フロー(--rootが必須) / コンポーネント間のアーキテクチャ")
    graph.add_argument("--root", help="起点のシンボル名（deps は相対パス）。指定すると部分グラフを出力")
    graph.add_argument("--depth", type=int, help="起点からの深さ（archではコンポーネントとするディレクトリの深さ）")
    graph.add_argument("--direction", choices=[t.value for t in Traversal], default="both")
    graph.add_argument("--external", action="store_true", help="プロジェクト外への関係も含める")
    graph.add_argument("--no-unresolved", action="store_true", help="未解決の関係を含めない")
    graph.add_argument("--file")
    graph.add_argument("-o", "--output", help="出力先ファイル（省略時は標準出力）")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return exc.code
    except BrokenPipeError:
        # `codeinsight ... | head` のように出力先が先に閉じた場合は、静かに終了する。
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
    finally:
        while OPENED:
            OPENED.pop().close()

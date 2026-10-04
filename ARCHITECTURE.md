# ARCHITECTURE

CodeInsightのシステム構成と依存関係を示す。論理的な責務分割は [AGENTS.md §5](AGENTS.md#5-システムアーキテクチャ) に定義されており、本書は現在の実装（Phase 1〜3）がそれをどう具体化したかを記述する。

## モジュール構成

```
src/codeinsight/
├── domain/            GUI・DB・LLMに依存しないデータモデル（dataclass / enum）
│   ├── project.py          Project, ProjectConfiguration
│   ├── source_file.py      SourceFile, Language, AnalysisFileStatus, FileFreshness
│   ├── symbol.py           Symbol, SymbolKind
│   ├── location.py         SourceLocation, Confidence（確定/推定）
│   ├── reference.py        Reference, ReferenceKind, ResolutionStatus
│   ├── dependency.py       Dependency, DependencyKind
│   └── analysis_result.py  AnalysisResult, AnalysisStatus
│
├── analysis/          静的解析（決定論的な処理のみ。AIには委ねない）
│   ├── language.py          拡張子ベースの言語識別
│   ├── language_adapter.py  LanguageAdapter Protocol, SourceUnit, FileAnalysis
│   ├── ids.py               決定的なシンボル/参照/依存関係IDの払い出し
│   ├── c_analyzer.py        CAnalyzer（libclang）: シンボル・呼び出し・参照・include
│   ├── python_analyzer.py   PythonAnalyzer（標準ast）: シンボル・呼び出し・継承・import・型推定
│   ├── symbol_extractor.py  言語ごとのアダプターを呼び分ける調整役
│   ├── reference_resolver.py プロジェクト横断の参照・依存関係の解決
│   ├── call_graph.py        CallGraph（呼び出し階層・経路検索）、強連結成分（循環検出）
│   ├── flow_analysis.py     関数/クラス単位の制御フロー・例外・変数の定義使用・状態変化（Python AST、問い合わせ時に実行）
│   ├── c_flow_analysis.py   Cの関数単位の制御フロー・データフロー・状態・終了経路・リスク（Clang AST、問い合わせ時に実行）
│   └── fingerprint.py       解析ロジックの指紋（解析器バージョンに含める）
│
├── application/       ユースケースの実行
│   ├── project_manager.py       プロジェクトの登録・取得・削除・一覧
│   ├── analysis_coordinator.py  走査→解析→保存→解決の一連の処理
│   ├── project_index.py         解析結果をメモリに読み込んだ検索・ナビゲーション用の索引
│   ├── search_service.py        シンボル/ファイル名/テキストの検索
│   ├── navigation_service.py    定義・参照・呼び出し関係・依存関係・ソース表示
│   ├── freshness_service.py     解析結果と現在のソースの一致判定（古さの検出）
│   ├── describe_service.py      シンボルの詳細（宣言・要約・メンバ・件数）
│   ├── overview_service.py      リポジトリの全体像
│   ├── c_flow_service.py      Cの関数単位の解析（制御フロー・データフロー・状態・終了経路・リスク。Clang AST）
│   ├── flow_service.py          制御フロー・データフロー・状態・例外経路（ソースの鮮度を確認して実行）
│   ├── risk_service.py          潜在的な問題の手がかり
│   ├── external_service.py      外部連携の分類・副作用の候補
│   ├── external_findings_service.py 外部ツール（SARIF）の指摘の取り込みと、古さの判定
│   ├── architecture_service.py  コンポーネント・層構造・循環・層の逆向き依存の候補
│   ├── config_service.py        設定値（環境変数・CLI引数・設定ファイル・定数）
│   ├── boundary_service.py      入口と境界（CLI・HTTP・イベント・スレッド・非同期・キャッシュ）
│   ├── test_map_service.py      テストとの対応・テストの無いコード
│   ├── impact_service.py        影響範囲
│   ├── unused_service.py        未使用コードの候補
│   ├── history_service.py       Git履歴（変更履歴・変更頻度・同時変更）
│   ├── environment_service.py   実行環境の前提
│   ├── spec_check_service.py    文書と実装の差の手がかり
│   ├── understand_service.py    読解カード（8つの問いに沿って上記を集約）
│   ├── source_scan.py           ソース走査の共通部品（解析後の変更を検出して対象外にする）
│   ├── paths.py                 テストパスの判定
│   ├── cfg_builder.py           関数の制御フロー図の組み立て
│   └── graph_builder.py         グラフモデル（呼び出し/ファイル依存/継承/アーキテクチャ）の組み立て
│
├── infrastructure/    ファイル・Git・永続化との接続
│   ├── file_scanner.py        走査、.gitignore尊重、既定除外、symlink安全化
│   ├── git_repository.py      Gitリポジトリ識別・リビジョン・行範囲/ファイルの履歴（読み取り専用）
│   ├── analysis_repository.py SQLiteへの永続化（トランザクション、スキーマ移行）
│   ├── schema.py              スキーマ定義とバージョン
│   └── config.py              データ保存先の解決
│
├── presentation/      表示・出力形式
│   ├── graph_export.py    Mermaid / DOT / JSON への出力
│   ├── html_viewer.py     自己完結型のローカルHTMLビューアー
│   ├── reading_report.py  make reading の成果物を1つの印刷用HTMLにまとめる（エスケープ・CSP・図のSVG埋め込み・打ち切りの明示）
│   ├── pdf_export.py      HTMLをヘッドレスのブラウザでPDFにする（PDFの完成を監視し、終了しないブラウザを止める）
│   ├── structure_view.py  ディレクトリ・ファイル・シンボルの階層表示
│   ├── labels.py          解決状態の表示ラベル（CLI・TUIで共用）
│   ├── tui_model.py       TUIの状態とキー操作（cursesに依存しない）
│   ├── tui_view.py        TUIの画面の組み立て（描画命令を返す純粋関数）
│   └── tui.py             cursesによる入出力（薄い層）
│
├── dynamic/           動的解析（スタブ。設計は DYNAMIC_ANALYSIS.md。対象を実行するコードを持たない）
│   ├── permission.py      許可モデル（既定は拒否。--allow-run とコマンドの明示が揃うまで実行しない）
│   ├── sandbox.py         隔離の方針（既定は最も厳しい）とバックエンドの確認
│   ├── collectors.py      収集器の一覧（言語別・すべて未実装）
│   └── service.py         plan（実行しない計画）/ run（許可の確認のみ。実行は未実装）
│
├── ai/                AI解説（解析結果を入力に、解説を生成・検証する。解析器の代替にはしない）
│   ├── config.py          AIConfig（送信の許可・送信先・APIキーの秘匿）、設定の解決（コマンドライン>環境変数>設定ファイル）
│   ├── provider.py        AIProvider Protocol、OpenAICompatibleProvider（標準ライブラリのみ。Ollama等）
│   ├── context.py         ContextBuilder（解析結果の事実とソースを予算内で組み立て、引用してよい位置を記録）
│   ├── retrieval.py       質問に関連するシンボルの検索（名前・パス・docstring・本文の語をBM25風に採点。日本語は用語辞書で英語の語に展開）
│   ├── prompt.py          PromptBuilder（規則・根拠・課題の組み立て）
│   ├── citations.py       CitationValidator（引用・識別子・根拠のない主張の機械的な検証）
│   ├── service.py         ExplanationService（同意の確認→根拠→生成→検証→別テーブルへ保存。explain_symbols: 問い合わせだけ並列・保存済みの再利用・再試行・打ち切り）
│   └── evaluation.py      評価ケース（eval/ai_cases.toml）の読み込み・実行・機械的な採点（保存はしない。モデル・プロンプトの比較用）
│
├── (リポジトリ直下) eval/ai_cases.toml  AI評価ケース、.github/workflows/ci.yml  CI（pytest 3.11〜3.13・ruff・mypy）、Makefile、LICENSE（GPL-3.0-or-later）
│
├── bootstrap.py       標準の解析アダプターの組み立て（CLI・将来のGUIで共用）
└── cli/               コマンドラインインターフェース（責務ごとのモジュール。依存は common ← project ← reading、explore・graph・ai_commands は common のみ、parser が全てを束ねる）
    ├── common.py          共通部品（エラー・出力・プロジェクト選択・表示の整形）
    ├── explore.py         探索・参照（tui / analyze / status / symbols / tree / search / def / refs / callers / trace / path / deps / show / describe / overview / unresolved）
    ├── project.py         プロジェクト全体の洞察（externals / effects / architecture / config / boundaries / environment / docs-check / history / tests / impact / unused）
    ├── reading.py         関数の読解（flow / dataflow / state / exceptions / risks / understand）
    ├── reading_c.py       Cの関数の読解コマンド（flow / dataflow / exceptions / state）の表示
    ├── report.py          reading-report（成果物を1ファイルのPDF/HTMLにまとめる）
    ├── dynamic_commands.py 動的解析のコマンド（dynamic-plan / dynamic-run。スタブ）
    ├── graph.py           グラフ出力（graph）
    ├── ai_commands.py     AI解説（explain / explain-file / explain-path / ask / explanations / ai-status / ai-eval）
    ├── parser.py          コマンドの登録（argparse）とエントリポイント `main`
    └── __main__.py        `python -m codeinsight.cli`
```

AI層（`ai/`）は、解析基盤（domain・application）の結果を入力として解説を生成・検証する。解析基盤はAI層に依存せず、AIを使わない機能は、AI層が無くても（AIに接続できなくても）動作する。

## 依存方向

```
cli ──> ai ──────────> application
 │       │                 ├─> analysis ──> domain
 │       └─> infrastructure └─> infrastructure ──> domain
 ├─> presentation ──> application
 └─> application
```

* `domain` はどのレイヤーにも依存しない。
* `analysis` と `infrastructure` は `domain` にのみ依存し、互いには依存しない。
* `application` が `analysis` と `infrastructure` を組み合わせてユースケースを実現する。
* `presentation` は `application` が組み立てたグラフモデル・索引を表示形式へ変換する。
* `cli` は上記を呼び出す。

循環依存は存在しない。

設計判断：`application` は `AnalysisRepository`（SQLite実装）を直接参照している。リポジトリのProtocol化も検討したが、現時点で実装は1つだけで切り替える予定もなく、AGENTS.md §10.9（必要のない抽象化の禁止）に照らして見送った。DBを差し替える必要が生じた時点で導入する。

## 主要なデータフロー

### 解析（`codeinsight analyze`）

1. `ProjectManager.get_or_register` がプロジェクトを取得/登録する。
2. `AnalysisCoordinator.analyze_project` が `GitRepository` で現在のリビジョンを取得し、`FileScanner` でファイルを列挙する。以降の保存は1トランザクションで行う。
3. 存在しなくなったファイルの解析結果を削除する。
4. ファイルごとに、内容を1回だけ読み込み（ハッシュ計算と構文解析に同じバイト列を使う）、内容ハッシュと解析器バージョンが前回と同じならスキップする。そうでなければ `SymbolExtractor` が `LanguageAdapter` に解析を委譲する。
5. 解析器は `FileAnalysis`（シンボル・参照・依存関係・警告・エラー）を返す。この段階の参照先は、ファイル内で分かる範囲の「照合キー」（C: libclangのUSR、Python: 修飾名候補）のみを持つ。
6. 成功したファイルは結果を洗い替えて保存する。失敗したファイル（読み込み不能・構文エラー・解析器の例外）は `failed` として記録し、古い解析結果を削除して処理を継続する。
7. `ReferenceResolver` がプロジェクト全体のシンボル表で参照・依存関係を解決し、解決状態・確からしさ・理由を保存する。
8. 全体の実行結果を `AnalysisResult`（解析器バージョン・リビジョン・警告・エラー）として保存する。

### 参照（`search` / `def` / `refs` / `callers` / `trace` / `deps` / `graph` など）

1. `AnalysisRepository` から `ProjectIndex` へ解析結果を読み込む。
2. `FreshnessService` が現在のファイルのハッシュと解析時のハッシュを比較し、変更されたファイルがあれば警告を出す。
3. `NavigationService` / `SearchService` / `GraphBuilder` が結果を返し、`presentation` と `cli` が整形して出力する。

## 保存データ（SQLite、スキーマバージョン6）

| テーブル | 内容 |
|---|---|
| `projects` | プロジェクトと解析設定、直近のリビジョン |
| `source_files` | ファイル、内容ハッシュ、解析状態、解析器バージョン |
| `symbols` | シンボル（種類・位置・親子関係・USR・docstring/コメントの先頭行） |
| `references_` | 参照（呼び出し・継承・import等）と、解決状態・確からしさ・理由・根拠位置 |
| `dependencies` | ファイル/モジュール間の依存（include/import）と、解決状態・根拠位置 |
| `analysis_results` | 解析実行の履歴（解析器バージョン、リビジョン、警告、エラー） |
| `external_findings` | 外部ツール（SARIF）の指摘（**解析結果とは別管理**）。ツール名・版・規則名・水準・位置・取り込み時のファイルの内容ハッシュ・取り込んだSARIFのハッシュ |
| `explanations` | AI解説（**解析結果とは別管理**）。モデル・プロンプトと入力のハッシュ・根拠にしたファイルの内容ハッシュ・検証結果・検証状態 |

スキーマには `PRAGMA user_version` でバージョンを持たせ、Phase 1のDB（バージョン未設定）は開く際に列・テーブルを追加して移行する。移行したDBは解析器バージョンが空になるため、次回の解析で全ファイルが再解析される。新しいバージョンのDBは開かない。

AIの説明文は、解析結果とは別に管理する方針（AGENTS.md §1.2-2）であり、専用の `explanations` テーブルに保存する。解析結果のテーブル（シンボル・参照・依存関係）には書き込まず、再解析でも解説は消えない（根拠にしたファイルが変わると「古い解説」と示す）。

* v6: `external_findings`（`import-sarif` で取り込んだ外部ツールの指摘。再解析でも消えず、ファイルが変わると「古い」と示す）。
* v5: `projects.compile_commands_dir`（解析時に使った compile_commands.json の場所。Cの関数単位の解析が、問い合わせ時に同じ設定で再解析するため）。旧版のDBは開いた時に列を追加して移行する。

## 非侵襲性の実装

* `GitRepository` は `git rev-parse` などの読み取り専用コマンドのみを実行する。
* `AnalysisRepository` のDBファイルは、既定では対象リポジトリの外部（`~/.codeinsight/codeinsight.db`、`CODEINSIGHT_DATA_DIR`で変更可）に保存され、対象リポジトリ内には一切書き込まない。
* 対象プログラムのビルド・実行は一切行わない。
* ソース表示（`show`）は、解析済みファイルとして登録されたプロジェクト内のパスのみを読み、プロジェクト外を指すパスは読まない。
* 解析対象由来の文字列（シンボル名・診断メッセージ・ソース行）は、CLIでは端末の制御文字を無害化して出力し、HTMLビューアーでは `textContent` のみで表示する（AGENTS.md §4.4: リポジトリ内のソースコードを信頼できない入力として扱う）。

## 問い合わせ時の解析

制御フロー・データフロー・状態・例外・設定値・境界・リスクは、保存済みの解析結果（シンボルの位置・解決済みの呼び出し）と、**問い合わせ時に読み込む現在のソースのAST**を組み合わせる。解析結果にASTや派生データを保存しないため、スキーマは小さく保たれ、解析器の改善が過去の結果に残らない。引き換えに、ソースが解析後に変更されていると位置が対応しないため、各サービスはファイルの内容ハッシュを確認し、異なる場合は実行しない（一括走査では対象外にして示す）。
走査コストが大きいサービス（設定値・境界・リスク）は、対象ファイル・対象の文字列を、構文解析の前に絞り込める（`understand` は、対象のシンボルを含むファイルだけを構文解析する）。

サービス間の依存は、`understand_service` が他のサービスを集約する一方向で、サービス同士が互いに依存する循環はない。`analysis/flow_analysis.py` はドメインにもインフラにも依存しない純粋なAST解析で、サービス層から呼ばれる。

## AI解説の依存と安全性

* `ai/` は `application`・`infrastructure`・`domain` に依存し、逆の依存はない。`ai/service.py` が、`application` のサービス（読解カード・呼び出し経路・検索）の結果を `ContextBuilder` 経由で根拠にする。
* 既定では、何も外部へ送信しない。送信には明示的な許可（`--allow-send` または設定）が必要で、送信先がこの計算機の外の場合は、さらに `--allow-remote` が必要（`AIConfig.check_consent`）。`--dry-run` は送信せず、プロンプトを表示する。
* APIキーは、環境変数 `CODEINSIGHT_AI_API_KEY` からのみ読む。コマンドライン引数（シェルの履歴に残る）でも設定ファイル（平文で残る）でも受け取らない。設定ファイルに `api_key` があっても無視し、`ai-status` が警告する。`repr`・表示・例外メッセージには含めない。
* 設定ファイル `~/.codeinsight/config.toml` の `[ai]` テーブル（`CODEINSIGHT_DATA_DIR` で場所を変更可）には、送信先・モデル・送信の許可などを書ける（APIキーは除く）。

`ai/evaluation.py` は `ai/service.py` を使う側であり、逆の依存はない。評価は一時DBに対象を解析して行い、評価対象のリポジトリにも利用者のDBにも書き込まない。

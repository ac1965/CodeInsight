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
│   └── fingerprint.py       解析ロジックの指紋（解析器バージョンに含める）
│
├── application/       ユースケースの実行
│   ├── project_manager.py       プロジェクトの登録・取得・削除・一覧
│   ├── analysis_coordinator.py  走査→解析→保存→解決の一連の処理
│   ├── project_index.py         解析結果をメモリに読み込んだ検索・ナビゲーション用の索引
│   ├── search_service.py        シンボル/ファイル名/テキストの検索
│   ├── navigation_service.py    定義・参照・呼び出し関係・依存関係・ソース表示
│   ├── freshness_service.py     解析結果と現在のソースの一致判定（古さの検出）
│   └── graph_builder.py         グラフモデル（呼び出し/ファイル依存/継承）の組み立て
│
├── infrastructure/    ファイル・Git・永続化との接続
│   ├── file_scanner.py        走査、.gitignore尊重、既定除外、symlink安全化
│   ├── git_repository.py      Gitリポジトリ識別・リビジョン取得（読み取り専用）
│   ├── analysis_repository.py SQLiteへの永続化（トランザクション、スキーマ移行）
│   ├── schema.py              スキーマ定義とバージョン
│   └── config.py              データ保存先の解決
│
├── presentation/      表示・出力形式
│   ├── graph_export.py    Mermaid / DOT / JSON への出力
│   ├── html_viewer.py     自己完結型のローカルHTMLビューアー
│   └── structure_view.py  ディレクトリ・ファイル・シンボルの階層表示
│
├── bootstrap.py       標準の解析アダプターの組み立て（CLI・将来のGUIで共用）
└── cli.py             コマンドラインインターフェース
```

AI層（AIProvider、PromptBuilder、ContextBuilder、CitationValidator）はPhase 4で実装する。現時点ではAIに依存する機能はない。

## 依存方向

```
cli
 ├─> presentation ──> application
 └─> application
      ├─> analysis ──> domain
      └─> infrastructure ──> domain
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

## 保存データ（SQLite、スキーマバージョン2）

| テーブル | 内容 |
|---|---|
| `projects` | プロジェクトと解析設定、直近のリビジョン |
| `source_files` | ファイル、内容ハッシュ、解析状態、解析器バージョン |
| `symbols` | シンボル（種類・位置・親子関係・USR） |
| `references_` | 参照（呼び出し・継承・import等）と、解決状態・確からしさ・理由・根拠位置 |
| `dependencies` | ファイル/モジュール間の依存（include/import）と、解決状態・根拠位置 |
| `analysis_results` | 解析実行の履歴（解析器バージョン、リビジョン、警告、エラー） |

スキーマには `PRAGMA user_version` でバージョンを持たせ、Phase 1のDB（バージョン未設定）は開く際に列・テーブルを追加して移行する。移行したDBは解析器バージョンが空になるため、次回の解析で全ファイルが再解析される。新しいバージョンのDBは開かない。

AIの説明文は、解析結果とは別に管理する方針（AGENTS.md §1.2-2）であり、Phase 4で専用のテーブルを追加する。

## 非侵襲性の実装

* `GitRepository` は `git rev-parse` などの読み取り専用コマンドのみを実行する。
* `AnalysisRepository` のDBファイルは、既定では対象リポジトリの外部（`~/.codeinsight/codeinsight.db`、`CODEINSIGHT_DATA_DIR`で変更可）に保存され、対象リポジトリ内には一切書き込まない。
* 対象プログラムのビルド・実行は一切行わない。
* ソース表示（`show`）は、解析済みファイルとして登録されたプロジェクト内のパスのみを読み、プロジェクト外を指すパスは読まない。
* 解析対象由来の文字列（シンボル名・診断メッセージ・ソース行）は、CLIでは端末の制御文字を無害化して出力し、HTMLビューアーでは `textContent` のみで表示する（AGENTS.md §4.4: リポジトリ内のソースコードを信頼できない入力として扱う）。

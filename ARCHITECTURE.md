# ARCHITECTURE

CodeInsightのシステム構成と依存関係を示す。論理的な責務分割は [AGENTS.md §5](AGENTS.md#5-システムアーキテクチャ) に定義されており、本書はPhase1時点の実装がそれをどう具体化したかを記述する。

## モジュール構成

```
src/codeinsight/
├── domain/           GUI・DB・LLMに依存しないデータモデル（dataclass）
│   ├── project.py         Project, ProjectConfiguration
│   ├── source_file.py     SourceFile, Language, AnalysisFileStatus
│   ├── symbol.py          Symbol, SymbolKind
│   ├── reference.py       Reference, ReferenceKind, ResolutionStatus（Phase2で使用開始）
│   ├── dependency.py      Dependency, DependencyKind（Phase2で使用開始）
│   └── analysis_result.py AnalysisResult, AnalysisStatus
│
├── analysis/          言語別の静的解析（決定論的な解析のみ、AIには委ねない）
│   ├── language.py         拡張子ベースの言語識別
│   ├── language_adapter.py LanguageAdapter Protocol, FileAnalysis
│   ├── c_analyzer.py       CAnalyzer（libclang / clang.cindex）
│   ├── python_analyzer.py  PythonAnalyzer（標準ast）
│   └── symbol_extractor.py 言語ごとのアダプターを呼び分ける調整役
│
├── application/       ユースケースの実行
│   ├── project_manager.py       プロジェクトの登録・削除・一覧
│   └── analysis_coordinator.py  走査→解析→保存の一連の処理
│
├── infrastructure/    ファイル・Git・永続化との接続
│   ├── file_scanner.py       走査、.gitignore尊重、既定除外、symlink安全化
│   ├── git_repository.py     Gitリポジトリ識別・リビジョン取得（読み取り専用）
│   ├── analysis_repository.py SQLiteへの永続化
│   └── config.py              データ保存先の解決
│
└── cli.py             `codeinsight analyze` / `codeinsight symbols`（Phase1確認用の最小限のCLI）
```

Presentation層（ProjectView等のGUI画面）とAI層はPhase2以降で実装する。Phase1はCLIのみで動作を確認できる。

## 依存方向

```
cli
 └─> application
      ├─> analysis ──> domain
      └─> infrastructure ──> domain
```

* `domain` はどのレイヤーにも依存しない。
* `analysis` と `infrastructure` は `domain` にのみ依存し、互いには依存しない。
* `application` が `analysis` と `infrastructure` を組み合わせてユースケースを実現する。
* `cli` は `application` 経由でのみ機能にアクセスする。

循環依存は存在しない。

## 主要なデータフロー（`codeinsight analyze`）

1. `cli._cmd_analyze` が `AnalysisRepository` を開き、`ProjectManager` でプロジェクトを取得/登録する。
2. `AnalysisCoordinator.analyze_project` が `FileScanner` でファイルを列挙する。
3. ファイルごとに `analysis.language.detect_language` で言語を識別し、`SymbolExtractor` が対応する `LanguageAdapter`（`CAnalyzer` / `PythonAnalyzer`）にシンボル抽出を委譲する。
4. 抽出結果（`FileAnalysis`）に含まれるシンボル・警告・エラーを `AnalysisRepository` に永続化する。エラーがあるファイルは `analysis_status=failed` として記録し、正常に解析できたファイルの結果を汚染しない。
5. 全体の実行結果を `AnalysisResult` としてまとめ、保存する。

## 非侵襲性の実装

* `GitRepository` は `git rev-parse` などの読み取り専用コマンドのみを実行する。
* `AnalysisRepository` のDBファイルは、既定では対象リポジトリの外部（`~/.codeinsight/codeinsight.db`、`CODEINSIGHT_DATA_DIR`で変更可）に保存され、対象リポジトリ内には一切書き込まない。
* 対象プログラムのビルド・実行は一切行わない。

# TESTING

テスト方法と実行結果を記録する。

## 実行方法

```bash
uv sync --extra dev
uv run pytest -v
```

`uv` が無い場合は、標準的な仮想環境でも動作する。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

## テスト構成

| ファイル | 件数 | 対象 | 種別 |
|---|---|---|---|
| `test_language_detection.py` | 3 | 拡張子ベースの言語識別 | 単体 |
| `test_python_analyzer.py` | 3 | PythonAnalyzer（モジュール/クラス/関数/デコレータ/構文エラー） | 単体 |
| `test_c_analyzer.py` | 5 | CAnalyzer（関数/構造体/typedef/マクロ/ローカル変数/構文エラー） | 単体 |
| `test_file_scanner.py` | 6 | FileScanner（走査、既定除外、.gitignore、symlink循環・脱出防止） | 単体 |
| `test_analysis_repository.py` | 3 | AnalysisRepository（保存・再読み込み・洗い替え・削除） | 単体 |
| `test_repository_schema.py` | 4 | スキーマバージョン、Phase 1のDBの移行、新しいDBの拒否、トランザクションのロールバック | 単体 |
| `test_project_manager.py` | 3 | ProjectManager（登録・一覧・削除・異常系） | 単体 |
| `test_references.py` | 21 | 参照・依存関係の抽出と解決（下記） | 結合 |
| `test_coordinator_robustness.py` | 10 | 決定的ID、パッケージ階層のモジュール名、失敗/削除ファイルの結果除去、読み込み不能・解析器例外での継続、進捗通知、リビジョン更新、解析器バージョン | 結合 |
| `test_analysis_coordinator_integration.py` | 4 | 登録→走査→解析→保存→再読み込み、エラーファイル混在、再解析スキップ | 結合 |
| `test_navigation.py` | 10 | 検索（モード・種類・ファイル・テキスト）、シンボルの特定と曖昧さ、呼び出し元/先、参照、階層（再帰・深さ制限・未解決）、経路、依存と循環、ソース表示と古さ | 結合 |
| `test_graph_and_views.py` | 8 | グラフ（確定/未解決/外部の区別、部分グラフ、種類別）、Mermaid/DOT/JSON/HTMLの出力とエスケープ、構造ツリー | 結合 |
| `test_cli.py` | 11 | CLIの各コマンド、JSON出力、曖昧なシンボルの終了コード、古さの警告、複数プロジェクトの選択、端末制御文字の無害化 | 結合 |

`test_references.py` が検証する内容：

* C：直接呼び出しの複数ファイルにまたがる解決（定義側へ）、関数ポインタ経由（変数・構造体メンバ）の未解決、関数名の参照（`FUNCTION_REF`）と呼び出しの区別、再帰、外部関数、非アクティブな条件付きコンパイルの扱い、`#include` の解決・外部。
* Python：名前・import経由の解決、`self` / `super()` 経由（推定）、継承、動的呼び出し（`getattr`）の未解決、デコレータ付き呼び出し先（推定）、動的インポートの警告と未解決の依存、循環import、再エクスポートの追跡、star import、型注釈・単一代入・インスタンス属性・生成直後のレシーバーによる型推定（再代入される属性は推定しないこと）、組み込み型・外部ライブラリのメソッドの分類、回帰テスト。

## テスト用ソースコード（`tests/fixtures/`、AGENTS.md §8.3）

| ディレクトリ | 内容 |
|---|---|
| `c_sample/` | 単純な関数呼び出し、複数ファイル、マクロ、条件付きコンパイル（宣言のみ）、構造体、再帰 |
| `c_callgraph/` | 関数ポインタ（変数・構造体メンバ）、関数名の参照、マクロ、非アクティブな条件付きコンパイル、構造体とポインタ、再帰、外部関数、`static` 関数 |
| `python_sample/` | 関数呼び出し、クラス継承、デコレータ、非同期関数 |
| `python_pkg/` | パッケージ、循環import、動的インポート、`getattr`、デコレータによる置換、`self` / `super()`、型注釈・単一代入による型推定、非同期関数 |
| `python_reexport/` | `__init__.py` による再エクスポート、star import、組み込み型・外部ライブラリの型、インスタンス属性の型推定 |

解析できないケースについても、期待される未解決・外部・警告の状態を検証している（AGENTS.md §8.3）。

## 実行結果（最終確認時点）

```
91 passed in 3.02s
```

全テストが成功している。

## 手動確認

### CLIによるエンドツーエンド確認

結果は、対象リポジトリの外の一時DBに保存した。

| 対象 | ファイル | シンボル | 参照 | 結果 |
|---|---|---|---|---|
| `tests/fixtures/c_sample` | 3 | 11 | 7 | success |
| `tests/fixtures/c_callgraph` | 3 | 17 | 15 | success |
| `tests/fixtures/python_pkg` | 5 | 20 | 32 | success |
| `tests/fixtures/python_reexport` | 6 | 20 | 30 | success |

`c_callgraph` に対し、`callees` / `callers` / `trace` / `path` / `deps` / `show` / `search` / `unresolved` / `graph` の各コマンドを実行し、期待どおりの出力（未解決・外部・再帰の明示、静的な関係である旨の注記）を確認した。

### 実プロジェクトでの確認（AGENTS.md §2.3）

[narou_dl](https://github.com/ac1965/narou_dl)（Python）と、CodeInsight自身（自己解析）を対象にした。解析の前後で対象リポジトリに変更が生じていないこと（`git status`）を確認した。

| 対象 | ファイル | シンボル | 参照 | 解決 | 未解決 | 曖昧 | 外部 |
|---|---|---|---|---|---|---|---|
| narou_dl | 33 | 461 | 2124 | 578 | 391 | 0 | 1155 |
| CodeInsight | 72 | 787 | 2919 | 1277 | 600 | 2 | 1040 |

* 精度の確認として、narou_dlの解決済み呼び出し（460件、うち推定125件）から無作為に16件（推定10件・確定6件）を抜き取り、実際のソースと突き合わせた。誤った解決は見つからなかった。ただし、抜き取りは網羅的な精度評価ではない。
* 解析ロジックの改善前後の比較（narou_dl）：未解決 682 → 391、解決 455 → 578（再エクスポート・star import・型推定・組み込み型の分類を追加した結果）。この比較の過程で、解析中の例外（モジュール変数の型推定の不具合）を発見し、そのファイルだけが `failed` として記録され他のファイルの解析が継続されることを確認したうえで、修正と回帰テストを追加した。
* [PownForge](https://github.com/ac1965/PownForge) は、未コミットの変更が多いため検証対象から除外した（AGENTS.md §2.3）。除外を決める前に一度だけ読み取り専用で解析し、解析ロジックの改善点の洗い出しに用いた（リポジトリは変更していない。結果のDBは一時領域のみ）。

### HTMLビューアー

内蔵ブラウザで `graph call --format html` の出力を開き、描画（ノード・辺・凡例、自己再帰のループ）、ノードのクリックによる詳細表示、コンソールエラーが無いことを確認した。

## 既知の制約（テストの範囲外）

* `compile_commands.json` を用いた解析パス（`CAnalyzer(compile_commands_dir=...)` の精密な引数解決）は、実際のビルドシステムとの統合テストを未実施。
* 出力したMermaid・DOTの構文は、パーサーによる機械的な検証をしていない（テストはエスケープと構造の確認まで）。Mermaidは、ラベルの特殊文字を実体参照に置き換えて対策している。
* 性能（大規模リポジトリでの解析時間・メモリ使用量）は評価していない（AGENTS.md §4.3: 根拠のない性能の保証をしない）。検索・ナビゲーションは、プロジェクト全体の解析結果をメモリに読み込む実装であり、大規模なリポジトリでの挙動は未確認。
* GUI・AI解説機能は未実装のため、テスト対象外。

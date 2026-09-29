# TESTING

Phase1（解析基盤）のテスト方法と実行結果を記録する。

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

| ファイル | 対象 | 種別 |
|---|---|---|
| `tests/test_language_detection.py` | 拡張子ベースの言語識別 | 単体 |
| `tests/test_python_analyzer.py` | PythonAnalyzer（モジュール/クラス/関数/デコレータ/構文エラー） | 単体 |
| `tests/test_c_analyzer.py` | CAnalyzer（関数/構造体/typedef/マクロ/ローカル変数/構文エラー） | 単体 |
| `tests/test_file_scanner.py` | FileScanner（走査、既定除外、.gitignore、symlink循環・脱出防止） | 単体 |
| `tests/test_analysis_repository.py` | AnalysisRepository（保存・再読み込み・洗い替え・削除） | 単体 |
| `tests/test_project_manager.py` | ProjectManager（登録・一覧・削除・異常系） | 単体 |
| `tests/test_analysis_coordinator_integration.py` | 登録→走査→解析→保存→再読み込みの一連のフロー、エラーファイル混在時の挙動、再解析スキップ | 結合 |

テスト用ソースコードは `tests/fixtures/c_sample/`（単純な関数呼び出し・複数ファイル・マクロ・条件付きコンパイル・構造体・再帰関数を含む）と `tests/fixtures/python_sample/`（関数呼び出し・クラス継承・デコレータ・非同期関数を含む）に配置している（AGENTS.md §8.3準拠。動的インポート・循環インポート等はPhase2以降のテストで追加する）。

## 実行結果（最終確認時点）

```
27 passed in 0.36s
```

全テストが成功している。

## 手動確認（CLIによるエンドツーエンド確認）

自動テストに加え、以下をCLIで手動確認した（結果はコミットに含まれない一時ディレクトリで実施）。

1. `codeinsight analyze tests/fixtures/c_sample --db <tmp>/c.db` → 3ファイル・12シンボルを抽出し `success` で完了。`codeinsight symbols --db <tmp>/c.db` で保存結果が再読み込みできることを確認。
2. `codeinsight analyze tests/fixtures/python_sample --db <tmp>/py.db` → 2ファイル・14シンボルを抽出し `success` で完了。
3. 正常なPythonファイルと構文エラーのPythonファイルを混在させたディレクトリを解析し、`partial` として完了すること、エラーファイルの内容が `errors` に記録されること、正常ファイルのシンボルは保存されることを確認。

## 既知の制約（テストの範囲外）

* `compile_commands.json` を用いた解析パス（`CAnalyzer(compile_commands_dir=...)` の精密な引数解決）は、警告が出ないことのみを単体テストで簡易的に確認しており、実際のビルドシステムとの統合テストは未実施（Phase2以降で実プロジェクト（PownForge・narou_dl等、[README.md](README.md)参照）を用いた検証を予定）。
* GUI・AI解説機能はPhase1では未実装のため、テスト対象外。

# CHANGELOG

## Phase 1: 解析基盤（未リリース）

### 追加

* プロジェクト管理（登録・一覧・削除、.gitignore尊重、既定除外ディレクトリ/ファイル、symlink安全化）
* C言語解析アダプター（libclang）：関数定義/宣言、構造体/共用体/列挙型、typedef、マクロ、グローバル/static/ローカル変数の抽出
* Python解析アダプター（標準ast）：モジュール/クラス/関数/メソッド/デコレータ/非同期関数/グローバル変数/クラス変数の抽出
* 解析結果のSQLiteへの永続化と再読み込み、content_hashに基づく再解析スキップ
* 解析エラー・警告の記録（構文エラー、compile_commands.json未検出時の精度制約）
* 最小限のCLI（`codeinsight analyze` / `codeinsight symbols`）
* テスト用C/Pythonフィクスチャおよび単体・結合テスト（27件）
* ドキュメント：ARCHITECTURE.md, ANALYSIS.md, TESTING.md, REQUIREMENTS.md

### 既知の制約

* 関数呼び出し関係、import/include依存関係の抽出は未実装（Phase2で対応予定）
* ローカル変数（Python）、インスタンス変数、構造体フィールドの抽出は未実装
* GUI・AI解説機能は未実装（Phase3・4で対応予定）

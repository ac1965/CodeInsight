# CodeInsight

C言語・Pythonを中心としたコードリーディング支援ソフトウェア

## 概要

CodeInsightは、既存ソフトウェアのソースコードを体系的に解析し、開発者がプログラムの構造・処理の流れ・データの依存関係および実装上の設計意図を理解するためのコードリーディング支援ソフトウェアです。

C言語およびPythonを主要な対象言語とし、静的解析によって取得できる**事実**と、AIによる**解釈・説明**を明確に区別して提示します。単なるコード検索やチャット型のコード解説ツールではなく、ソースコード全体を対象に、構造・依存関係・処理フローを横断的に把握できることを目的としています。

詳細な要件・設計方針は [AGENTS.md](AGENTS.md) を参照してください。

## 基本方針

1. **事実と推論の分離** — 静的解析で確認できた事実と、AIが推論した内容を明確に区別する。
2. **ソースコードへのトレーサビリティ** — 解析結果には、可能な限りファイル名・行番号・シンボル名を根拠として付与する。
3. **静的解析とAIの責務の分離** — 構文解析・シンボル抽出・参照関係の取得は決定論的な解析器が担い、AIは要約・説明・仮説の提示に限定する。
4. **既存ソフトウェアの非侵襲的な解析** — 対象リポジトリのソースコードを変更せず、対象プログラムを無断で実行しない。
5. **段階的な拡張** — C言語とPythonを優先し、言語固有の解析と言語非依存の基盤を分離した設計とする。

## 対象ユーザー

* 既存システムの保守・運用担当者
* ソフトウェアの設計・実装を調査する開発者
* 他者が開発したソフトウェアの構造を理解する技術者
* OSSの実装やライブラリの内部動作を調査する技術者
* セキュリティや品質の観点からソースコードを調査する技術者

## 開発状況

**Phase 1〜3(解析基盤・コードナビゲーション・可視化)を実装済みです。** C言語/Pythonのシンボル・呼び出し・継承・import/includeの抽出と解決、検索、定義・参照・呼び出し階層・経路・依存関係の追跡、グラフ出力(Mermaid / DOT / JSON / ローカルHTML)がCLIから使えます。AIによる解説(Phase 4)、データフロー解析(Phase 5)は未実装です(詳細は [AGENTS.md §9](AGENTS.md#9-開発フェーズ)、実装状況は [REQUIREMENTS.md](REQUIREMENTS.md) を参照)。

静的解析で確定できない関係(関数ポインタ、動的な呼び出し等)は推測で確定せず、**未解決**として明示します。候補を静的に特定できるが実行時の挙動で変わりうるものは**推定**として、確定とは区別して表示します(AGENTS.md §3.5.1)。

実践的な検証対象は、以下の開発中リポジトリです(詳細は [AGENTS.md §2.3](AGENTS.md#23-実プロジェクトでの解析対象検証用) を参照)。現在のスコープはC言語・Pythonのため、[narou_dl](https://github.com/ac1965/narou_dl) が対象です。[PownForge](https://github.com/ac1965/PownForge) は未コミットの変更が多いため当面除外しています。Go製の[RiskForge](https://github.com/ac1965/RiskForge)とEmacs Lisp製の[.emacs.d](https://github.com/ac1965/.emacs.d)は将来の言語追加後の対象候補です。

## アーキテクチャ概要

```text
CodeInsight
├── Application     … ユースケースの実行(プロジェクト管理・検索・ナビゲーション・解説)
├── Analysis        … 言語別の静的解析(C/Python)、シンボル抽出、依存関係・呼び出しグラフ解析
├── Domain          … 特定のGUI/DB/LLMに依存しないドメインモデル
├── AI              … 解析結果を利用したコード解説(解析器の代替にはしない)
├── Infrastructure  … ファイル・Git・永続化・キャッシュ・設定
└── Presentation    … プロジェクト/ソースコード/構造・依存関係/AI解説の各画面
```

各層の詳細な責務分割は [AGENTS.md §5](AGENTS.md#5-システムアーキテクチャ)、実装のモジュール構成は [ARCHITECTURE.md](ARCHITECTURE.md) を参照してください。

## セットアップ

```bash
uv sync --extra dev
```

`uv` が無い場合は標準的な仮想環境でも動作します。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

C言語解析にはlibclang(PyPIパッケージに同梱)を使用します。追加のClangインストールは不要です。

## 使い方(CLI)

解析結果は、既定で `~/.codeinsight/codeinsight.db` に保存されます(対象リポジトリには書き込みません。`--db` または環境変数 `CODEINSIGHT_DATA_DIR` で変更できます)。登録したプロジェクトが複数ある場合は `--project`(ID・名前・ルートパス)で指定します。

```bash
# プロジェクトを走査・解析し、結果を保存する
uv run codeinsight analyze /path/to/target-repo

# 解析状況と、解析後にソースが変更されたファイルを確認する
uv run codeinsight status
```

### 調べる

```bash
uv run codeinsight tree                         # ディレクトリ・ファイル・シンボルの階層
uv run codeinsight search fib                   # シンボル検索(--match exact|prefix|substring, --kind, --file)
uv run codeinsight search "fib(" --text         # テキスト全文検索(--regex, -i)。構文解析ではなく文字列一致
uv run codeinsight def main                     # 定義箇所
uv run codeinsight refs add                     # 参照箇所
uv run codeinsight callers fib                  # 呼び出し元
uv run codeinsight callees main                 # 呼び出し先(未解決・外部を含む)
uv run codeinsight trace main --depth 3         # 呼び出し階層(再帰・未解決を明示)
uv run codeinsight path main fib                # 呼び出し経路の検索
uv run codeinsight deps main.c                  # ファイルの依存関係(--dependents, --cycles, --external)
uv run codeinsight show ops.c:16-21             # 行番号付きでソースを表示
uv run codeinsight unresolved                   # 静的に確定できなかった参照・依存関係
```

名前が複数のシンボルに一致する場合は、候補を表示して終了します(修飾名か `--file` で絞り込みます)。多くのコマンドは `--format json` に対応しています。静的な呼び出し関係は、実行順序や実際に通る経路を示すものではない点に注意してください。

### グラフを出力する

```bash
uv run codeinsight graph call --root main --depth 2 --format mermaid
uv run codeinsight graph deps --format dot -o deps.dot
uv run codeinsight graph inherit --format json
uv run codeinsight graph call --format html -o call.html   # 自己完結型のローカルビューアー(外部通信なし)
```

`graph` の種類は `call`(呼び出し)・`deps`(ファイル依存)・`inherit`(継承)。`--root`(起点)・`--depth`・`--direction out|in|both` で部分グラフに絞れます。実線は確定、破線は推定、点線は未解決・外部の関係です。

`compile_commands.json` が解析対象ディレクトリにある場合は自動的に利用されます。無い場合は最小限のデフォルト引数で解析し、その旨を警告として表示します(詳細は [ANALYSIS.md](ANALYSIS.md))。

## テスト

```bash
uv run pytest -v
```

詳細は [TESTING.md](TESTING.md) を参照してください。

## ドキュメント

* [AGENTS.md](AGENTS.md) — 開発目的・機能要件・非機能要件・アーキテクチャ・開発フェーズを含む開発指示書
* [ARCHITECTURE.md](ARCHITECTURE.md) — 実装したモジュール構成と依存関係
* [ANALYSIS.md](ANALYSIS.md) — 解析方式と既知の制約
* [REQUIREMENTS.md](REQUIREMENTS.md) — 要件に対する実装状況の要約
* [TESTING.md](TESTING.md) — テスト方法と実行結果
* [CHANGELOG.md](CHANGELOG.md) — 変更履歴

## ライセンス

未定

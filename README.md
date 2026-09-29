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

**Phase 1(解析基盤)を実装済みです。** プロジェクト管理・ファイル走査・C言語/Pythonのシンボル抽出・解析結果の永続化・基本的なテスト(27件)が動作します。関数呼び出しやimport/include等の依存関係抽出、検索、可視化、AI解説は未実装で、Phase 2以降で対応します(詳細は [AGENTS.md §9](AGENTS.md#9-開発フェーズ)、実装状況の詳細は [REQUIREMENTS.md](REQUIREMENTS.md) を参照)。

実践的な検証対象として、以下の開発中リポジトリの解析にも対応する予定です(詳細は [AGENTS.md §2.3](AGENTS.md#23-実プロジェクトでの解析対象検証用) を参照)。現在のスコープはC言語・Pythonのため、[PownForge](https://github.com/ac1965/PownForge)・[narou_dl](https://github.com/ac1965/narou_dl)のPython部分が対象で、Go製の[RiskForge](https://github.com/ac1965/RiskForge)とEmacs Lisp製の[.emacs.d](https://github.com/ac1965/.emacs.d)は将来の言語追加後の対象候補です。

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

各層の詳細な責務分割は [AGENTS.md §5](AGENTS.md#5-システムアーキテクチャ)、Phase1実装のモジュール構成は [ARCHITECTURE.md](ARCHITECTURE.md) を参照してください。

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

```bash
# プロジェクトを走査・解析し、結果を保存する(既定のDB: ~/.codeinsight/codeinsight.db)
uv run codeinsight analyze /path/to/target-repo

# 保存済みのシンボルを一覧表示する
uv run codeinsight symbols --db ~/.codeinsight/codeinsight.db
```

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

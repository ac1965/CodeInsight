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

**Phase 1〜3(解析基盤・コードナビゲーション・可視化)に加え、コードリーディングのための関数単位の解析(制御フロー・データフロー・状態・例外・設定値・境界・影響範囲・履歴など)を実装済みです。** C言語/Pythonのシンボル・呼び出し・参照・依存の抽出と解決、検索、グラフ出力(Mermaid / DOT / JSON / ローカルHTML)、そして関数について「なぜ存在するか / 誰が呼ぶか / 入力 / 変更 / 戻り値 / 影響 / 失敗時 / なぜ今の実装か」の8つの問いに事実で答える読解カード(`understand`)が、CLIから使えます。AIによる解説(Phase 4)も実装済みで(下記「AIで解説する」)、動的解析(実行観測)は未実装です。実装状況の詳細と制約は [REQUIREMENTS.md](REQUIREMENTS.md) と [ANALYSIS.md](ANALYSIS.md) を参照してください。

* 静的に確定できない関係(関数ポインタ、動的な呼び出し等)は推測で確定せず、**未解決**として明示します。候補を特定できるが実行時の挙動で変わりうるものは**推定**として、確定と区別します(AGENTS.md §3.5.1)。
* 制御フロー・データフロー・状態・例外・設定値・境界の解析は**Python**が対象です(Cは呼び出し・参照・依存・外部連携・履歴・テストまで)。データフローは関数単位・流れ非依存の**近似**です。
* 設計判断の「理由」はコードから確認できないため推測せず、変更履歴・コメント・文書という手がかりを示します。
* 対象リポジトリのソースは変更せず、プログラムも実行しません(Gitは読み取り専用)。
* AIには**既定では何も送信しません**。送信は利用者の明示的な許可が必要で、ローカルLLM(Ollama等)ならこの計算機の中で完結します。AIの解説は、解析結果(事実)とは別に管理し、引用を機械的に検証したうえで、根拠を確認できない記述を「未確認」と示します。

実践的な検証対象は、以下の開発中リポジトリです(詳細は [AGENTS.md §2.3](AGENTS.md#23-実プロジェクトでの解析対象検証用) を参照)。現在のスコープはC言語・Pythonのため、[narou_dl](https://github.com/ac1965/narou_dl) が対象です。[PownForge](https://github.com/ac1965/PownForge) は未コミットの変更が多いため当面除外しています。Go製の[RiskForge](https://github.com/ac1965/RiskForge)とEmacs Lisp製の[.emacs.d](https://github.com/ac1965/.emacs.d)は将来の言語追加後の対象候補です。

## アーキテクチャ概要

```text
CodeInsight
├── Application     … ユースケースの実行(プロジェクト管理・検索・ナビゲーション・解説)
├── Analysis        … 言語別の静的解析(C/Python)、シンボル抽出、参照解決、呼び出しグラフ、制御フロー・データフロー(Python)
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

よく使う操作は `make` から実行できます(`make help` で一覧)。

```bash
make setup                              # 依存のインストール
make check                              # 構文の確認とテスト
make analyze TARGET=../my-repo          # 解析(対象は変更しない)
make overview                           # 全体像(DB=・PROJECT= で解析結果を指定)
make understand NAME=main               # 関数の読解カード
make explain-dry NAME=main MODEL=qwen3-coder:latest   # AIへ送る内容の確認(送信しない)
make explain NAME=main MODEL=qwen3-coder:latest AI_SEND=1   # AI解説(送信の許可が必要)
```

## 使い方(CLI)

解析結果は、既定で `~/.codeinsight/codeinsight.db` に保存されます(対象リポジトリには書き込みません。`--db` または環境変数 `CODEINSIGHT_DATA_DIR` で変更できます)。登録したプロジェクトが複数ある場合は `--project`(ID・名前・ルートパス)で指定します。多くのコマンドは `--format json` と、表示から除くパスを指定する `--exclude 'tests/*'` に対応します。

```bash
# プロジェクトを走査・解析し、結果を保存する
uv run codeinsight analyze /path/to/target-repo

# 解析状況と、解析後にソースが変更されたファイルを確認する
uv run codeinsight status
```

解析後にソースを変更した場合、読み取り系のコマンドは「解析後に変更されています」と警告します(行番号などが古い可能性)。ソースの内容を読む解析(`flow`・`dataflow`・`understand` など)は、位置のずれによる誤りを避けるため、変更されたファイルでは実行されません。再解析(`analyze`)してください。

### 初めて扱うリポジトリを把握する

```bash
uv run codeinsight overview             # 言語・主要モジュール・エントリポイント・中心となる関数・循環
uv run codeinsight architecture         # コンポーネント構成・層構造・循環・外部連携(役割名は名前による推定)
uv run codeinsight boundaries           # 入口と境界(CLI・HTTP・イベント・スレッド・非同期・キャッシュ)
uv run codeinsight environment          # 実行環境の前提(Pythonの版・依存・OS分岐・外部コマンド)
uv run codeinsight config               # 設定値(環境変数・CLI引数・設定ファイル・定数)と使われる箇所
uv run codeinsight externals            # 外部連携(ネットワーク・DB・ファイル・プロセス等)
uv run codeinsight tree --depth 1 --no-variables
```

### 関数を読み解く

```bash
uv run codeinsight understand <関数>    # 8つの問いに沿った読解カード(なぜ存在する/誰が呼ぶ/入力/変更/戻り値/影響/失敗時/履歴)
uv run codeinsight describe <シンボル>  # 宣言・docstring・メンバ・呼び出し/参照の件数
uv run codeinsight show <シンボル>      # 定義のソース(FILE[:LINE[-END]] も可)
uv run codeinsight flow <関数>          # 分岐・ループ・例外処理・循環的複雑度・リトライ/タイムアウトの手がかり
uv run codeinsight dataflow <関数> <変数> --depth 3 [--upstream]   # 値の行き先(代入・引数・戻り値・状態)、呼び出し元の実引数
uv run codeinsight state <クラス|モジュール>   # self.<属性>・モジュール変数の書き込み/変更/読み取り
uv run codeinsight exceptions <関数>    # 外へ出うる例外と、握りつぶし
uv run codeinsight effects <関数>       # 副作用の候補(直接と、呼び出し先を介したもの)
```

### 追う・探す

```bash
uv run codeinsight search fib                   # シンボル検索(--match exact|prefix|substring, --kind, --file)
uv run codeinsight search "fib(" --text         # テキスト全文検索(--regex, -i)。構文解析ではなく文字列一致
uv run codeinsight def main                     # 定義箇所
uv run codeinsight refs add                     # 参照箇所(呼び出し・名前・型注釈・import・継承)
uv run codeinsight callers fib                  # 呼び出し元
uv run codeinsight callees main                 # 呼び出し先(未解決・外部を含む。--hide-external)
uv run codeinsight trace main --depth 3         # 呼び出し階層(再帰・未解決を明示。--external で外部も)
uv run codeinsight path main fib                # 呼び出し経路の検索
uv run codeinsight deps main.c                  # ファイルの依存関係(--dependents, --cycles, --external)
uv run codeinsight unresolved                   # 静的に確定できなかった参照・依存関係(理由別)
```

### 影響・品質・履歴

```bash
uv run codeinsight impact <シンボル>    # 変更したときの影響範囲(利用者側・入口・テスト)
uv run codeinsight tests <シンボル>     # 届くテスト(静的な連鎖。カバレッジではない)。--untested で届かないコード
uv run codeinsight unused               # どこからも参照されていないシンボルの候補(確度つき)
uv run codeinsight risks                # 潜在的な問題の手がかり(例外の握りつぶし・eval・shell=True 等)
uv run codeinsight history [<シンボル>] # 変更履歴(Git・読み取り専用)。指定なしなら変更頻度・同時変更
uv run codeinsight docs-check           # 文書の識別子・オプション・環境変数と、実装の差(手がかり)
```

### AIで解説する(Phase 4)

解析結果(事実)とソースを根拠にAIが解説し、**回答の引用(`[ファイル:行]`)を機械的に検証**します。検証できない記述は「⚠未確認」と示され、検証済みの事実としては扱われません。AIの解説は解析結果とは別に保存され、`explanations` で参照できます。

```bash
# 1. 接続を確認する(ソースコードは送信しない)。Ollama を使う例(既定の送信先: http://localhost:11434/v1)
uv run codeinsight ai-status --ai-model qwen3-coder:latest

# 2. 送信される内容を確認する(何も送信しない)
uv run codeinsight explain <関数> --dry-run --ai-model qwen3-coder:latest

# 3. 送信を許可して解説する
uv run codeinsight explain <関数|クラス> --allow-send --ai-model qwen3-coder:latest
uv run codeinsight explain-file src/foo.py --allow-send --ai-model qwen3-coder:latest
uv run codeinsight explain-path <呼び出し元> <呼び出し先> --allow-send --ai-model qwen3-coder:latest
uv run codeinsight ask "キャッシュはどこに保存されますか?" --allow-send --ai-model qwen3-coder:latest

# 保存済みの解説(検証状態・古さつき)
uv run codeinsight explanations [<ID>]
```

評価セット（モデルやプロンプトの比較用。サンプルのソースのみ送信）:

```bash
uv run codeinsight ai-eval --list                                   # ケースの一覧（AI不使用）
uv run codeinsight ai-eval --allow-send --ai-model qwen3-coder:latest
```

* `--allow-send` が無いと、何も送信しません。送信先がこの計算機の外(localhost以外)の場合は、さらに `--allow-remote` が必要です。環境変数 `CODEINSIGHT_AI_ALLOW_SEND=1`・`CODEINSIGHT_AI_BASE_URL`・`CODEINSIGHT_AI_MODEL`、または `~/.codeinsight/config.toml` の `[ai]` でも設定できます。APIキーは環境変数 `CODEINSIGHT_AI_API_KEY` のみで読み(設定ファイルに書いても無視し、`ai-status` が警告します)、表示・ログには出しません。
* `--no-source` は、生のソース行を送らず、解析結果の事実(名前・位置・件数・docstringの先頭行)だけを送ります。
* 検証できるのは、引用の存在・行範囲・渡した根拠の範囲内であること・ファイルが解析後に変更されていないこと・回答中の名前の実在までです。**根拠が主張を実際に裏付けているかは、利用者が確認してください。**
* 解説は、根拠にしたファイルが変更されると「古い解説」と示されます。AIに接続できない場合も、AIを使わない機能(`understand` など)は、そのまま使えます。

### グラフを出力する

```bash
uv run codeinsight graph call --root main --depth 2 --format mermaid
uv run codeinsight graph flow --root <関数> --format html -o flow.html   # 制御フロー図
uv run codeinsight graph arch --format mermaid                           # コンポーネント間の依存(層の逆向き依存の候補は破線)
uv run codeinsight graph deps --format dot -o deps.dot
uv run codeinsight graph inherit --format json
uv run codeinsight graph call --format html -o call.html                 # 自己完結型のローカルビューアー(外部通信なし)
```

`graph` の種類は `call`・`deps`・`inherit`・`flow`(`--root` 必須)・`arch`。`--root`・`--depth`・`--direction out|in|both` で部分グラフに絞れます。実線は確定、破線は推定、点線は未解決・外部の関係です。ノードが多い全体グラフは読みにくいため、`--root`・`--depth`・`--exclude` で絞ることを推奨します。

名前が複数のシンボルに一致する場合は、候補を表示して終了します(修飾名か `--file` で絞り込みます)。静的な呼び出し関係は、実行順序や実際に通る経路を示すものではない点に注意してください。

`compile_commands.json` が解析対象ディレクトリにある場合は自動的に利用されます。無い場合は最小限のデフォルト引数で解析し、その旨を警告として表示します(詳細は [ANALYSIS.md](ANALYSIS.md))。

## テスト

```bash
uv run pytest -v   # または make test
make lint          # ruff と mypy(CIと同じ)
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

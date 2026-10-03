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

**Phase 1〜3(解析基盤・コードナビゲーション・可視化)に加え、コードリーディングのための関数単位の解析(制御フロー・データフロー・状態・例外・設定値・境界・影響範囲・履歴など)を実装済みです。** C言語/Pythonのシンボル・呼び出し・参照・依存の抽出と解決、検索、グラフ出力(Mermaid / DOT / JSON / ローカルHTML)、そして関数について「なぜ存在するか / 誰が呼ぶか / 入力 / 変更 / 戻り値 / 影響 / 失敗時 / なぜ今の実装か」の8つの問いに事実で答える読解カード(`understand`)が、CLIから使えます。AIによる解説(Phase 4)も実装済みで(下記「AIで解説する」)、端末UI(`tui`)、コードリーディング資料の一括生成(`make reading`)、Cの関数単位の解析も使えます。動的解析(実行観測)は設計とスタブのみで、実行は未実装です([DYNAMIC_ANALYSIS.md](DYNAMIC_ANALYSIS.md))。実装状況の詳細と制約は [REQUIREMENTS.md](REQUIREMENTS.md) と [ANALYSIS.md](ANALYSIS.md) を参照してください。

* 静的に確定できない関係(関数ポインタ、動的な呼び出し等)は推測で確定せず、**未解決**として明示します。候補を特定できるが実行時の挙動で変わりうるものは**推定**として、確定と区別します(AGENTS.md §3.5.1)。
* 制御フロー・データフロー・状態・リスク・読解カードは**Python・C**が対象です(Cは呼び出し先を経由した終了の連鎖も示します)。例外の伝播・制御フロー図・呼び出し元の実引数・設定値・実行環境・境界の詳細は**Python**のみです。データフローは関数単位・流れ非依存の**近似**で、ポインタのエイリアス・関数ポインタ先・マクロの内部は追えないと明示します。言語ごとの対応範囲は [OPERATIONS.md](OPERATIONS.md) 10.2。
* 設計判断の「理由」はコードから確認できないため推測せず、変更履歴・コメント・文書という手がかりを示します。
* 対象リポジトリのソースは変更せず、プログラムも実行しません(Gitは読み取り専用)。
* AIには**既定では何も送信しません**。送信は利用者の明示的な許可が必要で、ローカルLLM(Ollama等)ならこの計算機の中で完結します。AIの解説は、解析結果(事実)とは別に管理し、引用を機械的に検証したうえで、根拠を確認できない記述を「未確認」と示します。

実践的な検証対象は、以下の開発中リポジトリです(詳細は [AGENTS.md §2.3](AGENTS.md#23-実プロジェクトでの解析対象検証用) を参照)。現在のスコープはC言語・Pythonのため、[narou_dl](https://github.com/ac1965/narou_dl) が対象です。[PownForge](https://github.com/ac1965/PownForge) は未コミットの変更が多いため当面除外しています。Go製の[RiskForge](https://github.com/ac1965/RiskForge)とEmacs Lisp製の[.emacs.d](https://github.com/ac1965/.emacs.d)は将来の言語追加後の対象候補です。

## アーキテクチャ概要

```text
CodeInsight
├── Application     … ユースケースの実行(プロジェクト管理・検索・ナビゲーション・解説)
├── Analysis        … 言語別の静的解析(C/Python)、シンボル抽出、参照解決、呼び出しグラフ、関数単位の制御フロー・データフロー(Python: ast / C: Clang AST)
├── Domain          … 特定のGUI/DB/LLMに依存しないドメインモデル
├── AI              … 解析結果を利用したコード解説(解析器の代替にはしない)
├── Dynamic         … 動的解析(設計とスタブのみ。許可の確認と、実行しない計画表示。対象を実行するコードを持たない)
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

### コードリーディング資料を一式作る(`make reading`)

対象のリポジトリから、読むための資料を出力ディレクトリにまとめて作ります。対象のソースは変更せず(出力先が対象の中だとエラー)、対象のプログラムは実行しません。

```bash
make reading TARGET=../my-repo                         # reading/my-repo/ に資料一式と目次(README.md)
make reading TARGET=../my-repo OUT=/tmp/out TOP=12     # 出力先・主要な関数の数
make reading TARGET=../c-proj COMPILE_DB=/tmp/build    # Cで compile_commands.json がある場合
make reading TARGET=../my-repo AI_SEND=1                # AI解説(ai/・PDFの「AIの解説」章)を追記。ソースの一部をAIへ送信するため、既定では作らない
make reading TARGET=../my-repo AI_SEND=1 MODEL=qwen3-coder:latest   # モデルを指定する場合（省略時は環境変数 CODEINSIGHT_AI_MODEL・設定ファイル）
make reading-c-build TARGET=../c-proj BUILD=/tmp/build ALLOW_BUILD=1   # autotools系: 別の場所で configure + ビルド記録(対象のconfigure/makeを実行するため許可が必須)
```

`OUT/<名前>-reading.pdf` が、**全体を1つにまとめたPDF**です（表紙・目次・言語別の対応範囲・全体像と図・入口と境界・主要な関数の読解カード・注意して読む箇所・背景・付録。ページ番号つき）。PDFにはヘッドレスのブラウザ（Chrome・Chromium・Edge。環境変数 `CODEINSIGHT_BROWSER` で指定可）を使います。ブラウザが無い場合は、HTML（`<名前>-reading.html`）を残し、理由を示します（ブラウザで開いて「PDFとして保存」もできます）。`PDF=0` で作らない、`make reading-pdf OUT=…` で成果物から作り直せます。

`OUT/README.md` が目次で、全体像 → 入口と境界 → 処理を追う(図・主要な関数の読解カード) → 注意して読む箇所 → 背景の順に読めます。取得できなかった項目は目次とログ(`logs/`)に記録します。**言語ごとの対応範囲も目次に明記します**(「0件」が、対応していない言語では「問題なし」を意味しないため)。

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

### 端末で行き来する(TUI)

```bash
uv run codeinsight tui          # 構造ツリー・ソース・呼び出し関係を、端末の中で行き来する
```

* 左に構造（ディレクトリ・ファイル・シンボル）、右上にソース（選択したシンボルの範囲を強調）、右下に呼び出し元・呼び出し先を表示する。
* `↑↓`/`jk` 移動、`→`/`Enter` 開く、`←` 閉じる・親へ、`Tab` 呼び出し関係へ、`Enter` その定義へ移動、`b` 戻る、`/` 名前の検索、`[` `]` ソースの頁送り、`?` ヘルプ、`q` 終了。
* 確定・推定・未解決・曖昧・外部を区別して示す。未解決・曖昧・外部の関係へは移動できず、その理由を示す。解析後に変更されたファイルは、行番号がずれている可能性を示す。
* 保存済みの解析結果を読むだけで、対象のコードにも解析結果にも書き込まない。端末（対話的な環境）でのみ動く。標準ライブラリの `curses` を使う。

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

名前が複数のシンボルに一致する場合は、候補を表示して終了します(修飾名・`--file`、または同名の定義が同じファイルにある場合は `名前@行番号` で絞り込みます)。静的な呼び出し関係は、実行順序や実際に通る経路を示すものではない点に注意してください。

`compile_commands.json` が解析対象ディレクトリにある場合は自動的に利用されます(別の場所は `analyze --compile-commands <ディレクトリ>`)。無い場合は最小限のデフォルト引数で解析し、その旨を警告として表示します(詳細は [ANALYSIS.md](ANALYSIS.md))。

### autotools系のCのプロジェクトを解析する

`configure` で生成される `config.h` などが無いと、Cのファイルの多くは解析に失敗します(`config.h` が見つからない等)。CodeInsightは対象に対して `configure` やビルドを自動実行しないため、利用者が**別のディレクトリ(out-of-tree)**で実行し、ビルドの記録を渡します。対象のソースは変更されません。

```bash
mkdir -p /tmp/build && cd /tmp/build
/path/to/project/configure                 # config.h を生成(ビルドディレクトリに)
bear -- make -j8                           # compile_commands.json を作る
uv run codeinsight analyze /path/to/project --compile-commands /tmp/build
```

* 相対パス(`-I.`)の解決、システムヘッダーの探索、ファイルを書き出すオプション(`-MMD -MF` など)の除去を、CodeInsightが補います。
* 記録に載っていないファイルやヘッダーは、近隣の設定・取り込む側のソースの設定を使って解析します。その場合は「推定」と警告に記録します。
* 実測(gccのlibiberty: 解析できたファイル 10 → 131 / 139)は [TESTING.md](TESTING.md)。

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
* [DYNAMIC_ANALYSIS.md](DYNAMIC_ANALYSIS.md) — 動的解析の設計（スタブ。機能案・許可モデル・隔離・データモデル・段階的な導入。実行は未実装）
* [OPERATIONS.md](OPERATIONS.md) — 運用ガイド（導入・日常の運用・言語別の手順・資料の作成・AI解説の安全な運用・結果の読み方・トラブルシューティング・保守）
* [CHANGELOG.md](CHANGELOG.md) — 変更履歴

## ライセンス

GNU General Public License v3.0 以降（GPL-3.0-or-later）。全文は [LICENSE](LICENSE) を参照してください。
解析対象のソースコードにはこのライセンスは及びません（本ソフトウェアは対象を変更せず、解析結果も対象のライセンスを変えません）。

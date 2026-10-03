# CodeInsight 開発・利用の補助タスク
#
#   make help                使えるタスクの一覧
#   make setup               依存のインストール（uv sync --extra dev）
#   make test                テストの実行
#   make analyze TARGET=...  対象リポジトリを解析する（読み取りのみ。DBは対象の外に保存）
#
# 変数（コマンドラインで上書きできる）:
#   TARGET   解析対象ディレクトリ（analyze で必須）
#   NAME     シンボル名（understand / explain などで必須）
#   DB       解析結果DBのパス（省略時は ~/.codeinsight/codeinsight.db）
#   PROJECT  プロジェクトのID・名前・ルートパス（登録が複数のとき）
#   MODEL    AIのモデル名（explain 系で必須。例: qwen3-coder:latest）

UV      ?= uv
PYTEST  ?= $(UV) run pytest
CI      ?= $(UV) run codeinsight

# 読み取り系コマンドに付ける共通オプション（DB・PROJECT は指定されたときだけ）
READ_OPTS = $(if $(DB),--db $(DB)) $(if $(PROJECT),--project $(PROJECT))
AI_OPTS   = $(if $(MODEL),--ai-model $(MODEL))

.DEFAULT_GOAL := help
.PHONY: help setup test test-v test-fast check compile clean \
        analyze status overview architecture unresolved \
        understand explain-dry explain ai-status ai-eval

help: ## 使えるタスクの一覧を表示する
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z_-]+:.*## / {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# --- 開発 ---

setup: ## 依存をインストールする（開発用を含む）
	$(UV) sync --extra dev

test: ## 全テストを実行する
	$(PYTEST) -q tests

test-v: ## 全テストを、詳細表示で実行する
	$(PYTEST) -v tests

test-fast: ## 最初に失敗したところで止めて実行する
	$(PYTEST) -q -x tests

compile: ## 構文エラーが無いかを確認する
	$(UV) run python -m compileall -q src

check: compile test ## 構文の確認とテストをまとめて実行する

clean: ## 生成物（キャッシュ・ビルド成果物）を削除する。解析結果DB(~/.codeinsight)は消さない
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null; true
	rm -rf .pytest_cache .mypy_cache build dist *.egg-info

# --- 解析（対象リポジトリは変更しない） ---

analyze: ## 対象を解析して保存する（TARGET=ディレクトリ が必須）
	$(if $(TARGET),,$(error TARGET を指定してください。例: make analyze TARGET=../my-repo))
	$(CI) analyze $(TARGET) $(if $(DB),--db $(DB))

status: ## 解析状況と、解析後に変更されたファイルを表示する
	$(CI) status $(READ_OPTS)

overview: ## リポジトリの全体像（主要モジュール・エントリポイント・中心となる関数）
	$(CI) overview $(READ_OPTS)

architecture: ## コンポーネント構成・層構造・循環・外部連携
	$(CI) architecture $(READ_OPTS)

unresolved: ## 静的に確定できなかった参照・依存関係（理由別）
	$(CI) unresolved $(READ_OPTS)

understand: ## 関数の読解カード（NAME=関数名 が必須）
	$(if $(NAME),,$(error NAME を指定してください。例: make understand NAME=main))
	$(CI) understand $(NAME) $(READ_OPTS)

# --- AI解説（既定では何も送信しない。送信にはAI_SEND=1が必要） ---

ai-status: ## AIの設定と接続を確認する（ソースは送らない）
	$(CI) ai-status $(AI_OPTS) $(if $(DB),--db $(DB))

explain-dry: ## AIへ送信される内容を表示する（送信しない。NAME・MODEL が必須）
	$(if $(NAME),,$(error NAME を指定してください))
	$(if $(MODEL),,$(error MODEL を指定してください。例: MODEL=qwen3-coder:latest))
	$(CI) explain $(NAME) --dry-run $(AI_OPTS) $(READ_OPTS)

explain: ## AIで解説する（NAME・MODEL・AI_SEND=1 が必須。ローカルLLMなら送信はこの計算機の中で完結）
	$(if $(NAME),,$(error NAME を指定してください))
	$(if $(MODEL),,$(error MODEL を指定してください。例: MODEL=qwen3-coder:latest))
	$(if $(filter 1,$(AI_SEND)),,$(error ソースコードをAIへ送信します。許可する場合は AI_SEND=1 を付けてください。内容は make explain-dry で確認できます))
	$(CI) explain $(NAME) --allow-send $(AI_OPTS) $(READ_OPTS)

ai-eval: ## AI解説を評価ケース(eval/ai_cases.toml)で採点する（MODEL・AI_SEND=1 が必須。サンプルのみを送信）
	$(if $(MODEL),,$(error MODEL を指定してください。例: MODEL=qwen3-coder:latest))
	$(if $(filter 1,$(AI_SEND)),,$(error サンプルのソースをAIへ送信します。許可する場合は AI_SEND=1 を付けてください。ケースの一覧は codeinsight ai-eval --list))
	$(CI) ai-eval --allow-send $(AI_OPTS)

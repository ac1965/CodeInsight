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
#
# コードリーディング成果物（対象のリポジトリから、読むための資料一式を出力ディレクトリに作る）:
#   make reading TARGET=../my-repo                       # 解析 → 資料一式 → 目次(README.md)
#   make reading TARGET=../my-repo OUT=out TOP=12        # 出力先・主要な関数の数を指定
#   make reading TARGET=../c-proj COMPILE_DB=/tmp/build  # Cで compile_commands.json がある場合
#   make reading-c-build TARGET=../c-proj BUILD=/tmp/build ALLOW_BUILD=1   # autotools系: 別の場所で configure+ビルド記録
#   make reading TARGET=... MODEL=qwen3-coder:latest AI_SEND=1  # AIの解説も加える（既定では送信しない）

UV      ?= uv
PYTEST  ?= $(UV) run pytest
CODEINSIGHT ?= $(UV) run codeinsight

# 読み取り系コマンドに付ける共通オプション（DB・PROJECT は指定されたときだけ）
READ_OPTS = $(if $(DB),--db $(DB)) $(if $(PROJECT),--project $(PROJECT))
AI_OPTS   = $(if $(MODEL),--ai-model $(MODEL))

.DEFAULT_GOAL := help
# --- コードリーディング成果物の設定 ---
# パスは、先頭の ~ を展開して絶対パスにそろえる（OUT=~/x のように、シェルが展開しない渡し方でも動くように）。
# コマンドラインの値を上書きするため override を使う。
expand_path = $(if $(strip $(1)),$(abspath $(patsubst ~/%,$(HOME)/%,$(patsubst ~,$(HOME),$(strip $(1))))))
override TARGET := $(call expand_path,$(TARGET))
READING_NAME = $(notdir $(TARGET))
OUT         ?= reading/$(READING_NAME)
override OUT := $(call expand_path,$(OUT))
TOP         ?= 8
RDB          = $(OUT)/analysis/codeinsight.db
RCI          = $(CODEINSIGHT)
RFLAGS       = --db $(RDB)
BUILD       ?= $(OUT)/build
override BUILD := $(call expand_path,$(BUILD))
COMPILE_DB  ?= $(if $(wildcard $(BUILD)/compile_commands.json),$(BUILD),)
override COMPILE_DB := $(call expand_path,$(COMPILE_DB))

.PHONY: help setup test test-v test-fast check compile clean \
        analyze status overview architecture unresolved \
        understand explain-dry explain ai-status ai-eval lint \
        reading reading-check reading-analyze reading-docs reading-graphs reading-functions reading-ai reading-index reading-c-build reading-clean

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

lint: ## ruff と mypy で静的検査する（CIと同じ）
	$(UV) run ruff check .
	$(UV) run mypy

check: compile lint test ## 構文の確認・静的検査・テストをまとめて実行する

clean: ## 生成物（キャッシュ・ビルド成果物）を削除する。解析結果DB(~/.codeinsight)は消さない
	find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null; true
	rm -rf .pytest_cache .mypy_cache build dist *.egg-info

# --- 解析（対象リポジトリは変更しない） ---

analyze: ## 対象を解析して保存する（TARGET=ディレクトリ が必須）
	$(if $(TARGET),,$(error TARGET を指定してください。例: make analyze TARGET=../my-repo))
	$(CODEINSIGHT) analyze $(TARGET) $(if $(DB),--db $(DB))

status: ## 解析状況と、解析後に変更されたファイルを表示する
	$(CODEINSIGHT) status $(READ_OPTS)

overview: ## リポジトリの全体像（主要モジュール・エントリポイント・中心となる関数）
	$(CODEINSIGHT) overview $(READ_OPTS)

architecture: ## コンポーネント構成・層構造・循環・外部連携
	$(CODEINSIGHT) architecture $(READ_OPTS)

unresolved: ## 静的に確定できなかった参照・依存関係（理由別）
	$(CODEINSIGHT) unresolved $(READ_OPTS)

understand: ## 関数の読解カード（NAME=関数名 が必須）
	$(if $(NAME),,$(error NAME を指定してください。例: make understand NAME=main))
	$(CODEINSIGHT) understand $(NAME) $(READ_OPTS)

# --- AI解説（既定では何も送信しない。送信にはAI_SEND=1が必要） ---

ai-status: ## AIの設定と接続を確認する（ソースは送らない）
	$(CODEINSIGHT) ai-status $(AI_OPTS) $(if $(DB),--db $(DB))

explain-dry: ## AIへ送信される内容を表示する（送信しない。NAME・MODEL が必須）
	$(if $(NAME),,$(error NAME を指定してください))
	$(if $(MODEL),,$(error MODEL を指定してください。例: MODEL=qwen3-coder:latest))
	$(CODEINSIGHT) explain $(NAME) --dry-run $(AI_OPTS) $(READ_OPTS)

explain: ## AIで解説する（NAME・MODEL・AI_SEND=1 が必須。ローカルLLMなら送信はこの計算機の中で完結）
	$(if $(NAME),,$(error NAME を指定してください))
	$(if $(MODEL),,$(error MODEL を指定してください。例: MODEL=qwen3-coder:latest))
	$(if $(filter 1,$(AI_SEND)),,$(error ソースコードをAIへ送信します。許可する場合は AI_SEND=1 を付けてください。内容は make explain-dry で確認できます))
	$(CODEINSIGHT) explain $(NAME) --allow-send $(AI_OPTS) $(READ_OPTS)

ai-eval: ## AI解説を評価ケース(eval/ai_cases.toml)で採点する（MODEL・AI_SEND=1 が必須。サンプルのみを送信）
	$(if $(MODEL),,$(error MODEL を指定してください。例: MODEL=qwen3-coder:latest))
	$(if $(filter 1,$(AI_SEND)),,$(error サンプルのソースをAIへ送信します。許可する場合は AI_SEND=1 を付けてください。ケースの一覧は codeinsight ai-eval --list))
	$(CODEINSIGHT) ai-eval --allow-send $(AI_OPTS)


# ============================================================
# コードリーディング成果物
#
# 対象のリポジトリを読む人のために、静的解析で確認できた事実を資料一式にまとめる。
#   - 対象のソースは変更しない。出力は OUT（対象の外）に限る。対象のプログラムは実行しない。
#   - AIの解説は、MODEL と AI_SEND=1 がある場合だけ作る（ai/ に、解析結果とは別に置く）。
#   - 取得できなかった項目は、目次（README.md）に記録する。失敗を隠さない。
# ============================================================

# 1項目を生成する: $(call gen,出力ファイル,サブコマンド...)。失敗してもほかの項目は続け、ログに残す。
define gen
@mkdir -p $(dir $(1)) $(OUT)/logs; \
if $(RCI) $(2) $(RFLAGS) > "$(1)" 2> "$(OUT)/logs/$(notdir $(1)).log"; then \
  [ -s "$(OUT)/logs/$(notdir $(1)).log" ] || rm -f "$(OUT)/logs/$(notdir $(1)).log"; \
else \
  echo "取得できませんでした: $(2)" > "$(1)"; cat "$(OUT)/logs/$(notdir $(1)).log" >> "$(1)"; \
  echo "  ! 取得できなかった項目: $(notdir $(1))"; \
fi
endef

# 主要な関数（入口・よく呼ばれる・多くを呼ぶ・大きい）の修飾名を、重複なしで TOP 件ずつ出す
define KEY_FUNCTIONS
import json, sys
d = json.load(sys.stdin)
top = int(sys.argv[1])
seen, out = set(), []
for key in ("entry_points", "most_called", "most_calling", "largest"):
    for item in d.get(key, [])[:top]:
        if item.get("kind") in ("function", "method") and item["qualified_name"] + str(item["start_line"]) not in seen:
            seen.add(item["qualified_name"] + str(item["start_line"])); out.append(f'{item["qualified_name"]}@{item["start_line"]}\t{item["path"]}')
print("\n".join(out[: top * 2]))
endef
export KEY_FUNCTIONS

# 目次を作る
define READING_INDEX
import json, os, sys, datetime
out, target, top = sys.argv[1], sys.argv[2], sys.argv[3]
def read(path):
    try:
        return open(os.path.join(out, path), encoding="utf-8").read()
    except OSError:
        return ""
try:
    ov = json.loads(read("overview.json"))
except ValueError:
    ov = {}
failed = sorted(f for f in os.listdir(os.path.join(out, "logs")) if os.path.isfile(os.path.join(out, "logs", f))) if os.path.isdir(os.path.join(out, "logs")) else []
missing = [f for f in ("overview.txt", "architecture.txt") if read(f).startswith("取得できませんでした")]
lines = [f"# コードリーディング資料: {ov.get('project', os.path.basename(target))}", ""]
lines += [f"* 対象: `{target}`（リビジョン: {ov.get('revision') or '-'}）",
          f"* 生成: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}（CodeInsight）",
          f"* 言語: {', '.join(f'{k} {v}' for k, v in ov.get('languages', {}).items()) or '-'}",
          f"* 参照の解決状況: {json.dumps(ov.get('reference_status', {}), ensure_ascii=False)}",
          f"* 解析に失敗したファイル: {len(ov.get('failed_files', []))}件 / 解析後に変更されたファイル: {len(ov.get('stale_files', []))}件", ""]
langs = set(ov.get("languages", {}))
coverage = {
    "python": ("構造・シンボル・呼び出し関係・依存・参照・影響範囲・外部連携・設定・境界・制御フロー・データフロー・例外・状態・リスク・テスト・履歴", "（なし）"),
    "c": ("構造・シンボル・呼び出し関係・include・参照（関数ポインタ・マクロは未解決として区別）・影響範囲・外部連携・テスト・履歴・関数内の制御フロー/データフロー/状態/終了経路/リスク（流れ非依存の近似）", "ポインタのエイリアス・関数ポインタ先・マクロ内部・呼び出し元の実引数の追跡・呼び出し先の終了の伝播。config/environment/boundaries の詳細（Pythonのみ）"),
}
lines += ["## この資料が対応している範囲（言語別）", ""]
for lang in sorted(langs):
    done, todo = coverage.get(lang, ("構造・シンボルなどの一部", "未確認"))
    lines += [f"* **{lang}** — 対応: {done}。未対応: {todo}"]
lines += [""]
lines += ["## この資料の読み方（事実と推論の区別）", "",
          "* ここにある内容は、構文解析で確認できた事実です。実行順序や実際に通る経路を示すものではありません。",
          "* 「推定」「未解決」「曖昧」「外部」は確定ではありません。理由を併記しています。",
          "* `ai/` の解説は、AIが解析結果を入力に生成したもので、解析結果ではありません（引用の検証結果つき）。", ""]
steps = [
    ("1. 全体像", [("overview.txt", "言語・主要モジュール・入口・中心となる関数"), ("architecture.txt", "コンポーネント構成・層構造・循環・外部連携"), ("graphs/arch.html", "コンポーネント間の依存（図）")]),
    ("2. 入口と境界", [("boundaries.txt", "入口・CLI・HTTP・イベント・スレッド・非同期・キャッシュ"), ("config.txt", "環境変数・CLIオプション・設定ファイル・定数"), ("externals.txt", "外部ライブラリ・システムへの入出力"), ("environment.txt", "言語のバージョン・依存・外部コマンド")]),
    ("3. 処理を追う", [("graphs/call.html", "関数呼び出しグラフ（図）"), ("graphs/deps.html", "ファイル間の依存（図）"), ("functions/", f"主要な関数の読解カード（最大{int(top) * 2}件）")]),
    ("4. 注意して読む箇所", [("risks.txt", "危険な書き方の候補"), ("tests-untested.txt", "テストから到達できない関数"), ("unused.txt", "使われていない可能性のあるコード"), ("analysis/unresolved.txt", "静的に確定できなかった関係（理由別）")]),
    ("5. 背景", [("history.txt", "変更履歴の手がかり（同時に変更されるファイル等）"), ("docs-check.txt", "文書とコードのずれの候補")]),
]
for title, items in steps:
    lines += [f"## {title}", ""]
    for rel, desc in items:
        path = os.path.join(out, rel)
        if rel.endswith("/"):
            names = sorted(os.listdir(path)) if os.path.isdir(path) else []
            lines.append(f"* [{rel}]({rel}) — {desc}: " + (", ".join(f"[{n[:-4]}]({rel}{n})" for n in names) or "なし"))
        elif os.path.exists(path):
            lines.append(f"* [{rel}]({rel}) — {desc}" + ("（取得できませんでした）" if read(rel).startswith("取得できませんでした") else ""))
        else:
            lines.append(f"* {rel} — {desc}（生成されていません）")
    lines.append("")
ai = os.path.join(out, "ai")
lines += ["## AIの解説", ""]
if os.path.isdir(ai) and os.listdir(ai):
    lines += [f"* [{n}](ai/{n})" for n in sorted(os.listdir(ai))]
else:
    lines += ["* 生成していません（`MODEL=... AI_SEND=1` を付けると、主要な関数の解説を `ai/` に作ります。送信内容は `make explain-dry` で確認できます）。"]
lines.append("")
if failed:
    lines += ["## 取得時の警告・エラー（`logs/`）", ""] + [f"* [logs/{f}](logs/{f})" for f in failed] + [""]
lines += ["## 再生成", "", "```bash", f"make reading TARGET={target} OUT={out}", "```", ""]
open(os.path.join(out, "README.md"), "w", encoding="utf-8").write("\n".join(lines))
print(f"目次を作成しました: {os.path.join(out, 'README.md')}")
endef
export READING_INDEX

reading-check:
	$(if $(TARGET),,$(error TARGET を指定してください。例: make reading TARGET=../my-repo))
	@case "$(TARGET)$(OUT)" in *" "*) echo "TARGET / OUT のパスに空白は使えません: $(TARGET) / $(OUT)"; exit 2;; esac
	@command -v $(firstword $(UV)) >/dev/null 2>&1 || { echo "$(firstword $(UV)) が見つかりません（https://docs.astral.sh/uv/ からインストールするか、UV=... で指定してください）"; exit 2; }
	@test -d "$(TARGET)" || { echo "TARGET がディレクトリではありません: $(TARGET)"; exit 2; }
	@case "$$(cd "$(TARGET)" && pwd -P)/" in \
	  "$$(mkdir -p "$(OUT)" && cd "$(OUT)" && pwd -P)/"*) echo "OUT は TARGET の外に指定してください（対象を変更しないため）: $(OUT)"; exit 2;; esac; \
	case "$$(mkdir -p "$(OUT)" && cd "$(OUT)" && pwd -P)/" in \
	  "$$(cd "$(TARGET)" && pwd -P)/"*) echo "OUT は TARGET の外に指定してください（対象を変更しないため）: $(OUT)"; exit 2;; esac

reading-analyze: reading-check ## [資料] 対象を解析する（OUT/analysis に保存）
	@mkdir -p $(OUT)/analysis $(OUT)/logs
	$(RCI) analyze $(TARGET) --db $(RDB) $(if $(COMPILE_DB),--compile-commands $(COMPILE_DB)) > $(OUT)/analysis/analyze.txt 2> $(OUT)/logs/analyze.log; \
	  rc=$$?; cat $(OUT)/analysis/analyze.txt | head -5; [ $$rc -le 1 ] || exit $$rc
	$(call gen,$(OUT)/analysis/status.txt,status)

reading-docs: reading-analyze ## [資料] 全体像・構成・境界・設定・外部・リスクなどを書き出す
	$(call gen,$(OUT)/overview.txt,overview --top 15)
	$(call gen,$(OUT)/overview.json,overview --top $(TOP) --format json)
	$(call gen,$(OUT)/architecture.txt,architecture)
	$(call gen,$(OUT)/boundaries.txt,boundaries)
	$(call gen,$(OUT)/config.txt,config)
	$(call gen,$(OUT)/externals.txt,externals)
	$(call gen,$(OUT)/environment.txt,environment)
	$(call gen,$(OUT)/risks.txt,risks)
	$(call gen,$(OUT)/tests-untested.txt,tests --untested)
	$(call gen,$(OUT)/unused.txt,unused --limit 100)
	$(call gen,$(OUT)/analysis/unresolved.txt,unresolved --limit 200)
	$(call gen,$(OUT)/history.txt,history)
	$(call gen,$(OUT)/docs-check.txt,docs-check --limit 100)

reading-graphs: reading-analyze ## [資料] 呼び出し・依存・構成の図（HTMLとMermaid）を作る
	@mkdir -p $(OUT)/graphs $(OUT)/logs
	@for kind in call deps arch; do \
	  $(RCI) graph $$kind --format html -o $(OUT)/graphs/$$kind.html $(RFLAGS) 2>> $(OUT)/logs/graph-$$kind.log || echo "  ! 取得できなかった項目: graphs/$$kind.html"; \
	  $(RCI) graph $$kind --format mermaid -o $(OUT)/graphs/$$kind.mmd $(RFLAGS) 2>> $(OUT)/logs/graph-$$kind.log || true; \
	  [ -s $(OUT)/logs/graph-$$kind.log ] || rm -f $(OUT)/logs/graph-$$kind.log; \
	done

reading-functions: reading-docs ## [資料] 主要な関数（上位 TOP 件ずつ）の読解カードを作る
	@mkdir -p $(OUT)/functions $(OUT)/logs
	@$(UV) run python -c "$$KEY_FUNCTIONS" $(TOP) < $(OUT)/overview.json > $(OUT)/functions/.names 2>> $(OUT)/logs/functions.log || echo "  ! 主要な関数の一覧を作れませんでした（logs/functions.log）"
	@while IFS="$$(printf '\t')" read -r name path; do \
	  [ -n "$$name" ] || continue; \
	  file="$(OUT)/functions/$$(echo "$$name" | sed 's/@\([0-9]*\)$$/_L\1/' | tr -c 'A-Za-z0-9._\n-' '_').txt"; \
	  $(RCI) understand "$$name" --file "$$path" $(RFLAGS) > "$$file" 2>> $(OUT)/logs/functions.log || { echo "  ! 取得できなかった関数: $$name"; rm -f "$$file"; }; \
	done < $(OUT)/functions/.names
	@[ -s $(OUT)/logs/functions.log ] || rm -f $(OUT)/logs/functions.log

reading-ai: reading-functions ## [資料] AI解説（MODEL と AI_SEND=1 がある場合のみ。ai/ に別管理で置く）
	@if [ -n "$(MODEL)" ] && [ "$(AI_SEND)" = "1" ]; then \
	  mkdir -p $(OUT)/ai; \
	  while IFS="$$(printf '\t')" read -r name path; do \
	    [ -n "$$name" ] || continue; \
	    base=$$(echo "$$name" | sed 's/@\([0-9]*\)$$/_L\1/' | tr -c 'A-Za-z0-9._\n-' '_'); \
	    $(RCI) explain "$$name" --file "$$path" --allow-send --ai-model $(MODEL) $(RFLAGS) > "$(OUT)/ai/$$base.md" 2>> $(OUT)/logs/ai.log || { echo "  ! AI解説を作れなかった関数: $$name"; rm -f "$(OUT)/ai/$$base.md"; }; \
	  done < $(OUT)/functions/.names; \
	else echo "AI解説はスキップ（MODEL=... AI_SEND=1 を付けると作成。送信内容は make explain-dry で確認できます）"; fi

reading-index: ## [資料] 目次（README.md）を作る
	@mkdir -p $(OUT)/logs
	@$(UV) run python -c "$$READING_INDEX" "$(OUT)" "$(TARGET)" "$(TOP)"

reading: reading-docs reading-graphs reading-functions reading-ai ## [資料] 対象のコードリーディング資料一式を OUT に作る（TARGET 必須）
	@$(MAKE) --no-print-directory reading-index TARGET=$(TARGET) OUT=$(OUT) TOP=$(TOP)
	@echo "完了: $(OUT)/README.md から読み始められます"

reading-c-build: reading-check ## [資料] autotools系のC: 別の場所で configure+ビルド記録（ALLOW_BUILD=1 が必須。対象の configure とmakeを実行する）
	$(if $(filter 1,$(ALLOW_BUILD)),,$(error 対象の configure と make を実行します（対象のコードは変更しませんが、ビルドの手順を動かします）。許可する場合は ALLOW_BUILD=1 を付けてください))
	@test -x "$(TARGET)/configure" || { echo "$(TARGET)/configure がありません（autoreconf は実行しません。生成済みの configure が必要です）"; exit 2; }
	@command -v bear >/dev/null || { echo "bear が必要です（brew install bear）"; exit 2; }
	@mkdir -p $(BUILD) && cd $(BUILD) && "$(abspath $(TARGET))/configure" > configure.log 2>&1 && bear -- $(MAKE) -j4 > make.log 2>&1; \
	  rc=$$?; echo "configure+make: rc=$$rc ($(BUILD)/configure.log, make.log)"; \
	  [ -f $(BUILD)/compile_commands.json ] && echo "compile_commands.json: $(BUILD)/compile_commands.json（make reading で自動的に使います）" || { echo "compile_commands.json を作れませんでした"; exit 1; }

reading-clean: ## [資料] OUT（成果物）を削除する。対象には触れない
	$(if $(TARGET),,$(error TARGET を指定してください))
	rm -rf $(OUT)

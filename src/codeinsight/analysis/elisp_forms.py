"""Emacs Lispの特殊形式・制御構造の名前の一覧。解析器・フロー解析・データフロー解析・制御フロー図で共有する。

名前の一覧が、各モジュールで食い違わないよう、ここで1か所に定義する。
"""

from __future__ import annotations

# 束縛
LET_FORMS = frozenset({"let", "let*", "letrec", "dlet", "lexical-let"})
IF_LET_FORMS = frozenset({"if-let", "if-let*", "when-let", "when-let*", "and-let*", "while-let"})
VARIABLE_BINDERS = LET_FORMS | IF_LET_FORMS  # 変数を束縛する形式（(変数 値) の束縛リストを持つ）
PLACE_BINDERS = frozenset({"cl-letf", "cl-letf*"})  # 場所（関数・変数）を一時的に書き換える束縛
LAMBDA_LIST_SKIP = frozenset({"&optional", "&rest", "&key", "&aux", "&body", "&allow-other-keys", "&context"})

# 分岐・繰り返し
IF_FORMS = frozenset({"if", "if-let", "if-let*"})
WHEN_FORMS = frozenset({"when", "unless", "when-let", "when-let*", "and-let*"})
BRANCH_FORMS = IF_FORMS | WHEN_FORMS
MATCH_FORMS = frozenset({"pcase", "pcase-exhaustive", "cl-case", "cl-ecase", "ecase", "cl-typecase", "cl-etypecase", "seq-case"})
EXHAUSTIVE_MATCH_SUFFIXES = ("exhaustive", "ecase", "etypecase")  # どの節にも一致しない場合に、シグナルになるもの
LOOP_FORMS = frozenset({"while", "dolist", "dotimes", "cl-dolist", "cl-dotimes", "while-let"})  # 制御フロー図で分解して示す繰り返し
ALL_LOOP_FORMS = LOOP_FORMS | {"cl-loop", "pcase-dolist", "named-let", "cl-do", "cl-do*"}  # 繰り返しとして数えるもの

# エラー（シグナル）・終了・早期の戻り
CONDITION_CASE_FORMS = frozenset({"condition-case", "condition-case-unless-debug"})
HANDLER_FORMS = frozenset({*CONDITION_CASE_FORMS, "ignore-errors", "ignore-error", "with-demoted-errors"})
SIGNAL_FORMS = {
    "error": "error", "user-error": "user-error", "signal": "signal", "cl-assert": "cl-assert", "cl-check-type": "wrong-type-argument", "throw": "throw",
}
TERMINATE_FORMS = frozenset({"kill-emacs", "kill-terminal"})
RETURN_FORMS = frozenset({"cl-return", "cl-return-from"})

# 値の書き込み
SETQ_FORMS = frozenset({"setq", "setq-local", "setq-default"})
PLACE_SECOND_FORMS = frozenset({"push", "cl-pushnew"})  # (push 値 場所): 場所が第2引数

"""Emacs Lisp の解析（決定論的）。

S式を、括弧の対応を追う読み取り器で木にし、そこからシンボル・参照・依存関係を取り出す。
トップレベルのフォームの切り出しの考え方（文字列・コメント・文字リテラルを括弧に数えない）は、
CodeReading（利用者の別のリポジトリ）の `extract_symbols.py` を土台にしている。ここで直した点:

* 文字リテラル `?\\(` `?\\)`（エスケープつき）で、括弧を誤って数えていた。
* 引用（`'x` `` `x``）の中と、評価される式を区別する（`,x` で引用を解除）。

Emacs Lisp は名前空間が全体で1つで、関数の置き換え（advice・`fset`・再定義）やマクロの展開があるため、
呼び出し先を名前の一致で決めた結果は「推定」とし、確定として扱わない（AGENTS.md §3.2.2・§3.5.1）。
追えないもの: マクロの展開の内部、`eval`、`funcall` に変数経由で渡された関数、`let`・引数による同名の局所束縛。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from codeinsight.analysis.ids import IdAllocator, build_symbol
from codeinsight.analysis.language_adapter import FileAnalysis, SourceUnit
from codeinsight.domain import (
    Dependency,
    DependencyKind,
    Language,
    Reference,
    ReferenceKind,
    ResolutionStatus,
    SourceLocation,
    Symbol,
    SymbolKind,
)

KEY_CALL = "el:call:"  # 関数の呼び出し・関数への参照（名前の一致で解決する）
KEY_VAR = "el:var:"  # 変数の参照
KEY_FEATURE = "el:feature:"  # (require 'feature) / (load "file")

# 定義フォーム → (シンボルの種類, 形式)
_DEFINERS: dict[str, tuple[SymbolKind, str]] = {
    "defun": (SymbolKind.FUNCTION, "defun"), "defsubst": (SymbolKind.FUNCTION, "defun"), "cl-defun": (SymbolKind.FUNCTION, "defun"),
    "define-inline": (SymbolKind.FUNCTION, "defun"), "cl-defsubst": (SymbolKind.FUNCTION, "defun"),
    "defmacro": (SymbolKind.MACRO, "defun"), "cl-defmacro": (SymbolKind.MACRO, "defun"),
    "cl-defgeneric": (SymbolKind.FUNCTION, "defun"), "cl-defmethod": (SymbolKind.FUNCTION, "method"),
    "defalias": (SymbolKind.FUNCTION, "alias"),
    "define-minor-mode": (SymbolKind.FUNCTION, "mode"), "define-derived-mode": (SymbolKind.FUNCTION, "mode"),
    "define-globalized-minor-mode": (SymbolKind.FUNCTION, "mode"), "define-generic-mode": (SymbolKind.FUNCTION, "mode"),
    "defvar": (SymbolKind.GLOBAL_VARIABLE, "var"), "defvar-local": (SymbolKind.GLOBAL_VARIABLE, "var"),
    "defconst": (SymbolKind.GLOBAL_VARIABLE, "var"), "defcustom": (SymbolKind.GLOBAL_VARIABLE, "var"),
    "defgroup": (SymbolKind.GLOBAL_VARIABLE, "group"), "defface": (SymbolKind.GLOBAL_VARIABLE, "face"),
    "cl-defstruct": (SymbolKind.STRUCT, "struct"),
}
_FORM_LABEL = {"group": "[カスタマイズグループ] ", "face": "[フェイス] "}
# 内部に定義を含みうる、条件付き・読み込み時評価のフォーム（どれが実行されるかは静的に決まらない）
_WRAPPERS = frozenset({"eval-and-compile", "eval-when-compile", "progn", "with-eval-after-load", "when", "unless", "if", "cond", "prog1", "let", "let*"})
_NO_CALL = frozenset(
    """quote function backquote if cond when unless let let* progn prog1 prog2 setq setq-local setq-default lambda and or while dolist dotimes
    catch condition-case ignore-errors unwind-protect save-excursion save-restriction save-match-data save-current-buffer interactive declare
    with-current-buffer with-temp-buffer with-temp-file with-output-to-string with-eval-after-load with-suppressed-warnings with-no-warnings
    eval-when-compile eval-and-compile cl-loop cl-flet cl-labels cl-letf cl-letf* cl-macrolet cl-case cl-ecase cl-typecase cl-block cl-return
    cl-return-from cl-dolist cl-dotimes cl-destructuring-bind cl-incf cl-decf cl-pushnew cl-callf cl-assert cl-check-type pcase pcase-let
    pcase-let* pcase-dolist pcase-setq pcase-exhaustive if-let if-let* when-let when-let* and-let* while-let thread-first thread-last push pop
    incf decf setf add-to-list use-package define-key define-error autoload provide require lexical-let named-let letrec dlet add-hook
    remove-hook with-slots with-memoization cl-once-only cl-with-gensyms""".split()
)
_BINDING_FORMS = frozenset({"let", "let*", "letrec", "dlet", "lexical-let", "cl-letf", "cl-letf*"})
_IF_LET_FORMS = frozenset({"if-let", "if-let*", "when-let", "when-let*", "and-let*", "while-let"})
_CLAUSE_FORMS = frozenset({"pcase", "pcase-exhaustive", "cl-case", "cl-ecase", "cl-typecase", "ecase", "pcase-let", "pcase-let*"})  # 先頭が式、残りが節
_LOCAL_FUNCTION_FORMS = frozenset({"cl-flet", "cl-labels", "cl-macrolet", "cl-flet*"})  # ((名前 引数 本体...) ...) 本体...
_SKIP_FIRST_FORMS = frozenset({"with-slots", "named-let", "cl-destructuring-bind"})  # 先頭は、パターン・名前（式ではない）
_SYMBOL_CHARS = re.compile(r"(?:\\.|[^\s()\[\]'`,\";\\])+", re.DOTALL)  # バックスラッシュでエスケープした特殊文字（`a\;b` `\[`）を含む
_HEADER = re.compile(r"^;;;\s+[^\s]+\s+---\s*(.*?)\s*(?:-\*-.*)?$")
_ESCAPE_NAMED = "xXuUN"


# --- S式の読み取り ---


@dataclass
class Atom:
    text: str
    line: int
    kind: str  # symbol / string / number / char
    quoted: bool = False  # 引用（'x `x）の中。`,x` では解除される
    function_quote: bool = False  # #'x


@dataclass
class Form:
    items: list[Atom | Form] = field(default_factory=list)
    line: int = 1
    end_line: int = 1
    quoted: bool = False
    vector: bool = False
    template: bool = False  # バッククォート（`）の中。マクロの本体では、展開後のコードのテンプレートであることが多い


class ElispSyntaxError(Exception):
    pass


def _char_literal_end(text: str, i: int) -> int:
    """`?` で始まる文字リテラルの終わりの位置。

    `?a` `?\\(` `?\\C-a` `?\\C-\\M-x` `?\\s--`（スーパー修飾の `-`）`?\\^\\\\`（制御文字の `\\`）`?\\x41` `?\\N{...}` に対応する。
    """

    n = len(text)
    j = i + 1
    if j >= n:
        return j
    if text[j] != "\\":
        return j + 1
    j += 1
    while j < n:  # 修飾（\C- \M- \S- \H- \s- \A- と、制御の \^）。後ろに、さらにエスケープが続くことがある
        if text[j] in "CMSHsA" and text[j + 1:j + 2] == "-":
            j += 2
        elif text[j] == "^":
            j += 1
        else:
            break
        if j < n and text[j] == "\\":
            j += 1
            continue
        return min(j + 1, n)  # 修飾の後ろの、素の1文字（`-` `?` `:` `]` など）
    if j >= n:
        return n
    escape = text[j]
    j += 1
    if escape == "N" and text[j:j + 1] == "{":
        end = text.find("}", j)
        return n if end < 0 else end + 1
    if escape in _ESCAPE_NAMED or escape.isdigit():
        while j < n and text[j].isalnum():
            j += 1
    return j


def read_forms(text: str) -> list[Form]:
    """ファイル全体を、トップレベルのフォームの並びとして読み取る。括弧が閉じない場合は例外にする。

    文字列・`;` コメント・文字リテラルの中の括弧は、数えない。
    """

    forms: list[Form] = []
    stack: list[Form] = []
    prefix: str | None = None  # 次の要素に適用する、引用（quote・backquote）または引用の解除（unquote）
    function_quote = False
    i, n, line = 0, len(text), 1

    def quoted_for_next() -> bool:
        inherited = stack[-1].quoted if stack else False
        if prefix in ("quote", "backquote"):
            return True
        if prefix == "unquote":
            return False
        return inherited

    def add(item: Atom | Form) -> None:
        if stack:
            stack[-1].items.append(item)
        elif isinstance(item, Form):
            forms.append(item)

    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r":
            i += 1
        elif c == ";":
            while i < n and text[i] != "\n":
                i += 1
        elif c == '"':
            start_line, j = line, i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                if j < n and text[j] == "\n":
                    line += 1
                j += 1
            if j >= n:
                raise ElispSyntaxError(f"文字列が閉じていません（{start_line}行目）")
            add(Atom(text[i + 1:j], start_line, "string", quoted=quoted_for_next()))
            prefix = None
            i = j + 1
        elif c == "?":
            j = _char_literal_end(text, i)
            add(Atom(text[i:j], line, "char"))
            prefix = None
            i = j
        elif c == "#" and text[i + 1:i + 2] == "'":
            function_quote = True
            i += 2
        elif c == "#" and text[i + 1:i + 2] in ("(", "["):
            i += 1  # #s( ... ) #( ... ) #[ ... ] の前置き。括弧の処理に任せる
        elif c in "'`":
            prefix = "quote" if c == "'" else "backquote"
            i += 1
        elif c == ",":
            prefix = "unquote"
            i += 2 if text[i:i + 2] == ",@" else 1
        elif c in "([":
            template = (prefix == "backquote") or (stack[-1].template and prefix != "unquote" if stack else False)
            form = Form(line=line, quoted=quoted_for_next(), vector=c == "[", template=template)
            add(form)
            stack.append(form)
            prefix = None
            i += 1
        elif c in ")]":
            if not stack:
                raise ElispSyntaxError(f"対応する開き括弧がありません（{line}行目）")
            stack.pop().end_line = line
            prefix = None
            i += 1
        else:
            match = _SYMBOL_CHARS.match(text, i)
            if match is None:
                i += 1
                continue
            word = match.group(0)
            kind = "number" if re.fullmatch(r"[-+]?\d+(\.\d+)?([eE][-+]?\d+)?", word) else "symbol"
            add(Atom(word, line, kind, quoted=quoted_for_next(), function_quote=function_quote))
            function_quote = False
            prefix = None
            i = match.end()
    if stack:
        raise ElispSyntaxError(f"括弧が閉じていません（{stack[0].line}行目から）")
    return forms


# --- 定義の名前・説明 ---


def _name_of(form: Form, style: str) -> str | None:
    if len(form.items) < 2:
        return None
    second = form.items[1]
    if style == "struct" and isinstance(second, Form) and second.items and isinstance(second.items[0], Atom):
        return second.items[0].text  # (cl-defstruct (name (:constructor ...)) ...)
    if style == "alias":
        return _quoted_symbol(second)  # (defalias 'name ...)
    if isinstance(second, Atom) and second.kind == "symbol":
        return second.text
    return None


def _docstring(form: Form, style: str) -> str:
    positions = {"defun": [3], "alias": [3], "mode": [2], "var": [3, 4], "group": [3], "face": [3], "struct": [2], "method": [3, 4, 5]}.get(style, [])
    for position in positions:
        if position < len(form.items):
            item = form.items[position]
            if isinstance(item, Atom) and item.kind == "string":
                return " ".join(item.text.split("\n")[0].split())[:160]
    return ""


def _header_summary(text: str) -> str:
    for line in text.splitlines()[:5]:
        match = _HEADER.match(line)
        if match:
            return match.group(1)
    return ""


def _quoted_symbol(item: Atom | Form) -> str | None:
    """`'name` または `(quote name)` の name（引用された記号）。それ以外は None。"""

    if isinstance(item, Atom) and item.kind == "symbol" and item.quoted:
        return item.text
    if isinstance(item, Form) and len(item.items) == 2 and isinstance(item.items[0], Atom) and item.items[0].text == "quote" and isinstance(item.items[1], Atom):
        return item.items[1].text
    return None


def _head_name(form: Form) -> str | None:
    head = form.items[0] if form.items else None
    return head.text if isinstance(head, Atom) and head.kind == "symbol" and not head.quoted else None


# --- 解析 ---

_CODING_COOKIE = re.compile(rb"coding[:=]\s*([-\w.]+)")
_EMACS_CODING_ALIASES = {"iso-latin-1": "latin-1", "latin-1": "latin-1", "iso-8859-1": "latin-1", "emacs-mule": "latin-1", "raw-text": "latin-1",
                         "no-conversion": "latin-1", "utf-8-emacs": "utf-8", "prefer-utf-8": "utf-8", "undecided": "utf-8"}


def _decode(content: bytes, result: FileAnalysis) -> str:
    """UTF-8で読めなければ、`-*- coding: … -*-` の指定、無ければ latin-1 で読む（行・桁は変わらない）。読み替えたことは警告に残す。"""

    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        pass
    cookie = _CODING_COOKIE.search(b"\n".join(content.splitlines()[:2]))
    candidates = []
    if cookie:
        name = cookie.group(1).decode("ascii", errors="ignore").lower()
        candidates.append(_EMACS_CODING_ALIASES.get(name, name))
    candidates.append("latin-1")
    for encoding in candidates:
        try:
            text = content.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        result.warnings.append(f"UTF-8ではないため、{encoding} として読み取りました（日本語などの文字は、正しく読めない可能性があります。行の位置は変わりません）。")
        return text
    return content.decode("latin-1")  # 到達しない（latin-1 は、すべてのバイトを読める）


_BLOCK_BEGIN = re.compile(r"^[ \t]*#\+begin_src[ \t]+(?:emacs-lisp|elisp)(?:[ \t]|$)", re.IGNORECASE)
_BLOCK_END = re.compile(r"^[ \t]*#\+end_src\b", re.IGNORECASE)
_NOWEB = re.compile(r"<<[^<>\n]*>>")
_ORG_COMMA_ESCAPE = re.compile(r"^([ \t]*),(?=\*|#\+)")


def org_elisp_blocks(text: str, warnings: list[str] | None = None) -> list[tuple[int, str]]:
    """Orgの `emacs-lisp` / `elisp` ソースブロックを、(本文の開始行, 行番号を保つための前置の改行つき本文) で返す。

    本文の行は元のファイルと同じ行番号になる。`<<名前>>`（noweb）は展開しない（空白に置き換え、警告に残す）。
    """

    lines = text.split("\n")
    blocks: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        if not _BLOCK_BEGIN.match(lines[index]):
            index += 1
            continue
        start = index + 1  # 本文の最初の行（0始まり）
        end = start
        while end < len(lines) and not _BLOCK_END.match(lines[end]):
            end += 1
        body = []
        for number in range(start, end):
            line = _ORG_COMMA_ESCAPE.sub(r"\1", lines[number])
            if _NOWEB.search(line):
                if warnings is not None:
                    warnings.append(f"{number + 1}行目: noweb参照 <<…>> は展開しません（その部分は空白として扱います）。")
                line = _NOWEB.sub(lambda m: " " * len(m.group(0)), line)
            body.append(line)
        blocks.append((start + 1, "\n" * start + "\n".join(body)))
        index = end + 1
    return blocks


class ElispAnalyzer:
    language = Language.ELISP

    def analyze_file(self, unit: SourceUnit) -> FileAnalysis:
        result = FileAnalysis()
        text = _decode(unit.content, result)
        is_org = unit.relative_path.lower().endswith(".org")
        try:
            if is_org:
                forms = self._org_forms(text, result)
                if forms is None:
                    return result
            else:
                forms = read_forms(text)
        except ElispSyntaxError as exc:
            result.errors.append(f"構文解析に失敗しました: {exc}")
            return result
        stem = PurePosixPath(unit.relative_path).stem
        ids = IdAllocator(unit.file_id)
        module = build_symbol(
            ids, unit.file_id, name=stem, qualified_name=stem, kind=SymbolKind.MODULE,
            start_line=1, end_line=len(text.splitlines()) or 1, summary="" if is_org else _header_summary(text),
        )
        result.symbols.append(module)
        collector = _Collector(unit, ids, result, module, _defined_variables(forms))
        for form in forms:
            collector.top_level(form)
            collector.dependencies(form)
        return result

    @staticmethod
    def _org_forms(text: str, result: FileAnalysis) -> list[Form] | None:
        """Orgのemacs-lispブロックごとに読む。閉じていないブロックは、そのブロックだけ飛ばして警告に残す。"""

        blocks = org_elisp_blocks(text, result.warnings)
        forms: list[Form] = []
        failed = 0
        for line, body in blocks:
            try:
                forms.extend(read_forms(body))
            except ElispSyntaxError as exc:
                failed += 1
                result.warnings.append(f"{line}行目から始まるブロックは読み取れず、解析対象から除きました: {exc}")
        if blocks and failed == len(blocks):
            result.errors.append("すべての emacs-lisp ブロックの構文解析に失敗しました。")
            return None
        if not blocks:
            result.warnings.append("emacs-lisp / elisp のソースブロックがありません（Orgの本文は解析しません）。")
        return forms


def _defined_variables(forms: list[Form]) -> set[str]:
    """このファイルで定義された変数の名前。変数の参照は、局所変数との区別が静的にできないため、これに限って記録する。"""

    names: set[str] = set()
    for form in forms:
        for node in _all_forms(form):
            definer = _head_name(node)
            if definer in _DEFINERS and _DEFINERS[definer][1] == "var":
                name = _name_of(node, "var")
                if name:
                    names.add(name)
    return names


class _Collector:
    def __init__(self, unit: SourceUnit, ids: IdAllocator, result: FileAnalysis, module: Symbol, variables: set[str]) -> None:
        self._variables = variables
        self._in_macro = False
        self._unit = unit
        self._ids = ids
        self._result = result
        self._module = module

    # --- トップレベル ---

    def top_level(self, form: Form) -> None:
        name = _head_name(form)
        if name in _DEFINERS and not form.quoted:
            self._definition(form, name)
        elif name in _WRAPPERS and not form.quoted and any(isinstance(i, Form) and self._defines(i) for i in form.items[1:]):
            # 条件付きの定義（プラットフォーム分岐など）や、読み込み時評価の中の定義。どれが実行されるかは静的に決まらない
            for item in form.items[1:]:
                if isinstance(item, Form) and self._defines(item):
                    self.top_level(item)
                else:
                    self._walk(item, self._module)
        else:
            self._walk(form, self._module)

    def _defines(self, form: Form) -> bool:
        name = _head_name(form)
        return bool(name) and not form.quoted and (name in _DEFINERS or (name in _WRAPPERS and any(isinstance(i, Form) and self._defines(i) for i in form.items[1:])))

    def _definition(self, form: Form, definer: str) -> None:
        kind, style = _DEFINERS[definer]
        name = _name_of(form, style)
        if name is None:
            self._walk(form, self._module)
            return
        symbol = build_symbol(
            self._ids, self._unit.file_id, name=name, qualified_name=name, kind=kind, start_line=form.line, end_line=form.end_line,
            parent=self._module, summary=_FORM_LABEL.get(style, "") + _docstring(form, style),
        )
        self._result.symbols.append(symbol)
        if style in ("group", "face", "struct"):
            return  # 設定の宣言・スロットの定義。本体に、呼び出しは無い
        self._in_macro = kind == SymbolKind.MACRO
        if style == "alias":
            target = _quoted_symbol(form.items[2]) if len(form.items) >= 3 else None
            if target:
                self._reference(ReferenceKind.FUNCTION_REF, target, form.line, symbol)
            return
        if style == "defun":
            body = form.items[3:]  # 名前の次は、引数リスト
        elif style == "method":  # (cl-defmethod name [:qualifier] ((arg type) ...) body)
            arglist = next((k for k, item in enumerate(form.items[2:], 2) if isinstance(item, Form)), len(form.items) - 1)
            body = form.items[arglist + 1:]
        else:
            body = form.items[2:]
        for item in body:
            self._walk(item, symbol)
        self._in_macro = False

    # --- require / load ---

    def dependencies(self, form: Form) -> None:
        for node in _all_forms(form):
            name = _head_name(node)
            if name == "require" and len(node.items) >= 2:
                argument = node.items[1]
                feature = _quoted_symbol(argument)
                if feature:
                    self._dependency(feature, node, KEY_FEATURE + feature)
                else:
                    self._dependency(argument.text if isinstance(argument, Atom) else "(式)", node, None, "featureが変数・式で指定されており、静的に確定できない")
            elif name in ("load", "load-file") and len(node.items) >= 2 and isinstance(node.items[1], Atom) and node.items[1].kind == "string":
                target = node.items[1].text
                self._dependency(target, node, KEY_FEATURE + PurePosixPath(target).stem, "load による読み込み（ファイル名の末尾で解決）")

    def _dependency(self, name: str, node: Form, key: str | None, note: str = "") -> None:
        self._result.dependencies.append(
            Dependency(
                dependency_id=self._ids.dependency_id("import", name, node.line), source_file_id=self._unit.file_id, target_name=name,
                target_key=key, dependency_kind=DependencyKind.IMPORT, evidence_location=SourceLocation(self._unit.file_id, node.line, node.end_line),
                resolution_status=ResolutionStatus.UNRESOLVED, note=note,
            )
        )

    # --- 式の走査（呼び出し・関数への参照・変数の参照） ---

    def _walk(self, item: Atom | Form, owner: Symbol) -> None:
        if isinstance(item, Atom):
            self._atom(item, owner)
        elif item.quoted:
            self._quoted(item, owner)
        elif item.vector or not item.items:
            for sub in item.items:
                self._walk(sub, owner)
        else:
            self._form(item, owner)

    def _form(self, form: Form, owner: Symbol) -> None:
        head = form.items[0]
        name = _head_name(form)
        rest = form.items[1:]
        if name is None:  # ((lambda ...) args) など、頭が式
            for sub in form.items:
                self._walk(sub, owner)
            return
        if name in ("quote", "function"):
            for sub in rest:
                if isinstance(sub, Atom) and name == "function":
                    self._reference(ReferenceKind.FUNCTION_REF, sub.text, sub.line, owner)
                elif isinstance(sub, Form):
                    self._quoted(sub, owner)
            return
        if name == "lambda":
            for sub in rest[1:]:  # 引数リストは、式ではない
                self._walk(sub, owner)
            return
        if name in _BINDING_FORMS:
            self._bindings(rest[0] if rest else None, owner)
            for sub in rest[1:]:
                self._walk(sub, owner)
            return
        if name in _IF_LET_FORMS:
            self._bindings(rest[0] if rest else None, owner, allow_bare_expression=True)
            for sub in rest[1:]:
                self._walk(sub, owner)
            return
        if name == "cond":
            for clause in rest:
                self._sequence(clause, owner)
            return
        if name == "condition-case":
            for sub in rest[1:2]:
                self._walk(sub, owner)
            for handler in rest[2:]:
                if isinstance(handler, Form):
                    for sub in handler.items[1:]:  # 先頭はエラーの種類
                        self._walk(sub, owner)
            return
        if name in ("dolist", "dotimes", "cl-dolist", "cl-dotimes"):
            spec = rest[0] if rest else None
            if isinstance(spec, Form):
                for sub in spec.items[1:]:
                    self._walk(sub, owner)
            for sub in rest[1:]:
                self._walk(sub, owner)
            return
        if name in _CLAUSE_FORMS:
            if name.startswith("pcase-let"):  # ((パターン 式) ...) 本体...
                self._bindings(rest[0] if rest else None, owner)
                for sub in rest[1:]:
                    self._walk(sub, owner)
                return
            for position, sub in enumerate(rest):
                if position == 0:
                    self._walk(sub, owner)
                elif isinstance(sub, Form) and not sub.quoted:
                    for clause_item in sub.items[1:]:  # 先頭は、パターン・キー
                        self._walk(clause_item, owner)
            return
        if name in _LOCAL_FUNCTION_FORMS:
            bindings = rest[0] if rest else None
            if isinstance(bindings, Form):
                for binding in bindings.items:
                    if isinstance(binding, Form):
                        for sub in binding.items[2:]:  # (名前 引数 本体...)
                            self._walk(sub, owner)
            for sub in rest[1:]:
                self._walk(sub, owner)
            return
        if name in _SKIP_FIRST_FORMS:
            for sub in rest[1:]:
                self._walk(sub, owner)
            return
        if name in ("setq", "setq-local", "setq-default"):
            for position, sub in enumerate(rest):
                if position % 2 == 1:
                    self._walk(sub, owner)
                elif isinstance(sub, Atom):
                    self._atom(sub, owner)
            return
        self._head(head, form, owner)
        funcalled = name in ("funcall", "apply", "mapcar", "mapc", "mapcan", "mapconcat", "add-hook", "remove-hook", "advice-add", "run-with-timer",
                             "run-with-idle-timer", "sort", "seq-filter", "seq-map", "cl-remove-if", "cl-remove-if-not", "cl-find-if", "cl-some", "cl-every")
        for sub in rest:
            if funcalled and isinstance(sub, Form) and sub.quoted and len(sub.items) == 2 and isinstance(sub.items[0], Atom) and sub.items[0].text == "quote":
                continue
            if funcalled and isinstance(sub, Atom) and sub.quoted and sub.kind == "symbol" and not sub.text.startswith(":"):
                self._reference(ReferenceKind.FUNCTION_REF, sub.text, sub.line, owner)  # (mapcar 'func xs) の 'func は関数への参照
                continue
            self._walk(sub, owner)

    def _bindings(self, bindings: Atom | Form | None, owner: Symbol, allow_bare_expression: bool = False) -> None:
        """`let` の束縛リスト。変数名は参照にせず、値の式だけを走査する。"""

        if not isinstance(bindings, Form):
            return
        for binding in bindings.items:
            if isinstance(binding, Form):
                first = binding.items[0] if binding.items else None
                if isinstance(first, Form) or (allow_bare_expression and len(binding.items) == 1):
                    for sub in binding.items:
                        self._walk(sub, owner)
                else:
                    for sub in binding.items[1:]:
                        self._walk(sub, owner)

    def _sequence(self, item: Atom | Form, owner: Symbol) -> None:
        """節（`cond` の節、`pcase` の分岐など）。先頭は呼び出しではなく、式の並びとして扱う。"""

        if isinstance(item, Form) and not item.quoted and not item.vector:
            for sub in item.items:
                self._walk(sub, owner)
        else:
            self._walk(item, owner)

    def _quoted(self, form: Form, owner: Symbol) -> None:
        """引用された式の中。`#'` で関数と明示されたもの以外は、参照にしない（データの可能性がある）。`,x` で評価される式は走査する。"""

        if self._in_macro and form.template and not form.vector:
            if form.items and isinstance(form.items[0], Atom):
                self._head(form.items[0], form, owner, in_template=True)  # マクロのテンプレート: 展開後の呼び出し
        for sub in form.items:
            if isinstance(sub, Atom) and sub.function_quote:
                self._reference(ReferenceKind.FUNCTION_REF, sub.text, sub.line, owner)
            elif isinstance(sub, Form):
                if sub.quoted:
                    self._quoted(sub, owner)
                else:
                    self._walk(sub, owner)  # バッククォートの中の `,(式)`
            elif isinstance(sub, Atom) and not sub.quoted and sub.kind == "symbol":
                self._atom(sub, owner)  # `,変数`

    def _head(self, head: Atom | Form, form: Form, owner: Symbol, in_template: bool = False) -> None:
        if not isinstance(head, Atom) or head.kind != "symbol" or (head.quoted and not in_template):
            return
        name = head.text
        if name in _NO_CALL or name.startswith((":", "&")) or form.vector:
            return
        self._reference(ReferenceKind.CALL, name, head.line, owner, form.end_line)

    def _atom(self, atom: Atom, owner: Symbol) -> None:
        if atom.kind != "symbol":
            return
        if atom.function_quote:
            self._reference(ReferenceKind.FUNCTION_REF, atom.text, atom.line, owner)
        elif not atom.quoted and atom.text in self._variables:
            self._reference(ReferenceKind.VARIABLE_REF, atom.text, atom.line, owner)

    def _reference(self, kind: ReferenceKind, name: str, start: int, owner: Symbol, end: int | None = None) -> None:
        key = (KEY_VAR if kind == ReferenceKind.VARIABLE_REF else KEY_CALL) + name
        self._result.references.append(
            Reference(
                reference_id=self._ids.reference_id(owner.symbol_id, kind.value, key, start), source_symbol_id=owner.symbol_id,
                target_name=name, target_key=key, reference_kind=kind,
                source_location=SourceLocation(self._unit.file_id, start, end or start), resolution_status=ResolutionStatus.UNRESOLVED,
            )
        )


def _all_forms(form: Form):
    yield form
    for item in form.items:
        if isinstance(item, Form):
            yield from _all_forms(item)

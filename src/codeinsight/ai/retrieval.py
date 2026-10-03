"""質問に関連するシンボルの機械的な検索（AIには頼らない）。

名前・パス・docstring・本文（識別子・文字列・コメント）の語を、BM25風に重み付けして順位を付ける。
- 識別子は snake_case / camelCase を語に分割し、単純な複数形・活用を正規化する。
- 日本語の質問は、プログラムでよく使う用語を英語の語に対応づける小さな辞書で補う（決定論的）。
- 解析時と内容が異なるファイルの本文は、使わない（名前・docstringのみで順位を付ける）。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable

from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import Symbol, SymbolKind

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
# 日本語は、文字種（漢字・カタカナ）ごとの連続を語とする。ひらがなは助詞・活用が多いため語として使わない。
_JAPANESE_WORD = re.compile(r"[一-龠々]{2,}|[ァ-ヶー]{2,}")
_STOP = frozenset({
    "the", "and", "for", "how", "what", "does", "this", "that", "with", "from", "into", "are", "not", "where", "when",
    "which", "who", "why", "is", "was", "do", "in", "of", "to", "a", "an", "it", "by", "on", "or", "be", "as", "at", "through", "used",
})
_GENERIC_JAPANESE = frozenset({
    "処理", "説明", "仕組", "実装", "関数", "動作", "機能", "方法", "場合", "内容", "部分", "全体", "クラス", "メソッド",
    "コード", "ソース", "プログラム", "箇所", "場所", "呼び出",
})

# 日本語の用語 → 英語の語（識別子・文字列に現れやすいもの）。完全な辞書ではなく、補助である。
GLOSSARY: dict[str, tuple[str, ...]] = {
    "保存": ("save", "store", "write", "dump", "persist", "insert"),
    "永続": ("persist", "save", "store", "database", "db"),
    "読み込": ("read", "load", "open", "parse"),
    "読込": ("read", "load", "open"),
    "書き込": ("write", "dump", "save"),
    "書き換": ("write", "set", "update", "assign", "global"),
    "更新": ("update", "set", "modify"),
    "削除": ("delete", "remove", "drop", "unlink"),
    "注文": ("order",),
    "通知": ("notify", "notification", "mail", "send", "post", "hook"),
    "メール": ("mail", "smtp", "email"),
    "送信": ("send", "post", "submit", "request"),
    "受信": ("receive", "recv", "read", "handle"),
    "環境変数": ("environ", "getenv", "env", "environment"),
    "設定": ("config", "setting", "settings", "option", "default", "set"),
    "例外": ("raise", "except", "exception", "error"),
    "エラー": ("error", "exception", "raise", "fail"),
    "再帰": ("recursion", "recursive", "fib", "factorial"),
    "引数": ("arg", "args", "argument", "param", "parameter", "argparse"),
    "コマンド": ("command", "cmd", "subprocess", "argparse", "cli"),
    "コマンドライン": ("argparse", "cli", "argv", "parser", "command"),
    "パーサー": ("parser", "parse", "argparse"),
    "スレッド": ("thread", "threading", "executor", "pool"),
    "非同期": ("async", "await", "asyncio", "coroutine"),
    "並行": ("thread", "async", "concurrent", "executor", "pool"),
    "認証": ("auth", "login", "token", "password", "credential"),
    "検証": ("validate", "verify", "check", "assert"),
    "作成": ("create", "build", "new", "init"),
    "生成": ("create", "generate", "build"),
    "実行": ("run", "exec", "execute", "call", "start", "subprocess"),
    "起動": ("start", "launch", "run", "spawn", "thread"),
    "取得": ("get", "fetch", "read", "retrieve", "load"),
    "リトライ": ("retry", "retries", "attempt", "backoff"),
    "再試行": ("retry", "retries", "attempt"),
    "タイムアウト": ("timeout",),
    "キャッシュ": ("cache", "memo", "lru"),
    "ログ": ("log", "logger", "logging"),
    "データベース": ("database", "db", "sql", "sqlite", "connection", "insert"),
    "ファイル": ("file", "open", "path", "read", "write"),
    "カウンタ": ("counter", "count", "incr", "increment"),
    "カウンター": ("counter", "count", "incr", "increment"),
    "増や": ("incr", "increment", "add", "increase", "inc"),
    "減ら": ("decr", "decrement", "sub", "decrease"),
    "グローバル": ("global",),
    "フィボナッチ": ("fib", "fibonacci"),
    "関数ポインタ": ("pointer", "callback", "op", "fn", "func", "apply"),
    "ポインタ": ("pointer", "ptr", "callback"),
    "構造体": ("struct",),
    "初期化": ("init", "setup", "initialize", "create"),
    "終了": ("exit", "close", "shutdown", "stop", "finish"),
    "接続": ("connect", "connection", "socket", "client"),
    "入力": ("input", "read", "stdin", "arg", "request"),
    "出力": ("output", "print", "write", "stdout", "render"),
    "表示": ("print", "show", "display", "render"),
    "変換": ("convert", "transform", "map", "parse", "encode"),
    "危険": ("eval", "exec", "pickle", "shell", "unsafe"),
}

_WEIGHT_NAME = 4.0
_WEIGHT_PATH = 1.5
_WEIGHT_DOC = 2.0
_WEIGHT_BODY = 1.0


def normalize(word: str) -> str:
    """語を、単純な複数形・活用の違いを吸収した形にする（厳密な語幹抽出ではない）。"""

    word = word.lower()
    if word.endswith(("ss", "us", "is")):
        return word  # class・status・analysis など、語尾の s は複数形ではない
    for suffix, replacement in (("ies", "y"), ("ied", "y"), ("sses", "ss"), ("ing", ""), ("ed", ""), ("es", ""), ("s", "")):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if suffix in ("ing", "ed", "es") and len(word) - len(suffix) < 4:
                continue
            return word[: len(word) - len(suffix)] + replacement
    return word


def split_identifier(identifier: str) -> list[str]:
    """識別子を語に分割する（snake_case・camelCase）。分割前の全体も語として残す。"""

    parts = [p for chunk in identifier.split("_") for p in _CAMEL.findall(chunk)]
    words = [normalize(p) for p in parts if p]
    whole = normalize(identifier.replace("_", ""))
    if len(words) > 1 and whole:
        words.append(whole)
    return words


def _terms(text: str) -> list[str]:
    result: list[str] = []
    for identifier in _IDENT.findall(text):
        result.extend(split_identifier(identifier))
    return [t for t in result if len(t) >= 2 and t not in _STOP]


def query_terms(question: str) -> tuple[set[str], set[str]]:
    """質問から、(英語の語, 日本語の語) を取り出す。日本語の用語は辞書で英語の語にも展開する。"""

    latin: set[str] = set()
    for identifier in _IDENT.findall(question):
        if identifier.lower() in _STOP:
            continue
        for word in split_identifier(identifier):
            if len(word) >= 3 and word not in _STOP:
                latin.add(word)
    japanese = {w for w in _JAPANESE_WORD.findall(question) if w not in _GENERIC_JAPANESE}
    expanded: set[str] = set()
    for term in sorted(GLOSSARY, key=len, reverse=True):
        if term in question:
            expanded.update(normalize(w) for w in GLOSSARY[term])
    return latin | expanded, japanese


class _Document:
    __slots__ = ("symbol", "fields", "is_test", "body_length")

    def __init__(self, symbol: Symbol, fields: dict[str, Counter[str]], is_test: bool) -> None:
        self.symbol = symbol
        self.fields = fields
        self.is_test = is_test
        self.body_length = sum(fields["body"].values())


ReadLines = Callable[[str], list[str] | None]


def retrieve_symbols(index: ProjectIndex, question: str, limit: int = 4, read_lines: ReadLines | None = None) -> list[Symbol]:
    """質問に関連するシンボルを、順位の高い順に返す。

    read_lines は、相対パスから（解析時と一致する）ファイルの行を返す関数。None の場合や、
    None を返したファイルでは、名前・パス・docstringだけで順位を付ける。
    """

    latin, japanese = query_terms(question)
    if not latin and not japanese:
        return []

    documents = _build_documents(index, read_lines)
    if not documents:
        return []
    count = len(documents)
    average_body = max(sum(d.body_length for d in documents) / count, 1.0)
    asks_about_tests = any(word in question.lower() for word in ("test", "テスト"))
    document_frequency: Counter[str] = Counter()
    for document in documents:
        seen: set[str] = set()
        for counter in document.fields.values():
            seen.update(counter)
        document_frequency.update(seen)

    scored: list[tuple[float, Symbol]] = []
    for document in documents:
        score = 0.0
        matched = 0
        for term in latin:
            frequency = document_frequency.get(term, 0)
            if not frequency:
                continue
            idf = math.log(1 + (count - frequency + 0.5) / (frequency + 0.5))
            # 本文は、長いシンボルほど語が偶然現れやすいので、長さで正規化する（BM25のb=0.75）
            body = document.fields["body"].get(term, 0) / (0.25 + 0.75 * document.body_length / average_body)
            weighted = (
                _WEIGHT_NAME * document.fields["name"].get(term, 0)
                + _WEIGHT_PATH * document.fields["path"].get(term, 0)
                + _WEIGHT_DOC * document.fields["doc"].get(term, 0)
                + _WEIGHT_BODY * body
            )
            if weighted:
                matched += 1
                score += idf * (weighted * 2.2) / (weighted + 1.2)  # 出現回数の飽和（BM25）
        summary = document.symbol.summary
        for term in japanese:
            if term in summary:
                matched += 1
                score += 3.0
        if not score:
            continue
        score *= 1 + 0.15 * (matched - 1)  # 質問の語を多く含むものを優先する
        if document.symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS):
            score *= 1.1
        if document.is_test and not asks_about_tests:
            score *= 0.4  # 実装を尋ねる質問に、テストコードが上位を占めないようにする
        if document.symbol.name == "__init__":
            score *= 0.7
        scored.append((score, document.symbol))
    scored.sort(key=lambda item: (-item[0], item[1].qualified_name))
    return [symbol for _, symbol in scored[:limit]]


_SKIPPED_KINDS = (SymbolKind.LOCAL_VARIABLE, SymbolKind.MODULE, SymbolKind.FUNCTION_DECLARATION, SymbolKind.MACRO)


def _build_documents(index: ProjectIndex, read_lines: ReadLines | None) -> list[_Document]:
    """シンボルごとに、名前・パス・docstring・本文の語の出現回数を数える。"""

    file_lines: dict[str, list[str] | None] = {}
    children: dict[str, list[Symbol]] = {}
    for symbol in index.symbols.values():
        if symbol.parent_symbol_id:
            children.setdefault(symbol.parent_symbol_id, []).append(symbol)

    documents: list[_Document] = []
    for symbol in index.symbols.values():
        if symbol.kind in _SKIPPED_KINDS:
            continue
        path = index.path_of(symbol.file_id)
        fields: dict[str, Counter[str]] = {
            "name": Counter(split_identifier(symbol.name)),
            "path": Counter(_terms(path.replace("/", "_").replace(".", "_"))),
            "doc": Counter(_terms(symbol.summary)),
            "body": Counter(),
        }
        if read_lines is not None:
            if path not in file_lines:
                file_lines[path] = read_lines(path)
            lines = file_lines[path]
            if lines is not None:
                fields["body"].update(_body_terms(lines, symbol, children.get(symbol.symbol_id, [])))
        documents.append(_Document(symbol, fields, _is_test_path(path)))
    return documents


def _is_test_path(path: str) -> bool:
    parts = path.split("/")
    return any(p in ("tests", "test") for p in parts[:-1]) or parts[-1].startswith("test_") or parts[-1].endswith("_test.py")


def _body_terms(lines: list[str], symbol: Symbol, nested: list[Symbol]) -> list[str]:
    """シンボル自身の本文の語。入れ子のシンボル（クラス内のメソッドなど）の範囲は、その子に任せて除く。"""

    skip: set[int] = set()
    for child in nested:
        skip.update(range(child.start_line, child.end_line + 1))
    terms: list[str] = []
    for number in range(symbol.start_line, min(symbol.end_line, len(lines)) + 1):
        if number in skip:
            continue
        terms.extend(_terms(lines[number - 1]))
    return terms

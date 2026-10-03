from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from codeinsight.application.project_index import ProjectIndex
from codeinsight.domain import (
    DependencyKind,
    Language,
    Reference,
    ReferenceKind,
    ResolutionStatus,
    Symbol,
)

# カテゴリ（外部システムとの接続・副作用の種類）。照合は、最長一致の接頭辞で行う。
CATEGORY_LABELS = {
    "network": "ネットワーク/HTTP",
    "database": "データベース",
    "filesystem": "ファイルシステム",
    "process": "外部プロセス/OS",
    "persistence": "永続化・シリアライズ",
    "concurrency": "並行・非同期",
    "cache": "キャッシュ",
    "gui": "GUI/イベント",
    "config": "設定・環境変数・CLI引数",
    "logging": "ログ出力",
    "crypto": "暗号・ハッシュ",
    "scraping": "スクレイピング/ブラウザ操作",
    "other": "その他の外部ライブラリ",
}

_PYTHON_PREFIXES: dict[str, str] = {
    # network
    **{p: "network" for p in (
        "requests", "httpx", "urllib", "urllib3", "http", "socket", "ssl", "aiohttp", "websocket",
        "websockets", "ftplib", "smtplib", "imaplib", "poplib", "telnetlib", "xmlrpc", "grpc",
        "flask", "fastapi", "django", "starlette", "tornado", "uvicorn", "paramiko", "boto3", "botocore",
        "socketserver", "wsgiref")},
    # database
    **{p: "database" for p in (
        "sqlite3", "sqlalchemy", "psycopg2", "psycopg", "pymysql", "mysql", "pymongo", "redis", "peewee",
        "asyncpg", "elasticsearch", "tortoise", "dbm", "shelve")},
    # filesystem
    **{p: "filesystem" for p in (
        "pathlib", "shutil", "glob", "tempfile", "fnmatch", "zipfile", "tarfile", "fileinput", "os.path",
        "os.remove", "os.unlink", "os.rename", "os.replace", "os.makedirs", "os.mkdir", "os.rmdir",
        "os.listdir", "os.walk", "os.scandir", "os.stat", "os.chmod", "os.getcwd", "os.chdir", "io",
        "gzip", "bz2", "lzma")},
    "builtins.open": "filesystem",
    "open": "filesystem",
    # process / OS
    **{p: "process" for p in (
        "subprocess", "multiprocessing", "os.system", "os.popen", "os.exec", "os.spawn", "os.fork",
        "os.kill", "signal", "sys.exit", "platform", "shlex")},
    # persistence
    **{p: "persistence" for p in (
        "json", "pickle", "marshal", "csv", "plistlib", "yaml", "toml", "tomllib", "tomli_w", "xml",
        "configparser")},
    # concurrency
    **{p: "concurrency" for p in ("threading", "concurrent", "asyncio", "queue", "sched", "trio", "anyio")},
    # cache
    **{p: "cache" for p in ("functools.lru_cache", "functools.cache", "cachetools", "diskcache")},
    # gui / events
    **{p: "gui" for p in ("PySide6", "PySide2", "PyQt5", "PyQt6", "tkinter", "wx", "kivy", "pygame")},
    # config
    **{p: "config" for p in (
        "os.environ", "os.getenv", "os.putenv", "argparse", "click", "typer", "optparse", "getopt", "dotenv",
        "sys.argv")},
    # logging
    **{p: "logging" for p in ("logging", "loguru", "structlog", "warnings", "print", "builtins.print")},
    # crypto
    **{p: "crypto" for p in ("hashlib", "hmac", "secrets", "cryptography", "Crypto", "nacl", "jwt")},
    # scraping
    **{p: "scraping" for p in ("bs4", "lxml", "scrapy", "selenium", "playwright", "mechanize")},
}

_C_HEADER_PREFIXES: dict[str, str] = {
    **{p: "network" for p in ("sys/socket.h", "netinet/", "arpa/inet.h", "netdb.h", "curl/", "microhttpd.h")},
    **{p: "database" for p in ("sqlite3.h", "mysql/", "libpq-fe.h", "hiredis/")},
    **{p: "filesystem" for p in ("fcntl.h", "dirent.h", "sys/stat.h", "sys/types.h", "unistd.h", "stdio.h")},
    **{p: "process" for p in ("sys/wait.h", "signal.h", "spawn.h", "stdlib.h")},
    **{p: "concurrency" for p in ("pthread.h", "semaphore.h", "threads.h", "stdatomic.h", "sys/epoll.h", "sys/select.h", "poll.h")},
    **{p: "crypto" for p in ("openssl/", "sodium.h", "gcrypt.h")},
    **{p: "persistence" for p in ("jansson.h", "cJSON.h", "yaml.h", "libxml/", "expat.h")},
    **{p: "logging" for p in ("syslog.h",)},
    **{p: "gui" for p in ("gtk/", "SDL", "X11/", "GL/")},
}

# 操作の種類。カテゴリ（何に触れるか）とは別に、「何をするか」を最長一致の接頭辞で分類する。
#   pure   : 外部へ触れない（文字列・パスの計算、パース、例外クラス等）
#   read   : 外部から読み取る        write  : 外部へ書き込む・作る・消す
#   effect : ネットワーク・プロセス等の外部呼び出し    output : 標準出力・ログへの出力
#   io     : 読み書きのどちらか静的に分からない（open等）
_OPERATIONS: dict[str, str] = {
    **{p: "pure" for p in (
        "urllib.parse", "pathlib.Path", "pathlib.PurePath", "pathlib.Path.resolve", "pathlib.Path.with_suffix",
        "pathlib.Path.with_name", "pathlib.Path.with_stem", "pathlib.Path.joinpath", "pathlib.Path.expanduser",
        "pathlib.Path.absolute", "pathlib.Path.relative_to", "pathlib.Path.as_posix", "pathlib.Path.is_absolute",
        "pathlib.Path.parent", "pathlib.Path.name", "pathlib.Path.suffix", "pathlib.Path.stem", "pathlib.Path.parts",
        "os.path.join", "os.path.basename", "os.path.dirname", "os.path.splitext", "os.path.abspath",
        "os.path.normpath", "os.path.relpath", "os.path.expanduser", "os.path.split", "os.path.isabs",
        "json.loads", "json.dumps", "hashlib", "hmac", "base64", "shlex", "fnmatch", "logging.getLogger",
        "io.StringIO", "io.BytesIO", "argparse", "functools", "itertools", "platform", "sys.argv",
        "os.getcwd", "os.path.sep", "tempfile.gettempdir")},
    **{p: "read" for p in (
        "pathlib.Path.read_text", "pathlib.Path.read_bytes", "pathlib.Path.exists", "pathlib.Path.is_file",
        "pathlib.Path.is_dir", "pathlib.Path.iterdir", "pathlib.Path.glob", "pathlib.Path.rglob",
        "pathlib.Path.stat", "pathlib.Path.samefile", "os.listdir", "os.walk", "os.scandir", "os.stat",
        "os.path.exists", "os.path.isfile", "os.path.isdir", "os.path.getsize", "os.path.getmtime",
        "os.path.realpath", "json.load", "glob.glob", "glob.iglob", "os.environ", "os.getenv")},
    **{p: "write" for p in (
        "pathlib.Path.write_text", "pathlib.Path.write_bytes", "pathlib.Path.mkdir", "pathlib.Path.unlink",
        "pathlib.Path.rmdir", "pathlib.Path.rename", "pathlib.Path.touch", "pathlib.Path.chmod",
        "pathlib.Path.symlink_to", "pathlib.Path.hardlink_to", "pathlib.Path.replace", "shutil.copy",
        "shutil.copy2", "shutil.copyfile", "shutil.copytree", "shutil.move", "shutil.rmtree", "os.remove",
        "os.unlink", "os.rename", "os.replace", "os.makedirs", "os.mkdir", "os.rmdir", "os.chmod", "os.chown",
        "json.dump", "pickle.dump", "tempfile.mkdtemp", "tempfile.mkstemp", "tempfile.NamedTemporaryFile",
        "tempfile.TemporaryDirectory", "os.environ.setdefault", "os.putenv")},
    **{p: "effect" for p in (
        "requests.get", "requests.post", "requests.put", "requests.delete", "requests.patch", "requests.head",
        "requests.request", "requests.Session.get", "requests.Session.post", "requests.Session.put",
        "requests.Session.delete", "requests.Session.patch", "requests.Session.head", "requests.Session.request",
        "urllib.request", "http.client", "socket", "smtplib", "httpx", "aiohttp", "websockets", "websocket",
        "ftplib", "subprocess", "os.system", "os.popen", "os.exec", "os.spawn", "os.kill", "os.fork", "sys.exit",
        "signal", "multiprocessing", "threading.Thread", "asyncio.run", "asyncio.create_task",
        "sqlite3.connect", "sqlite3.Connection.execute", "sqlite3.Connection.executemany",
        "sqlite3.Connection.commit", "psycopg2.connect", "pymysql.connect", "pymongo", "redis", "boto3")},
    **{p: "output" for p in ("print", "builtins.print", "logging", "warnings.warn", "sys.stdout", "sys.stderr")},
    **{p: "io" for p in ("open", "builtins.open", "pathlib.Path.open", "zipfile.ZipFile", "tarfile.open", "gzip.open",
                         "io.open", "codecs.open", "shelve.open", "dbm.open")},
}
EFFECT_OPERATIONS = ("write", "effect", "output", "io")
INPUT_OPERATIONS = ("read", "io", "effect")
_EXCEPTION_SUFFIXES = ("Error", "Exception", "Warning", "Exit", "Interrupt")


def classify_operation(name: str) -> str:
    """外部名の操作の種類（pure/read/write/effect/output/io）。未知のものは "call"。"""

    last = name.rsplit(".", 1)[-1]
    if last.endswith(_EXCEPTION_SUFFIXES) and last[:1].isupper():
        return "pure"  # 例外クラスの参照（送出・捕捉の記述）は、外部への操作ではない
    best: tuple[int, str] | None = None
    for prefix, operation in _OPERATIONS.items():
        if name == prefix or name.startswith(prefix + "."):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), operation)
    return best[1] if best else "call"


# 副作用の可能性が高い呼び出し（名前での判定。外部の関数名・メソッド名が一致するもの）。
_EFFECT_METHODS = {
    "write_text": "ファイルへの書き込み", "write_bytes": "ファイルへの書き込み", "mkdir": "ディレクトリ作成",
    "unlink": "ファイル削除", "rmdir": "ディレクトリ削除", "touch": "ファイル作成", "chmod": "権限変更",
    "symlink_to": "リンク作成",
}  # `replace`/`rename` は str.replace 等と区別できないため、メソッド名だけの推定には含めない
_READ_METHODS = {
    "read_text": "ファイルの読み込み", "read_bytes": "ファイルの読み込み", "iterdir": "ディレクトリの列挙",
    "rglob": "ディレクトリの走査", "glob": "ディレクトリの走査", "is_file": "ファイルの確認",
    "is_dir": "ディレクトリの確認", "exists": "存在の確認",
}  # 変数の型が分からない場合の、メソッド名による推定（Path以外の同名メソッドの可能性がある）


@dataclass(frozen=True)
class ExternalUse:
    category: str
    library: str  # 外部名（例: requests.get, sqlite3, stdio.h）
    operation: str  # pure / read / write / effect / output / io / call（未分類）
    source_id: str | None  # 使っているシンボル（import/includeはファイル単位なのでモジュールシンボル、無ければNone）
    owner: str  # 使っているシンボルの修飾名（無ければファイルのパス）
    path: str
    line: int
    kind: str  # import / call / include
    confidence: str  # confirmed（名前解決済み）/ inferred（メソッド名などからの推定）


@dataclass
class ExternalReport:
    uses: list[ExternalUse] = field(default_factory=list)

    def by_category(self) -> dict[str, list[ExternalUse]]:
        grouped: dict[str, list[ExternalUse]] = defaultdict(list)
        for use in self.uses:
            grouped[use.category].append(use)
        return grouped


SIDE_EFFECT_CATEGORIES = ("network", "database", "filesystem", "process", "persistence", "logging", "gui")


@dataclass
class EffectSummary:
    """関数が（直接・呼び出しを介して）外部へ及ぼしうる副作用の候補。"""

    symbol: Symbol
    direct: list[ExternalUse] = field(default_factory=list)
    reachable: list[tuple[ExternalUse, list[str]]] = field(default_factory=list)  # (使用箇所, 呼び出し経路)
    reads_direct: list[ExternalUse] = field(default_factory=list)  # 外部からの読み取り（入力）
    reads_reachable: list[tuple[ExternalUse, list[str]]] = field(default_factory=list)
    unresolved_calls: int = 0  # 呼び出し先を特定できず、副作用を追えない呼び出しの数


def categorize(name: str, language: Language) -> str | None:
    """外部名（修飾名・ヘッダー名）をカテゴリに分類する。最長一致の接頭辞を用いる。"""

    table = _PYTHON_PREFIXES if language == Language.PYTHON else _C_HEADER_PREFIXES
    best: tuple[int, str] | None = None
    for prefix, category in table.items():
        if language == Language.PYTHON:
            matches = name == prefix or name.startswith(prefix + ".")
        else:
            matches = name == prefix or (prefix.endswith("/") and name.startswith(prefix))
        if matches and (best is None or len(prefix) > best[0]):
            best = (len(prefix), category)
    return best[1] if best else None


_C_FUNCTION_PREFIXES: dict[str, str] = {
    **{p: "filesystem" for p in ("fopen", "fread", "fwrite", "fclose", "fseek", "open", "read", "write", "close", "unlink",
                                 "rename", "remove", "mkdir", "rmdir", "opendir", "readdir", "stat", "chmod", "fsync")},
    **{p: "logging" for p in ("printf", "fprintf", "puts", "fputs", "perror", "syslog", "putchar")},
    **{p: "network" for p in ("socket", "connect", "bind", "listen", "accept", "send", "recv", "sendto", "recvfrom",
                              "getaddrinfo", "curl_")},
    **{p: "process" for p in ("system", "popen", "fork", "execl", "execv", "execvp", "execve", "kill", "signal", "exit", "abort")},
    **{p: "concurrency" for p in ("pthread_", "sem_", "thrd_")},
    **{p: "config" for p in ("getenv", "setenv", "putenv")},
    **{p: "database" for p in ("sqlite3_",)},
    **{p: "crypto" for p in ("SSL_", "EVP_", "RAND_", "SHA256", "MD5")},
}


def categorize_c_function(name: str) -> str | None:
    """C標準/システムの関数名をカテゴリに分類する（完全一致、または `pthread_` 等の接頭辞）。"""

    if name in _C_FUNCTION_PREFIXES:
        return _C_FUNCTION_PREFIXES[name]
    for prefix, category in _C_FUNCTION_PREFIXES.items():
        if prefix.endswith("_") and name.startswith(prefix):
            return category
    return None


# Emacs Lispの関数名（Emacs本体・標準のライブラリ）→ (カテゴリ, 操作)。名前による分類であり、
# advice・再定義・同名の別の関数の可能性があるため、確定ではなく推定として扱う。
_ELISP_FUNCTIONS: dict[str, tuple[str, str]] = {
    **{n: ("filesystem", "read") for n in (
        "insert-file-contents", "insert-file-contents-literally", "file-exists-p", "file-readable-p", "file-directory-p", "file-regular-p",
        "directory-files", "directory-files-recursively", "directory-files-and-attributes", "file-attributes", "file-truename",
        "file-newer-than-file-p", "file-symlink-p", "find-file-noselect", "locate-file", "file-modes", "file-writable-p",
        "load-file", "load", "insert-directory", "file-name-all-completions")},
    **{n: ("filesystem", "write") for n in (
        "write-region", "write-file", "delete-file", "delete-directory", "rename-file", "copy-file", "copy-directory", "make-directory",
        "make-symbolic-link", "add-name-to-file", "set-file-modes", "set-file-times", "append-to-file", "make-temp-file",
        "save-buffer", "basic-save-buffer",  "dired-delete-file", "make-empty-file")},
    **{n: ("process", "effect") for n in (
        "call-process", "call-process-region", "call-process-shell-command", "process-file", "start-process", "start-file-process", "make-process",
        "shell-command", "shell-command-to-string", "async-shell-command", "shell-command-on-region", "process-lines", "process-send-string",
        "process-send-region", "delete-process", "kill-process", "signal-process", "interrupt-process", "kill-emacs", "kill-terminal", "suspend-emacs",
        "compile", "make-serial-process", "make-pipe-process")},
    **{n: ("network", "effect") for n in (
        "url-retrieve", "url-retrieve-synchronously", "url-copy-file", "url-insert-file-contents", "url-http", "open-network-stream",
        "make-network-process", "network-lookup-address-info", "browse-url", "browse-url-default-browser", "eww", "request", "plz", "websocket-open",
        "package-refresh-contents", "package-install", "send-mail", "smtpmail-send-it", "message-send-and-exit")},
    **{n: ("config", "read") for n in ("getenv", "getenv-internal", "locate-user-emacs-file", "system-name", "user-login-name", "user-full-name",
                                        "emacs-pid", "executable-find", )},
    **{n: ("config", "write") for n in ("setenv", "putenv")},
    **{n: ("logging", "output") for n in ("message", "princ", "print", "prin1", "terpri", "display-warning", "lwarn", "warn", "minibuffer-message",
                                           "display-message-or-buffer", "pp", "write-char", )},
    **{n: ("gui", "io") for n in (
        "read-string", "read-from-minibuffer", "completing-read", "completing-read-multiple", "y-or-n-p", "yes-or-no-p", "read-file-name",
        "read-directory-name", "read-number", "read-char", "read-key", "read-event", "read-buffer", "read-passwd", "read-regexp", "read-answer",
        "switch-to-buffer", "pop-to-buffer", "display-buffer", "set-frame-parameter", "make-frame", "delete-frame", "x-popup-menu",
        "set-face-attribute", "set-window-buffer", "select-window", "ding", "beep", "sit-for", "recenter", "redisplay")},
    **{n: ("concurrency", "effect") for n in (
        "run-with-timer", "run-with-idle-timer", "run-at-time", "cancel-timer", "make-thread", "thread-join", "make-mutex", "mutex-lock",
        "accept-process-output", "sleep-for", "make-condition-variable", "async-start", )},
    **{n: ("database", "effect") for n in ("sqlite-open", "sqlite-execute", "sqlite-select", "sqlite-close", "sqlite-transaction", "sqlite-commit",
                                            "sqlite-pragma", "emacsql", "emacsql-with-transaction")},
    **{n: ("persistence", "write") for n in ("customize-save-variable", "customize-set-variable", "custom-save-all", "savehist-save", "desktop-save",
                                              "recentf-save-list", "bookmark-save",  "write-abbrev-file", )},
    **{n: ("crypto", "pure") for n in ("secure-hash", "md5", "sha1", "buffer-hash", "gnutls-hash-mac", "gnutls-symmetric-encrypt", "base64-encode-string",
                                        "base64-decode-string", "epg-encrypt-string", "epg-decrypt-string")},
}
def classify_elisp_function(name: str) -> tuple[str, str] | None:
    """Emacs Lispの関数名から (カテゴリ, 操作)。分類できなければ None。"""

    if name in _ELISP_FUNCTIONS:
        return _ELISP_FUNCTIONS[name]
    return None


def external_name(reference: Reference) -> str | None:
    """外部参照の、import元まで解決した修飾名。組み込みはその名前。"""

    key = reference.target_key or ""
    if key.startswith("pyimport:"):
        return key[len("pyimport:"):]
    if key.startswith("pytyped:"):
        body = key[len("pytyped:"):]
        type_key, _, attribute = body.rpartition("|")
        for prefix in ("pyimport:", "py:"):
            if type_key.startswith(prefix):
                return f"{type_key[len(prefix):]}.{attribute}"
    if reference.resolution_status == ResolutionStatus.EXTERNAL and key == "":
        return reference.target_name  # 組み込み関数など（open, print）
    return None


class ExternalService:
    """外部システム・外部ライブラリとの接続（ネットワーク・DB・ファイル・プロセス等）を洗い出す。

    importと、名前解決済みの呼び出しから分類する。呼び出し元の変数の型が分からないメソッド
    呼び出し（`path.write_text(...)` 等）は、メソッド名からの推定として別に示す。
    """

    def report(self, index: ProjectIndex) -> ExternalReport:
        report = ExternalReport()
        for dependency in index.dependencies:
            if dependency.resolution_status != ResolutionStatus.EXTERNAL or dependency.is_candidate:
                continue
            source_file = index.files.get(dependency.source_file_id)
            if source_file is None or source_file.language == Language.ELISP:  # Emacs Lispの require は分類しない（CやPythonの名前表は当てはめない）
                continue
            name = dependency.target_name
            category = categorize(name, source_file.language) or (
                "other" if dependency.dependency_kind == DependencyKind.IMPORT and not _is_stdlib(name) else None
            )
            if category is None:
                continue
            owner = self._module_symbol(index, dependency.source_file_id)
            report.uses.append(
                ExternalUse(
                    category, name, "import", owner.symbol_id if owner else None,
                    owner.qualified_name if owner else source_file.relative_path, source_file.relative_path,
                    dependency.evidence_location.start_line,
                    "include" if dependency.dependency_kind == DependencyKind.INCLUDE else "import",
                    "confirmed",
                )
            )
        for reference in index.references:
            if reference.reference_kind not in (ReferenceKind.CALL, ReferenceKind.NAME_REF):
                continue
            source = index.symbols.get(reference.source_symbol_id)
            if source is None:
                continue
            language = index.files[source.file_id].language
            path = index.path_of(source.file_id)
            if language == Language.ELISP:  # 名前による分類（確定ではなく推定）。マクロとして書かれる呼び出し（with-temp-file など）は対象外
                if reference.resolution_status == ResolutionStatus.EXTERNAL and reference.reference_kind == ReferenceKind.CALL:
                    classified = classify_elisp_function(reference.target_name)
                    if classified and classified[1] != "pure":
                        report.uses.append(ExternalUse(classified[0], reference.target_name, classified[1], source.symbol_id, source.qualified_name, path, reference.source_location.start_line, "call", "inferred"))
                continue
            if reference.resolution_status == ResolutionStatus.EXTERNAL:
                callee = external_name(reference) if language == Language.PYTHON else reference.target_name
                category = (categorize(callee, language) if language == Language.PYTHON else categorize_c_function(callee)) if callee else None
                operation = classify_operation(callee) if callee else "call"
                if category and category not in ("other",) and operation != "pure":  # 純粋な計算・例外クラスは外部への操作ではない
                    report.uses.append(ExternalUse(category, callee or "", operation, source.symbol_id, source.qualified_name, path, reference.source_location.start_line, "call", "confirmed"))
            elif reference.resolution_status == ResolutionStatus.UNRESOLVED and reference.reference_kind == ReferenceKind.CALL:
                method = reference.target_name.rsplit(".", 1)[-1]
                if "." in reference.target_name and (method in _EFFECT_METHODS or method in _READ_METHODS):
                    operation = "write" if method in _EFFECT_METHODS else "read"
                    report.uses.append(
                        ExternalUse("filesystem", reference.target_name, operation, source.symbol_id, source.qualified_name, path, reference.source_location.start_line, "call", "inferred")
                    )
        report.uses.sort(key=lambda u: (u.category, u.library, u.path, u.line))
        return report

    def effects(
        self,
        index: ProjectIndex,
        report: ExternalReport,
        symbol: Symbol,
        depth: int = 3,
    ) -> EffectSummary:
        """関数の副作用の候補を、直接の外部呼び出しと、解決済みの呼び出しをたどった先で集める。

        読み取りだけの場合もあるため「副作用の可能性」として扱う。呼び出し先を特定できない
        呼び出しの先は追えない（件数を示す）。
        """

        uses_by_symbol: dict[str, list[ExternalUse]] = defaultdict(list)
        reads_by_symbol: dict[str, list[ExternalUse]] = defaultdict(list)
        for use in report.uses:
            if use.source_id and use.kind == "call" and use.operation in EFFECT_OPERATIONS:
                uses_by_symbol[use.source_id].append(use)
            if use.source_id and use.kind == "call" and use.operation == "read":
                reads_by_symbol[use.source_id].append(use)

        calls_from: dict[str, list[Reference]] = defaultdict(list)
        for reference in index.references:
            if reference.reference_kind == ReferenceKind.CALL:
                calls_from[reference.source_symbol_id].append(reference)

        summary = EffectSummary(
            symbol,
            direct=list(uses_by_symbol.get(symbol.symbol_id, [])),
            reads_direct=list(reads_by_symbol.get(symbol.symbol_id, [])),
        )
        seen = {symbol.symbol_id}
        frontier: list[tuple[str, list[str]]] = [(symbol.symbol_id, [symbol.qualified_name])]
        counted: set[str] = set()
        for _ in range(depth):
            next_frontier: list[tuple[str, list[str]]] = []
            for current, route in frontier:
                for reference in calls_from.get(current, []):
                    if reference.resolution_status in (ResolutionStatus.UNRESOLVED, ResolutionStatus.AMBIGUOUS):
                        if reference.reference_id not in counted:
                            counted.add(reference.reference_id)
                            summary.unresolved_calls += 1
                        continue
                    target_id = reference.target_symbol_id
                    if reference.resolution_status != ResolutionStatus.RESOLVED or not target_id or target_id in seen:
                        continue
                    seen.add(target_id)
                    target = index.symbols.get(target_id)
                    if target is None:
                        continue
                    path = [*route, target.qualified_name]
                    summary.reachable.extend((use, path) for use in uses_by_symbol.get(target_id, []))
                    summary.reads_reachable.extend((use, path) for use in reads_by_symbol.get(target_id, []))
                    next_frontier.append((target_id, path))
            frontier = next_frontier
        return summary

    @staticmethod
    def _module_symbol(index: ProjectIndex, file_id: str) -> Symbol | None:
        for symbol in index.symbols.values():
            if symbol.file_id == file_id and symbol.kind.value == "module":
                return symbol
        return next((s for s in index.symbols.values() if s.file_id == file_id), None)


def _is_stdlib(name: str) -> bool:
    import sys

    return name.split(".", 1)[0] in sys.stdlib_module_names

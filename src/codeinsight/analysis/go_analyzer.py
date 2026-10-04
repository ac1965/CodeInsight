"""Go言語のシンボル・呼び出し・継承（埋め込み）・importの抽出器。

Goの構文解析は、標準ライブラリの `go/parser` を使う補助プログラム（`go_helper/goparse.go`）に任せる。補助プログラムは、
CodeInsight 自身のソースで、標準ライブラリだけに依存し、**対象のコードをコンパイルも実行もしない**（構文木を読むだけ）。
初回に `go build` で、データディレクトリ（`~/.codeinsight/tools/`）の下へ作る（ネットワークは使わない）。Goのツールチェーン（`go`）が
無い場合は、Goのファイルの解析を、その旨のエラーとして記録する（他の言語の解析には影響しない）。

型情報（`go/types`）は使わないため、解決は名前と、同じ関数の中で宣言から分かる変数の型に基づく。

* 同じパッケージの関数・メソッド・型は、パッケージ（ディレクトリ）の import パスと名前で解決する。
* `pkg.Func()` は、import の別名から import パスを引き、プロジェクト内のパッケージなら解決する。それ以外は外部（標準・外部ライブラリ）。
* `x.Method()` は、`x` の型が同じ関数の中の宣言（レシーバー・引数・`var x T`・`x := T{}`）から分かれば、その型のメソッドに解決する。
  分からなければ、同名のメソッドが1つだけのときに「推定」、複数なら曖昧、無ければ外部とする。インターフェース経由の呼び出しは、実装が複数ありうるため推定。
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path, PurePosixPath

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
from codeinsight.infrastructure.config import default_data_dir

HELPER_SOURCE = Path(__file__).resolve().parent / "go_helper" / "goparse.go"
KEY_GO = "go:"  # go:<importパス>.<名前> / go:<importパス>.<型>.<メソッド>
KEY_GO_METHOD = "gomethod:"  # gomethod:<メソッド名>（レシーバーの型を確定できない）
KEY_GO_IMPORT = "goimport:"  # goimport:<importパス>（プロジェクト内のパッケージ）
KEY_GO_CHAIN = "gochain:"  # gochain:<起点の型>|<フィールドの連鎖>|<メソッド>（a.b.M() の a の型が分かる場合）
TYPE_PREFIX = "型: "  # 構造体のフィールドのシンボルの要約。型の修飾名を持つ（解決でフィールドをたどるため）
BUILTIN_FUNCTIONS = frozenset(
    "append cap clear close complex copy delete imag len make max min new panic print println real recover".split()
)
BUILTIN_TYPES = frozenset(
    "any bool byte comparable complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune string uint uint8 uint16 uint32 uint64 uintptr".split()
)
_MODULE_LINE = re.compile(r"^\s*module\s+(\S+)", re.MULTILINE)
_REQUEST_TIMEOUT = 30.0


class GoToolchainError(Exception):
    pass


class _Helper:
    """補助プログラムの常駐プロセス（1ファイルごとに起動し直さない）。要求は、1件ずつ処理する。"""

    def __init__(self) -> None:
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._binary: Path | None = None
        self._unavailable: str | None = None
        atexit.register(self.close)

    def _build(self) -> Path:
        go = shutil.which("go")
        if go is None:
            raise GoToolchainError("Goのツールチェーン（go）が見つからないため、Goのファイルを解析できません（go をインストールしてください）")
        digest = hashlib.sha256(HELPER_SOURCE.read_bytes()).hexdigest()[:12]
        directory = default_data_dir() / "tools"
        # OS・CPUごとに別のファイルにする（ホストとコンテナで ~/.codeinsight を共有しても、互いの実行ファイルを使わない）
        platform_tag = f"{platform.system()}-{platform.machine()}".lower()
        binary = directory / f"goparse-{digest}-{platform_tag}"
        if binary.is_file():
            return binary
        directory.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="goparse-build-"))
        try:
            shutil.copy(HELPER_SOURCE, work / "main.go")
            (work / "go.mod").write_text("module codeinsightgoparse\n\ngo 1.21\n", encoding="utf-8")
            # ネットワークと、ツールチェーンの自動取得は使わない（標準ライブラリだけで作る）
            env = {**os.environ, "GOPROXY": "off", "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=mod", "CGO_ENABLED": "0"}
            completed = subprocess.run([go, "build", "-o", str(binary), "."], cwd=work, env=env, capture_output=True, text=True, timeout=180, check=False)
            if completed.returncode != 0:
                raise GoToolchainError(f"Goの補助プログラムを作れませんでした: {completed.stderr.strip()[:300]}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GoToolchainError(f"Goの補助プログラムを作れませんでした: {exc}") from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return binary

    def _start(self) -> subprocess.Popen[bytes]:
        if self._unavailable is not None:
            raise GoToolchainError(self._unavailable)
        if self._binary is None:
            try:
                self._binary = self._build()
            except GoToolchainError as exc:
                self._unavailable = str(exc)
                raise
        return subprocess.Popen([str(self._binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def request(self, name: str, source: bytes) -> dict:
        line = json.dumps({"name": name, "source": base64.b64encode(source).decode("ascii")}).encode("utf-8") + b"\n"
        with self._lock:
            for attempt in (1, 2):  # 補助プログラムが落ちていたら、1度だけ起動し直す
                if self._process is None or self._process.poll() is not None:
                    self._process = self._start()
                process = self._process
                assert process.stdin is not None and process.stdout is not None
                try:
                    process.stdin.write(line)
                    process.stdin.flush()
                    answer = process.stdout.readline()
                except (BrokenPipeError, OSError):
                    answer = b""
                if answer:
                    return json.loads(answer)
                self._process = None
                if attempt == 2:
                    break
        raise GoToolchainError("Goの補助プログラムから応答がありませんでした")

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            try:
                assert process.stdin is not None
                process.stdin.close()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()


_HELPER = _Helper()


def _find_module(unit: SourceUnit) -> tuple[str, PurePosixPath]:
    """ファイルが属する Go モジュールの (モジュールパス, モジュールのルートからのプロジェクト内の相対ディレクトリ)。go.mod が無ければ ("", ".")。"""

    root = unit.absolute_path
    for _ in PurePosixPath(unit.relative_path).parts:
        root = root.parent
    directory = unit.absolute_path.parent
    while True:
        go_mod = directory / "go.mod"
        if go_mod.is_file():
            try:
                match = _MODULE_LINE.search(go_mod.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                match = None
            relative = PurePosixPath(directory.relative_to(root).as_posix()) if directory != root else PurePosixPath(".")
            return (match.group(1) if match else ""), relative
        if directory == root or directory.parent == directory:
            return "", PurePosixPath(".")
        directory = directory.parent


def import_path_of(unit: SourceUnit) -> tuple[str, str]:
    """(このファイルのパッケージの import パス, モジュールパス)。"""

    module, module_dir = _find_module(unit)
    package_dir = PurePosixPath(unit.relative_path).parent
    try:
        inside = package_dir.relative_to(module_dir)
    except ValueError:
        inside = package_dir
    suffix = "" if str(inside) == "." else inside.as_posix()
    if module:
        return (module + ("/" + suffix if suffix else "")), module
    return (suffix or "."), ""


class GoAnalyzer:
    language = Language.GO

    def analyze_file(self, unit: SourceUnit) -> FileAnalysis:
        result = FileAnalysis()
        try:
            answer = _HELPER.request(unit.relative_path, unit.content)
        except GoToolchainError as exc:
            result.errors.append(str(exc))
            return result
        if answer.get("errors"):
            result.errors += [f"構文解析に失敗しました: {message}" for message in answer["errors"][:5]]
            return result
        package_path, module = import_path_of(unit)
        ids = IdAllocator(unit.file_id)
        file_name = PurePosixPath(unit.relative_path).name
        module_symbol = build_symbol(
            ids, unit.file_id, name=PurePosixPath(file_name).stem, qualified_name=f"{package_path}/{file_name}", kind=SymbolKind.MODULE,
            start_line=1, end_line=max((d["end"] for d in answer.get("decls", [])), default=1), summary=f"package {answer.get('package', '')}",
        )
        result.symbols.append(module_symbol)
        aliases: dict[str, str] = {}
        for imported in answer.get("imports", []):
            self._import(unit, ids, result, imported, module, aliases)
        types_here: dict[str, Symbol] = {}
        pending: list[tuple[Symbol, dict]] = []
        for decl in answer.get("decls", []):
            symbol = self._declaration(unit, ids, result, decl, package_path, module_symbol, types_here)
            if symbol is not None:
                pending.append((symbol, decl))
                self._members(unit, ids, result, symbol, decl, package_path, module, aliases)
        for symbol, decl in pending:
            for call in decl.get("calls") or []:
                self._call(unit, ids, result, symbol, call, package_path, module, aliases)
            for embedded in decl.get("embeds") or []:
                self._embedding(unit, ids, result, symbol, embedded, decl["start"], package_path, module, aliases)
        return result

    # --- import ---

    @staticmethod
    def _is_internal(path: str, module: str) -> bool:
        return bool(module) and (path == module or path.startswith(module + "/"))

    def _import(self, unit: SourceUnit, ids: IdAllocator, result: FileAnalysis, imported: dict, module: str, aliases: dict[str, str]) -> None:
        path = imported["path"]
        alias = imported.get("alias") or path.rsplit("/", 1)[-1]
        if alias not in ("_", "."):
            aliases[alias] = path
        internal = self._is_internal(path, module)
        result.dependencies.append(
            Dependency(
                dependency_id=ids.dependency_id("import", path, imported["line"]), source_file_id=unit.file_id, target_name=path,
                target_key=KEY_GO_IMPORT + path if internal else None, dependency_kind=DependencyKind.IMPORT,
                evidence_location=SourceLocation(unit.file_id, imported["line"], imported["line"]),
                resolution_status=ResolutionStatus.UNRESOLVED if internal else ResolutionStatus.EXTERNAL,
                note="" if internal else "プロジェクト外（標準ライブラリ・外部のモジュール）",
            )
        )

    # --- 宣言 ---

    def _declaration(self, unit, ids, result, decl: dict, package_path: str, module_symbol: Symbol, types_here: dict[str, Symbol]) -> Symbol | None:
        kind_name, name = decl["kind"], decl["name"]
        if kind_name == "func":
            kind, qualified, parent = (SymbolKind.FUNCTION, f"{package_path}.{name}", module_symbol)
        elif kind_name == "method":
            owner = decl.get("recv") or "?"
            kind, qualified, parent = SymbolKind.METHOD, f"{package_path}.{owner}.{name}", types_here.get(owner, module_symbol)
        elif kind_name in ("struct", "interface", "type"):
            kind = {"struct": SymbolKind.STRUCT, "interface": SymbolKind.INTERFACE, "type": SymbolKind.TYPEDEF}[kind_name]
            qualified, parent = f"{package_path}.{name}", module_symbol
        else:
            kind, qualified, parent = SymbolKind.GLOBAL_VARIABLE, f"{package_path}.{name}", module_symbol
        symbol = build_symbol(
            ids, unit.file_id, name=name, qualified_name=qualified, kind=kind, start_line=decl["start"], end_line=decl["end"], parent=parent,
            summary=decl.get("doc", ""), base_classes=tuple(decl.get("embeds") or ()),
        )
        result.symbols.append(symbol)
        if kind_name in ("struct", "interface", "type"):
            types_here[name] = symbol
        return symbol if kind_name in ("func", "method", "struct", "interface") else None

    def _qualify_type(self, name: str, package_path: str, module: str, aliases: dict[str, str]) -> str:
        """型名（Name / pkg.Name）の修飾名。組み込み型は `builtin:`、プロジェクト外の型は `ext:` を付ける。"""

        if not name:
            return ""
        owner_path, _, short = name.rpartition(".")
        if owner_path:
            path = aliases.get(owner_path)
            if path is None:
                return ""
            return f"{path}.{short}" if self._is_internal(path, module) else f"ext:{path}.{short}"
        if short in BUILTIN_TYPES:
            return f"builtin:{short}"
        return f"{package_path}.{short}"

    def _members(self, unit, ids, result, owner: Symbol, decl: dict, package_path: str, module: str, aliases: dict[str, str]) -> None:
        """構造体のフィールド（型を持つ）と、インターフェースのメソッド（宣言）をシンボルにする。"""

        for field in decl.get("fields") or []:
            qualified_type = self._qualify_type(field.get("type", ""), package_path, module, aliases)
            result.symbols.append(
                build_symbol(
                    ids, unit.file_id, name=field["name"], qualified_name=f"{owner.qualified_name}.{field['name']}", kind=SymbolKind.CLASS_VARIABLE,
                    start_line=field["line"], end_line=field["line"], parent=owner, summary=TYPE_PREFIX + qualified_type if qualified_type else "",
                )
            )
        for method in decl.get("imethods") or []:
            result.symbols.append(
                build_symbol(
                    ids, unit.file_id, name=method["name"], qualified_name=f"{owner.qualified_name}.{method['name']}", kind=SymbolKind.FUNCTION_DECLARATION,
                    start_line=method["start"], end_line=method["end"], parent=owner, summary="インターフェースのメソッド（宣言）",
                )
            )

    # --- 参照 ---

    def _reference(self, unit, ids, result, source: Symbol, kind: ReferenceKind, name: str, key: str | None, line: int, end: int,
                   status: ResolutionStatus = ResolutionStatus.UNRESOLVED, note: str = "") -> None:
        result.references.append(
            Reference(
                reference_id=ids.reference_id(source.symbol_id, kind.value, key or name, line), source_symbol_id=source.symbol_id, target_name=name,
                target_key=key, reference_kind=kind, source_location=SourceLocation(unit.file_id, line, end), resolution_status=status, note=note,
            )
        )

    def _call(self, unit, ids, result, source: Symbol, call: dict, package_path: str, module: str, aliases: dict[str, str]) -> None:
        name, x, recv_type = call["name"], call.get("x", ""), call.get("recv_type", "")
        line, end = call["line"], call["end_line"]

        def add(target: str, key: str | None, status=ResolutionStatus.UNRESOLVED, note: str = "") -> None:
            self._reference(unit, ids, result, source, ReferenceKind.CALL, target, key, line, end, status, note)

        if x and x in aliases and not recv_type:  # pkg.Func()
            path = aliases[x]
            if self._is_internal(path, module):
                add(f"{x}.{name}", f"{KEY_GO}{path}.{name}")
            else:
                add(f"{x}.{name}", None, ResolutionStatus.EXTERNAL, "プロジェクト外（標準ライブラリ・外部のモジュール）")
            return
        chain = call.get("chain", "")
        if chain and recv_type:  # a.b.Method(): a の型が分かれば、フィールドをたどって b の型を求める（解決時に）
            root = self._qualify_type(recv_type, package_path, module, aliases)
            if root.startswith("ext:") or root.startswith("builtin:"):
                add(name, None, ResolutionStatus.EXTERNAL, "プロジェクト外の型を起点にした呼び出し")
            elif root:
                add(name, f"{KEY_GO_CHAIN}{root}|{chain.split('.', 1)[1]}|{name}")
            else:
                add(name, f"{KEY_GO_METHOD}{name}")
            return
        if call.get("selector") and not x:  # f().Method(): 左辺の型を、この解析では確定できない
            add(name, f"{KEY_GO_METHOD}{name}")
            return
        if x:  # x.Method()
            if recv_type:
                owner_path, _, owner = recv_type.rpartition(".")
                if owner_path in aliases:
                    path = aliases[owner_path]
                    if not self._is_internal(path, module):
                        add(f"{x}.{name}", None, ResolutionStatus.EXTERNAL, "プロジェクト外の型のメソッド")
                        return
                    add(f"{x}.{name}", f"{KEY_GO}{path}.{owner}.{name}")
                elif owner_path:
                    add(f"{x}.{name}", f"{KEY_GO_METHOD}{name}")
                else:
                    add(f"{x}.{name}", f"{KEY_GO}{package_path}.{owner}.{name}")
            else:
                add(f"{x}.{name}", f"{KEY_GO_METHOD}{name}")
            return
        if name in BUILTIN_FUNCTIONS or name in BUILTIN_TYPES:
            add(name, None, ResolutionStatus.EXTERNAL, "組み込み関数・型")
            return
        add(name, f"{KEY_GO}{package_path}.{name}")

    def _embedding(self, unit, ids, result, source: Symbol, embedded: str, line: int, package_path: str, module: str, aliases: dict[str, str]) -> None:
        owner_path, _, owner = embedded.rpartition(".")
        if owner_path:
            path = aliases.get(owner_path)
            if path is not None and self._is_internal(path, module):
                self._reference(unit, ids, result, source, ReferenceKind.INHERITANCE, embedded, f"{KEY_GO}{path}.{owner}", line, line)
            else:
                self._reference(unit, ids, result, source, ReferenceKind.INHERITANCE, embedded, None, line, line, ResolutionStatus.EXTERNAL, "プロジェクト外の型")
        elif owner in BUILTIN_TYPES:
            return
        else:
            self._reference(unit, ids, result, source, ReferenceKind.INHERITANCE, embedded, f"{KEY_GO}{package_path}.{owner}", line, line)

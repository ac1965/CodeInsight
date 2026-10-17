"""Goの補助プログラム `cifast` の構築と呼び出し。

`cifast` は、標準ライブラリだけに依存する、本ソフトウェア自身のソース（`native/cifast/main.go`）で、対象のコードを読み取る以外のことをしない。
初回に `go build` で、データディレクトリ（`~/.codeinsight/tools/`）の下へ作る（ネットワークとツールチェーンの自動取得は使わない）。
Goのツールチェーンが無い・作れない・実行に失敗した場合は、結果を `None` で返し、呼び出し側が Python の実装へ戻る（エラーにはしない）。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from codeinsight.infrastructure.config import default_data_dir

HELPER_SOURCE = Path(__file__).resolve().parent / "cifast" / "main.go"
_TIMEOUT = 300.0


class NativeHelper:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._binary: Path | None = None
        self._unavailable: str | None = None

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable

    def _enabled(self) -> bool:
        return os.environ.get("CODEINSIGHT_NO_NATIVE", "") not in ("1", "true", "yes")

    def _build(self) -> Path:
        go = shutil.which("go")
        if go is None:
            raise OSError("Goのツールチェーン（go）が見つかりません")
        digest = hashlib.sha256(HELPER_SOURCE.read_bytes()).hexdigest()[:12]
        platform_tag = f"{platform.system()}-{platform.machine()}".lower()
        directory = default_data_dir() / "tools"
        binary = directory / f"cifast-{digest}-{platform_tag}"
        if binary.is_file():
            return binary
        directory.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix="cifast-build-"))
        try:
            shutil.copy(HELPER_SOURCE, work / "main.go")
            (work / "go.mod").write_text("module codeinsightcifast\n\ngo 1.21\n", encoding="utf-8")
            env = {**os.environ, "GOPROXY": "off", "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=mod", "CGO_ENABLED": "0"}
            completed = subprocess.run([go, "build", "-o", str(binary), "."], cwd=work, env=env, capture_output=True, text=True, timeout=180, check=False)
            if completed.returncode != 0:
                raise OSError(f"補助プログラムを作れませんでした: {completed.stderr.strip()[:300]}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return binary

    def _ensure(self) -> Path | None:
        if not self._enabled():
            return None
        with self._lock:
            if self._binary is None and self._unavailable is None:
                try:
                    self._binary = self._build()
                except (OSError, subprocess.SubprocessError) as exc:
                    self._unavailable = str(exc)
            return self._binary

    def call(self, request: dict) -> dict | None:
        """補助プログラムに1件の要求を渡す。使えない・失敗した場合は None（呼び出し側が Python で処理する）。"""

        binary = self._ensure()
        if binary is None:
            return None
        try:
            completed = subprocess.run([str(binary)], input=json.dumps(request).encode("utf-8"), capture_output=True, timeout=_TIMEOUT, check=False)
            answer = json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        if completed.returncode != 0 or not isinstance(answer, dict) or "error" in answer:
            return None
        return answer

    # ---- 操作 ----

    def scan(self, root: Path, gitignore_lines: list[str] | None, exclude_dirs: set[str], exclude_files: set[str]) -> list[str] | None:
        """root 配下の解析対象ファイルの相対パス（走査順）。.gitignore の書式が範囲外なら None。"""

        answer = self.call({
            "op": "scan", "root": str(root), "use_gitignore": gitignore_lines is not None,
            "gitignore_lines": gitignore_lines or [], "exclude_dirs": sorted(exclude_dirs), "exclude_files": sorted(exclude_files),
        })
        if answer is None or answer.get("unsupported"):
            return None
        return list(answer.get("files") or [])

    def hash_files(self, paths: list[str]) -> dict[str, str | None] | None:
        """ファイルの SHA-256（読めないものは None）。"""

        answer = self.call({"op": "hash", "paths": paths})
        return None if answer is None else dict(answer["hashes"])

    def paths(self, edges: dict[str, list[tuple[int, str]]], source: str, target: str, max_depth: int, limit: int) -> list[list[int]] | None:
        answer = self.call({
            "op": "paths", "source": source, "target": target, "max_depth": max_depth, "limit": limit,
            "edges": {k: [{"id": i, "target": t} for i, t in v] for k, v in edges.items()},
        })
        return None if answer is None else [list(p) for p in answer["paths"]]


_HELPER = NativeHelper()


def native_helper() -> NativeHelper:
    return _HELPER

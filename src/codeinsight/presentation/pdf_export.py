"""HTML を PDF に変換する（ヘッドレスのブラウザを使う。新しい依存は増やさない）。

変換するのは、CodeInsight が生成した自己完結のHTML（スクリプトなし・外部通信なし）だけで、対象のプログラムではない。
ブラウザが見つからない・変換に失敗した場合は、例外で理由を示す（PDFが作れなかったことを隠さない）。
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ENV_BROWSER = "CODEINSIGHT_BROWSER"
_MAC_APPS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
)
_PATH_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome", "microsoft-edge", "msedge")


class PdfExportError(Exception):
    """PDFを作れなかった（ブラウザが無い・変換の失敗・出力が無い）。"""


def find_browser() -> str | None:
    """PDF変換に使うブラウザ。環境変数 CODEINSIGHT_BROWSER、macOSのアプリ、PATH の順に探す。"""

    configured = os.environ.get(ENV_BROWSER)
    if configured:
        return configured if (Path(configured).is_file() or shutil.which(configured)) else None
    if sys.platform == "darwin":
        for candidate in _MAC_APPS:
            if Path(candidate).is_file():
                return candidate
    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


def html_to_pdf(html_path: Path, pdf_path: Path, browser: str | None = None, timeout: float = 180.0) -> Path:
    """HTMLファイルをPDFにする。成功したらPDFのパスを返す。"""

    browser = browser or find_browser()
    if browser is None:
        raise PdfExportError(
            "PDFに変換するブラウザ（Chrome・Chromium・Edge）が見つかりません。"
            f"インストールするか、環境変数 {ENV_BROWSER} で実行ファイルを指定してください。HTMLは作成済みです（{html_path}）。ブラウザで開いて印刷（PDFとして保存）することもできます。"
        )
    pdf_path = pdf_path.resolve()
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="codeinsight-pdf-") as profile:  # 利用者のブラウザのプロファイルを使わない
        command = [
            browser, "--headless", "--disable-gpu", "--disable-extensions", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={profile}", "--no-pdf-header-footer", f"--print-to-pdf={pdf_path}", html_path.resolve().as_uri(),
        ]
        try:
            process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True
            )  # 補助プロセスごと止められるよう、独立したプロセスグループにする
        except OSError as exc:
            raise PdfExportError(f"ブラウザを起動できません: {browser}: {exc}") from exc
        try:
            finished = _wait_for_pdf(process, pdf_path, timeout)
        finally:
            _stop(process)
        if not finished:
            error = ""
            try:
                error = (process.stderr.read() if process.stderr else "").strip().splitlines()[-1][:200]
            except (OSError, IndexError, ValueError):
                pass
            raise PdfExportError(
                f"PDFが作られませんでした（{'時間切れ' if process.returncode is None or process.returncode < 0 else '終了コード ' + str(process.returncode)}）。{error}"
            )
    return pdf_path


def _complete(path: Path) -> bool:
    """PDFとして書き終わっているか（先頭が %PDF で、末尾が %%EOF）。"""

    try:
        with path.open("rb") as handle:
            head = handle.read(5)
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 64))
            return head == b"%PDF-" and b"%%EOF" in handle.read()
    except OSError:
        return False


def _wait_for_pdf(process: subprocess.Popen, pdf_path: Path, timeout: float, settle: float = 1.0) -> bool:
    """PDFが書き終わるまで待つ。ブラウザ（特にChromeの安定版）は、PDFを書いた後も、補助プロセスが残って終了しないことがあるため、
    プロセスの終了ではなく、PDFの完成（末尾が %%EOF でサイズが安定）を待つ。"""

    deadline = time.monotonic() + timeout
    last_size, stable_since = -1, 0.0
    while time.monotonic() < deadline:
        if pdf_path.is_file() and _complete(pdf_path):
            size = pdf_path.stat().st_size
            if size == last_size:
                if time.monotonic() - stable_since >= settle:
                    return True
            else:
                last_size, stable_since = size, time.monotonic()
        elif process.poll() is not None:
            return False  # PDFを作らずに終了した
        time.sleep(0.2)
    return False


def _stop(process: subprocess.Popen) -> None:
    """ブラウザをプロセスグループごと止める。"""

    if process.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue

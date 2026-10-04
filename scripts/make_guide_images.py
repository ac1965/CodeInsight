"""ビューアーの操作説明（src/codeinsight/web/guide/VIEWER.md）の画像を、実際のビューアーから生成する。

同梱の小さなサンプル（tests/fixtures/layered）を解析し、ビューアーを起動して、ヘッドレスのChrome系ブラウザで撮影する。
画面を変更したあとは、これを実行して、画像を撮り直す（`uv run python scripts/make_guide_images.py`）。
番号の印は、ビューアーの `#annotate=` の指定で付けるため、手作業の加工は要らない。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

from codeinsight.application import NavigationService
from codeinsight.cli import main
from codeinsight.infrastructure import AnalysisRepository
from codeinsight.presentation.pdf_export import find_browser
from codeinsight.web.api import ViewerApi
from codeinsight.web.server import ViewerServer

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "tests" / "fixtures" / "layered"
OUT_DIR = ROOT / "src" / "codeinsight" / "web" / "guide" / "images"
ROOT_SYMBOL = "app.service.orders.place_order"
WIDTH, HEIGHT = 1280, 860

# 画像の名前 -> (ウィンドウの高さ, URLの # 以降)
SHOTS: dict[str, tuple[int, str]] = {
    "01-controls.png": (860, f"kind=call&direction=both&depth=2&root={quote(ROOT_SYMBOL)}&select=place_order&annotate=controls"),
    "02-depends-on.png": (860, f"kind=call&direction=both&depth=2&root={quote(ROOT_SYMBOL)}"),
    "03-flow.png": (860, f"kind=flow&root={quote(ROOT_SYMBOL)}&select=return"),
    "04-source.png": (860, f"kind=call&direction=both&depth=1&root={quote(ROOT_SYMBOL)}&select=save&annotate=panel"),
    "05-extract.png": (860, f"kind=call&direction=both&depth=1&root={quote(ROOT_SYMBOL)}&select=place_order&tab=extract"),
    "06-reading.png": (860, "view=reading&doc=README.md&annotate=docs"),
    "07-reading-card.png": (860, "view=reading&doc=functions%2Fapp.service.orders.place_order_L7.txt&annotate=docs"),
}


def _screenshot(browser: str, url: str, target: Path, height: int, timeout: float = 90.0) -> None:
    target.unlink(missing_ok=True)
    profile = tempfile.mkdtemp(prefix="ci-guide-")
    process = subprocess.Popen(
        [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", f"--user-data-dir={profile}",
         f"--window-size={WIDTH},{height}", "--virtual-time-budget=8000", f"--screenshot={target}", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    last = -1
    stable = 0
    try:
        while time.monotonic() < deadline:
            size = target.stat().st_size if target.exists() else 0
            stable = stable + 1 if size and size == last else 0
            last = size
            if stable >= 3:  # Chromeは、画像を書いたあとも終了しないことがあるため、書き終わりを見て止める
                return
            if process.poll() is not None and size:
                return
            time.sleep(0.5)
        raise RuntimeError(f"撮影がタイムアウトしました: {url}")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        shutil.rmtree(profile, ignore_errors=True)


def main_script() -> int:
    browser = find_browser()
    if browser is None:
        print("Chrome・Chromium・Edge が見つかりません。")
        return 2
    work = Path(tempfile.mkdtemp(prefix="ci-guide-work-"))
    data = work / "data"
    out = work / "reading"
    env = {**os.environ, "CODEINSIGHT_DATA_DIR": str(data), "SERVE": "0"}
    os.environ["CODEINSIGHT_DATA_DIR"] = str(data)
    # 利用者の環境のパスが、画像に入らないよう、サンプルを一時ディレクトリへ複製して解析する
    sample = work / "layered"
    shutil.copytree(SAMPLE, sample)
    subprocess.run(["make", "--no-print-directory", "reading", f"TARGET={sample}", f"OUT={out}", "TOP=8", "PDF=0"], cwd=ROOT, env=env, check=True, capture_output=True)
    db = data / "codeinsight.db"
    if main(["status", "--db", str(db), "--project", str(sample)]) != 0:
        return 1
    repository = AnalysisRepository(db, check_same_thread=False)
    project = next(p for p in repository.list_projects() if p.root_path == sample.resolve())
    server = ViewerServer("127.0.0.1", 0, ViewerApi(repository, project, out))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        for name, (height, fragment) in SHOTS.items():
            _screenshot(browser, f"{server.url}#{fragment}", OUT_DIR / name, height)
            print(f"  {name}")
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(work, ignore_errors=True)
    _ = NavigationService  # 型の読み込みを確実にする（解析結果の読み取りに使う）
    return 0


if __name__ == "__main__":
    raise SystemExit(main_script())

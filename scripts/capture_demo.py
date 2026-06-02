#!/usr/bin/env python3
"""README 冒頭のデモGIF (docs/assets/demo.gif) を再生成する。

  uv run --with playwright playwright install chromium   # 初回のみ（Chromium取得）
  uv run --with playwright python scripts/capture_demo.py           # GIF 生成（ffmpeg 必須）
  uv run --with playwright python scripts/capture_demo.py --still   # 静止画 demo_still.png のみ

文言・色・例(ROC曲線など)を変えたいときは docs/assets/demo.html を編集して再実行する。
demo.gif は「実機スクショではなくイメージ図」。実トークン・実 userId は一切含まない。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit(
        "playwright が見つかりません。次で実行してください:\n"
        "  uv run --with playwright playwright install chromium\n"
        "  uv run --with playwright python scripts/capture_demo.py"
    )

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "docs" / "assets"
HTML = (ASSETS / "demo.html").as_uri()
VID = ASSETS / "_vid"
W, H, LOOP_SEC = 1000, 520, 6.1


def main() -> None:
    still = "--still" in sys.argv
    with sync_playwright() as p:
        browser = p.chromium.launch()
        if still:
            page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=2)
            page.goto(HTML)
            time.sleep(4.4)  # 全アニメ完了後
            out = ASSETS / "demo_still.png"
            page.screenshot(path=str(out))
            browser.close()
            print(f"✓ {out}")
            return

        if VID.exists():
            shutil.rmtree(VID)
        ctx = browser.new_context(
            viewport={"width": W, "height": H},
            device_scale_factor=2,
            record_video_dir=str(VID),
            record_video_size={"width": W, "height": H},
        )
        page = ctx.new_page()
        page.goto(HTML)
        time.sleep(LOOP_SEC)  # 1ループ録画
        ctx.close()
        browser.close()

    webm = max(VID.glob("*.webm"), key=lambda f: f.stat().st_mtime)
    gif = ASSETS / "demo.gif"
    if not shutil.which("ffmpeg"):
        print(f"録画: {webm}\nffmpeg が無いため GIF 変換はスキップ（brew install ffmpeg）。")
        return
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(webm), "-vf",
            "fps=18,scale=820:-1:flags=lanczos,split[s0][s1];"
            "[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer",
            "-loop", "0", str(gif), "-y",
        ],
        check=True,
    )
    shutil.rmtree(VID, ignore_errors=True)
    print(f"✓ {gif}  ({gif.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()

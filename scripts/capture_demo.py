#!/usr/bin/env python3
"""README/SETUP_LINE 用の説明 GIF (docs/assets/*.gif) を再生成する。

  uv run --with playwright playwright install chromium   # 初回のみ（Chromium取得）
  uv run --with playwright python scripts/capture_demo.py                       # demo.gif
  uv run --with playwright python scripts/capture_demo.py --target setup-flow   # setup-flow.gif
  uv run --with playwright python scripts/capture_demo.py --still               # 静止画のみ

文言・色・例を変えたいときは docs/assets/<target>.html を編集して再実行する。
どちらの GIF も「実機スクショではなくイメージ図」。実トークン・実 userId は一切含まない。
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

# target ごとのサイズ・再生時間
TARGETS = {
    "demo":       {"w": 1000, "h": 520, "loop_sec": 6.1, "still_at": 4.4, "fps": 18, "colors": 128},
    "setup-flow": {"w":  900, "h": 440, "loop_sec": 9.9, "still_at": 1.2, "fps": 12, "colors": 96},
}


def parse_args() -> tuple[str, bool]:
    args = sys.argv[1:]
    target = "demo"
    if "--target" in args:
        i = args.index("--target")
        target = args[i + 1]
        if target not in TARGETS:
            sys.exit(f"unknown --target: {target}. 選択肢: {', '.join(TARGETS)}")
    return target, "--still" in args


def main() -> None:
    target, still_only = parse_args()
    cfg = TARGETS[target]
    html = (ASSETS / f"{target}.html").as_uri()
    gif = ASSETS / f"{target}.gif"
    still = ASSETS / f"{target}_still.png"
    vid_dir = ASSETS / f"_vid_{target}"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        if still_only:
            page = browser.new_page(viewport={"width": cfg["w"], "height": cfg["h"]}, device_scale_factor=2)
            page.goto(html)
            time.sleep(cfg["still_at"])
            page.screenshot(path=str(still))
            browser.close()
            print(f"✓ {still}")
            return

        if vid_dir.exists():
            shutil.rmtree(vid_dir)
        ctx = browser.new_context(
            viewport={"width": cfg["w"], "height": cfg["h"]},
            device_scale_factor=2,
            record_video_dir=str(vid_dir),
            record_video_size={"width": cfg["w"], "height": cfg["h"]},
        )
        page = ctx.new_page()
        page.goto(html)
        time.sleep(cfg["loop_sec"])
        ctx.close()
        browser.close()

    webm = max(vid_dir.glob("*.webm"), key=lambda f: f.stat().st_mtime)
    if not shutil.which("ffmpeg"):
        print(f"録画: {webm}\nffmpeg が無いため GIF 変換はスキップ（brew install ffmpeg）。")
        return
    vf = (
        f"fps={cfg['fps']},scale=820:-1:flags=lanczos,split[s0][s1];"
        f"[s0]palettegen=max_colors={cfg['colors']}[p];[s1][p]paletteuse=dither=bayer"
    )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(webm), "-vf", vf, "-loop", "0", str(gif), "-y"],
        check=True,
    )
    shutil.rmtree(vid_dir, ignore_errors=True)
    print(f"✓ {gif}  ({gif.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()

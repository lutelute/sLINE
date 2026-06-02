#!/usr/bin/env python3
"""LINE 登録ガイド用のスクリーンショットを撮る（Playwright・対話式）。

使い方（playwright は本体依存に入れていないので --with で一時利用）:
    uv run --with playwright playwright install chromium   # 初回のみ（Chromium取得）
    uv run --with playwright python scripts/capture_guide.py

⚠️ 必ず「ダミー / 新規のテスト用プロバイダー・チャネル」で使うこと。
   実トークン・実 userId・本番アカウント名が写ると、公開時に漏洩する。
   撮影後、docs/images/ を必ず目視確認し、個人情報が写っていないか確認すること。

挙動:
  - ブラウザ(可視)を起動して LINE Developers Console を開く。
  - 各ステップで「目的の画面を開いて Enter」を促す（s + Enter でスキップ）。
  - 撮影前にヘッダのアカウント名/メール等を best-effort でブラー（保証はしない）。
  - docs/images/<slug>.png に保存。
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit(
        "playwright が見つかりません。次で実行してください:\n"
        "  uv run --with playwright playwright install chromium\n"
        "  uv run --with playwright python scripts/capture_guide.py"
    )

OUT = Path(__file__).resolve().parent.parent / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

# (ファイル名, 案内文) — docs/SETUP_LINE.md の各手順に対応
STEPS = [
    ("01-oa-create", "LINE Official Account Manager でアカウント作成（manager.line.biz）"),
    ("02-enable-messaging-api", "設定 → Messaging API →「Messaging APIを利用する」の画面"),
    ("03-issue-token", "Developers Console → Messaging API設定 → チャネルアクセストークン（長期）発行"),
    ("04-channel-secret", "Basic settings（チャネル基本設定）→ Channel secret"),
    ("05-add-friend-qr", "Messaging API設定 → QRコード（Bot を友だち追加）"),
    ("06-your-user-id", "Basic settings → Your user ID（表示される場合）"),
    ("07-auto-reply-off", "Official Account Manager → 応答設定 → 応答メッセージ OFF"),
]

# 個人情報を隠す best-effort CSS（LINE の UI 変更で外れることがある＝目視確認は必須）
MASK_CSS = """
*[class*="account"], *[class*="email"], *[class*="Email"],
*[class*="userName"], *[class*="user-name"], *[class*="profile"],
header *[class*="name"] { filter: blur(7px) !important; }
"""


def main() -> None:
    print("=" * 60)
    print(" LINE 登録ガイド スクショ撮影（ダミーアカウントで！）")
    print("=" * 60)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto("https://developers.line.biz/console/")
        print("\n▶ 開いたブラウザで、ダミー/テスト用アカウントにログインしてください。")
        input("  ログインが終わったら Enter: ")

        for slug, desc in STEPS:
            ans = input(f"\n▶ {desc}\n   その画面を表示して Enter（スキップは s + Enter）: ").strip()
            if ans.lower() == "s":
                print("   スキップ")
                continue
            try:
                page.add_style_tag(content=MASK_CSS)
            except Exception:
                pass
            out = OUT / f"{slug}.png"
            page.screenshot(path=str(out), full_page=False)
            print(f"   ✓ 保存: {out}")

        browser.close()
    print("\n完了。docs/images/ を開いて、個人情報（メール・本名・実トークン・実userId）が")
    print("写っていないか必ず目視確認してください。問題なければガイドに埋め込みます。")


if __name__ == "__main__":
    main()

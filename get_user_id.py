#!/usr/bin/env python3
"""自分の LINE userId を取得するための一度きりのヘルパー。

LINE の userId は U[0-9a-f]{32} 形式で、アプリに表示される @ID とは別物。
取り方は2通り:

  (A) LINE Developers コンソール → チャネルの Basic settings → "Your user ID"
      （Business ID と連携済みの場合のみ表示）。これが見えるならこのスクリプトは不要。

  (B) Webhook で拾う（このスクリプト）。
      1) 先に Tailscale Funnel を有効化しておく（README 参照）。
      2) このスクリプトを起動: `uv run get_user_id.py`
      3) LINE Developers コンソールの Messaging API 設定で
         Webhook URL を  https://<あなたのFunnelホスト>/callback  に設定し、
         "Use webhook" を ON にする。
      4) スマホの LINE で、友だち追加した自分の Bot に何かメッセージを送る。
      5) このスクリプトが source.userId を表示するので、それを .env の LINE_USER_ID に貼る。

このスクリプトは Funnel 経由で公開されるため、なりすまし(偽の userId を送り込む)を
防ぐ目的で LINE_CHANNEL_SECRET による署名検証を「必須」とする（未設定なら起動を拒否）。
取得が終わったら必ず撤収すること:
  - このスクリプトを止める
  - `tailscale funnel reset` で公開を解除
  - LINE コンソールの Webhook URL を消す/OFFにする
  - 表示された userId が「自分が送ったメッセージ」由来か確認してから .env に貼る
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

PORT = int((os.environ.get("LINE_STATIC_PORT") or "8910").strip())
CHANNEL_SECRET = (os.environ.get("LINE_CHANNEL_SECRET") or "").strip()


def _valid_signature(body: bytes, signature: str) -> bool:
    # CHANNEL_SECRET は main() で必須化済み（ここに来る時点で必ず設定されている）
    mac = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode()
    return hmac.compare_digest(expected, signature or "")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # 静かに
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        sig = self.headers.get("X-Line-Signature", "")

        # LINE は応答 200 を期待するので、検証成否に関わらず 200 を返す
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

        if not _valid_signature(body, sig):
            print("⚠️  署名検証に失敗（LINE_CHANNEL_SECRET を確認）。無視します。", file=sys.stderr)
            return

        try:
            payload = json.loads(body.decode() or "{}")
        except json.JSONDecodeError:
            return

        for ev in payload.get("events", []):
            src = ev.get("source", {})
            uid = src.get("userId")
            if uid:
                print("\n========================================")
                print(f"  あなたの userId: {uid}")
                print("  → これを .env の LINE_USER_ID に貼ってください")
                print("========================================\n")


def main() -> None:
    if not CHANNEL_SECRET:
        sys.exit(
            "LINE_CHANNEL_SECRET が未設定です。Funnel 公開下で署名検証なしは危険なので、"
            "LINE コンソールの Channel secret を .env の LINE_CHANNEL_SECRET に設定してから実行してください。"
        )
    print(f"Webhook 受信待ち: 127.0.0.1:{PORT}/callback (Ctrl-C で終了)")
    print("Funnel 経由の公開 URL を LINE の Webhook URL に設定し、Bot にメッセージを送ってください。")
    print("取得後は必ず: スクリプト停止 → `tailscale funnel reset` → Webhook URL を削除/OFF。")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()

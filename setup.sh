#!/usr/bin/env bash
# sLINE (LINE ブリッジ MCP) セットアップスクリプト（macOS）。
# 依存チェック → uv sync → .env 用意 → launchd 配信サーバー登録 → 登録コマンド案内。
# 冪等（何度実行しても安全）。トークン等の秘密は対話で聞かず、.env に手で入れてもらう。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "==> sLINE セットアップ ($PROJECT_DIR)"

# 1) 前提チェック
if ! command -v uv >/dev/null 2>&1; then
  echo "✗ uv が必要です。インストール: https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi
command -v tailscale >/dev/null 2>&1 || echo "⚠️  tailscale が見つかりません（画像/動画の公開URL化に必須）"
command -v ffmpeg    >/dev/null 2>&1 || echo "⚠️  ffmpeg が見つかりません（動画送信に必要: brew install ffmpeg）"

# 2) 依存インストール
echo "==> uv sync"
uv sync

# 3) .env 用意
if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> .env を作成しました。エディタで以下を設定してください:"
  echo "     LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID / LINE_PUBLIC_BASE_URL"
else
  echo "==> .env は既存のため上書きしません"
fi

# 4) launchd 配信サーバー（127.0.0.1:PORT を常駐させ、Funnel の宛先を生かし続ける）
UV_BIN="$(command -v uv)"
PLIST_SRC="$PROJECT_DIR/com.line-bridge.static.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.line-bridge.static.plist"
if [ -f "$PLIST_SRC" ]; then
  mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"
  sed -e "s#__UV_BIN__#${UV_BIN}#g" -e "s#__PROJECT_DIR__#${PROJECT_DIR}#g" \
      "$PLIST_SRC" > "$PLIST_DST"
  launchctl unload -w "$PLIST_DST" 2>/dev/null || true
  launchctl load   -w "$PLIST_DST"
  echo "==> 配信サーバーを launchd に登録: $PLIST_DST"
else
  echo "⚠️  $PLIST_SRC が見つかりません（配信サーバーの常駐はスキップ）"
fi

# 5) 仕上げ案内
PORT="$(grep -E '^LINE_STATIC_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2)"
PORT="${PORT:-8910}"
cat <<EOF

==> 次の手順（詳細は README.md）:
  1. LINE Bot を作成し、アクセストークン/Channel secret を取得
  2. Tailscale Funnel を有効化:  tailscale funnel --bg ${PORT}
  3. .env を設定（トークン / userId / 公開URL）
  4. userId 未取得なら:  uv run python get_user_id.py
  5. 動作確認:           uv run python smoke_test.py
  6. Claude Code に登録（user スコープ＝全プロジェクトで使える）:
       claude mcp add --scope user line-bridge -- \\
         uv run --directory "$PROJECT_DIR" server.py

セットアップ完了。
EOF

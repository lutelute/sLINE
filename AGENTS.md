# AGENTS.md

AI エージェント（Claude Code など）向けの道標。このリポジトリを読んだら、まずここを見てください。

## このツールは何か
Claude Code が生成したテキスト/画像/動画を、ユーザー自身の LINE に push 送信する MCP サーバー。
詳細は [README.md](README.md)。

## あなたのタスク別ガイド

### このツールを誰かの環境に「導入」するなら
→ **[docs/AI_SETUP.md](docs/AI_SETUP.md) に従う**。要点：
- `./setup.sh`（Windows は `./setup.ps1`）で依存・配信サーバーを準備。
- **人間にしかできない操作**（LINE管理画面でのトークン発行、QRでの友だち追加、Tailscale 管理コンソールでの権限付与）は、勝手に進めず**手順を提示してユーザーの完了を待つ**。
- LINE Bot 登録の画面手順は [docs/SETUP_LINE.md](docs/SETUP_LINE.md)。
- `.env`（トークン等の秘密）は表示・コミットしない（`.gitignore` 済み）。
- 導入後は `uv run python smoke_test.py` → 実送信で確認。動画を送ったら「すぐスマホで開いて」と伝える。

### このリポジトリの「コードを改修」するなら
- ファイル配置は [README.md「提供ツールと構成ファイル」](README.md#提供ツールと構成ファイル)（ツリー）を参照。`server.py` はルート固定（MCP登録・launchd・CI が直接参照する外部契約）。
- 変更後は必ず `uv run python smoke_test.py`（認証不要・45チェック）。
- `server.py` を編集したら配信サーバーの常駐を再起動（macOS: `launchctl kickstart -k gui/$(id -u)/com.line-bridge.static`）。
- **クロスプラットフォームを壊さない**：OS 依存（ファイルロック・スリープ抑制・パス）は分岐で書く。GitHub Actions が macOS/Windows/Linux で smoke_test を検証する。
- コミットの author メールは GitHub の `noreply`（`{id}+user@users.noreply.github.com`）にする（個人メールだと GH007 で push 拒否される）。
- `.env*` は秘密。触れる必要があるときは値を出力しない。

## ツール（MCP）
- `send_text(message)` — テキスト送信（長文自動分割）
- `send_image(path, caption="")` — 画像送信（自動変換・プレビュー生成）
- `send_images(paths, caption="")` — 複数画像を1送信にまとめる
- `send_video(path, caption="")` — GIF/動画を mp4 にして送信（自動再生）
- `send_file(path, caption="")` — PDF 等の任意ファイルを公開URL化し「タップで開けるリンク」として送信（LINE はボットのファイル添付に非対応のため）
- `send_location(latitude, longitude, title="", address="")` — 地図ピンを送信（push のみ）
- `send_buttons(text, buttons, title="")` — 本文＋URLボタン（最大4・https必須）のアクション付き通知（push のみ）
- `send_stats(limit=20)` — 今月の使用通数と送信記録・遅延

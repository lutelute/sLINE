# AI に導入してもらうためのガイド

このツールは「**Claude Code などの AI エージェントに読ませて、導入作業を代行してもらう**」使い方を想定しています。
このページは「導入者が AI に出す指示の例」と「AI が従う手順」の両方をまとめたものです。

---

## A. 導入者へ — AI に出す指示の例（コピペ可）

AI（Claude Code など）をこのリポジトリのディレクトリで起動し、次のように頼んでください。

### まるごとおまかせ
```
このリポジトリ（sLINE）を私の環境にセットアップして。
README.md と docs/SETUP_LINE.md と docs/AI_SETUP.md を読んで、できる作業は進めて。
LINEの管理画面操作・トークン発行・QRでの友だち追加など「私がやるべきこと」は、
画面のどこを操作するか具体的に指示して、私の完了報告を待ってから次に進んで。
最後に send_text と send_image でテスト送信して、私のLINEに届くか確認させて。
```

### 段階的にやる
```
まず sLINE の setup.sh（Windowsなら setup.ps1）を実行して、依存と配信サーバーを準備して。
次に、私が LINE 側で取得すべき値（トークン・userId・公開URL）を一覧で教えて。
私が値を渡したら .env に設定して、smoke_test と実送信で動作確認して。
```

### 困ったとき
```
sLINE で画像が届かない。README のトラブルシュートと send_stats のログを見て原因を切り分けて。
```

---

## B. AI へ — このリポジトリを導入する手順

あなた（AI）がユーザーの環境に sLINE を導入するときの段取りです。**人間にしかできない操作**は勝手に進めず、具体的な手順を提示してユーザーの完了を待ってください。

### 0. 前提の確認
- `command -v uv`（必須）/ `command -v tailscale`（画像・動画に必須）/ `command -v ffmpeg`（動画に必要）を確認。
- 無いものはユーザーにインストールを促す（uv: 公式インストーラ、ffmpeg: `brew install ffmpeg` / `choco install ffmpeg`）。
- OS を確認（macOS / Windows / Linux）。

### 1. セットアップスクリプト
- macOS / Linux: `./setup.sh`
- Windows (PowerShell): `./setup.ps1`
- これで「依存インストール・`.env` 雛形・配信サーバーの常駐登録」まで済む。冪等なので再実行しても安全。

### 2. 人間に依頼すること（AI には実行できない＝手順を提示して待つ）
1. **LINE公式アカウント作成 → Messaging API 有効化**（[docs/SETUP_LINE.md](SETUP_LINE.md) の §1〜2 を案内）。
2. **Channel access token（長期）と Channel secret の発行**（管理画面のどのタブか具体的に指示）。
3. **Bot を友だち追加**（スマホの LINE で QR を読む。push の宛先になるため必須）。
4. **Tailscale 管理コンソールでの設定**（MagicDNS / HTTPS証明書の有効化、Funnel 権限の ACL 付与）。

→ これらは「ブラウザ/スマホでユーザーが操作」する必要がある。**1ステップずつ依頼し、完了報告を待つ**こと。

### 3. AI ができること（ユーザーの値を受け取って実行）
- ユーザーが貼ったトークン等を `.env` に設定（※ `.env` は `.gitignore` 済み。中身を会話ログに残しすぎない、コミットしない）。
- `tailscale funnel --bg <PORT>` の実行と `tailscale funnel status` の確認。
- 公開URL（`https://<host>.ts.net`）を `.env` の `LINE_PUBLIC_BASE_URL` に設定。
- userId 取得（[docs/SETUP_LINE.md](SETUP_LINE.md) §5）。`get_user_id.py` の起動 → ユーザーに Bot へ送信を依頼 → 表示された userId を `.env` に設定 → 撤収（funnel reset / Webhook OFF）。
- MCP 登録: `claude mcp add --scope user line-bridge -- uv run --directory "<このリポジトリの絶対パス>" server.py`
- `uv run python smoke_test.py` で自己テスト（認証不要・45チェック）。
- 実送信テスト: `send_text` / `send_image`（あれば `send_video`）でユーザーの LINE に届くか確認。

### 4. 動作確認とユーザーへの確認
- smoke_test が全通過することを確認。
- 実際に送って、ユーザーに「LINE に届いたか」を聞く。
- **動画を送ったら「すぐスマホで開いて」と伝える**（動画はスマホが再生時に直接取得するため、Mac がスリープすると取得失敗する。README「制約・注意」参照）。

### 5. 注意点（AI が守ること）
- `.env` の中身（トークン・userId・secret）は秘密。表示・コミットを避ける。
- 月間クォータ（無料枠 200通/月、既定の安全上限 180/月）があるので、テスト送信を無駄打ちしない。
- `server.py` を編集したら launchd / タスクスケジューラの常駐を再起動（README 参照）。
- クロスプラットフォーム（macOS/Windows/Linux）を壊さない。OS 依存は分岐で書く。

---

## 関連ドキュメント
- [README.md](../README.md) — 全体像・セットアップ・トラブルシュート
- [docs/SETUP_LINE.md](SETUP_LINE.md) — LINE Bot 登録の詳細（画面の場所つき）

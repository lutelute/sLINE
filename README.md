# LINE ブリッジ MCP（sLINE）

Claude Code が生成したテキスト・画像・動画を、**自分の LINE に push 送信**する MCP サーバー。
「スマホから Claude Code に指示は出せるけど、出来上がった画像や結果が手元で見られない」を解決する。

> 状態: テキスト・画像・動画(GIF→mp4)・遅延記録・月間クォータすべて稼働。実測 push受理→LINE取得 約 0.1s（画像）。動画は PC版・スマホ版の LINE で再生確認済み。

```
 スマホ ──(Remote Control / SSH)──▶ Mac で動く Claude Code
                                        │ ツール呼び出し
                                        ▼
                                  LINE ブリッジ MCP (server.py)
                                        │
                  ┌─────────────────────┼──────────────────────┐
                  │ テキスト             │ 画像・動画            │
                  ▼                      ▼                       │
        LINE Messaging API        ① public/ にメディアを置く     │
        push(text)                ② 127.0.0.1:PORT で配信        │
                                  ③ Tailscale Funnel で公開URL化 │
                                  ④ push(image/video, 公開URL) ──┘
                                        │
                                        ▼
                                  📱 自分の LINE に届く
```

なぜこの構成かというと、**LINE の画像・動画送信は「公開 HTTPS URL」が必須**で、ローカルファイルを直接添付できないため。Mac が Tailscale 上にいれば、**Tailscale Funnel** で `public/` を無料で公開 URL 化して解決できる。

> 補足: 以前主流だった **LINE Notify は 2025-03-31 にサービス終了済み**。本ツールは後継の **LINE Messaging API** を使う。

---

## 必要なもの（前提）

- **OS: macOS / Windows / Linux**（macOS=launchd+caffeinate、Windows=タスクスケジューラ+SetThreadExecutionState、Linux=コアのみ・常駐は各自。詳細は末尾「[対応プラットフォーム](#対応プラットフォーム)」）
- **[uv](https://docs.astral.sh/uv/)**（Python 実行・依存管理）
- **[Tailscale](https://tailscale.com/)**（画像・動画の公開 URL 化に **Funnel** を使う。テキストのみなら不要）
- **ffmpeg**（動画 `send_video` を使う場合のみ。`brew install ffmpeg`）
- **LINE 公式アカウント**（無料。Messaging API チャネル）
- **Claude Code**（この MCP を登録するホスト）

> Funnel の代わりに別のトンネル（Cloudflare Tunnel 等）で 127.0.0.1:PORT を HTTPS 公開しても動くが、本 README は Tailscale Funnel 前提で書く。

---

## クイックスタート

**macOS / Linux:**
```bash
git clone <this-repo> sLINE && cd sLINE
./setup.sh          # 依存チェック → uv sync → .env 用意 → 配信サーバーを常駐登録
```

**Windows (PowerShell):**
```powershell
git clone <this-repo> sLINE; cd sLINE
./setup.ps1         # 同上（配信サーバーはタスクスケジューラに登録）
```

`setup.sh` / `setup.ps1` は冪等（何度実行しても安全）。実行後、表示される案内に従って LINE 側設定（下記セットアップ §1〜）と `.env` 記入を済ませれば完了。

---

## AIに導入してもらう

このツールは **AI エージェント（Claude Code など）に読ませて導入を代行させる**使い方を想定しています。AI に出す指示の例と AI が従う手順は **[docs/AI_SETUP.md](docs/AI_SETUP.md)** に、AI が自動で読む道標は **[AGENTS.md](AGENTS.md)** にあります。

例: リポジトリのディレクトリで Claude Code を起動し、こう頼む 👇
```
このリポジトリ（sLINE）を私の環境にセットアップして。docs/AI_SETUP.md に従って、
できる作業は進めて。LINE管理画面の操作など私がやるべきことは具体的に指示して。
最後に send_text と send_image でテスト送信して、LINEに届くか確認させて。
```

---

## 構成ファイル

| ファイル | 役割 |
|---|---|
| `server.py` | MCP 本体。`send_text` / `send_image` / `send_video` / `send_stats` ツール + メディア配信用の静的サーバー内蔵 |
| `setup.sh` | セットアップ自動化（依存チェック・`uv sync`・`.env`用意・launchd登録） |
| `get_user_id.py` | 自分の LINE userId を Webhook で取得する一度きりのヘルパー |
| `.env.example` | 設定テンプレート（→ `.env` にコピーして使う） |
| `smoke_test.py` | 認証情報なしで動く部分の自己テスト |
| `com.line-bridge.static.plist` | 配信サーバーを常駐させる launchd テンプレート（`setup.sh` が実値を埋めて配置） |
| `pyproject.toml` | 依存定義（uv 管理） |
| `docs/SETUP_LINE.md` | LINE Bot 登録の詳細ガイド（初心者向け・画面の場所つき） |

ツール:
- **`send_text(message)`** — テキストを LINE に送る（長文は自動分割）
- **`send_image(path, caption="")`** — ローカル画像を公開 URL 化して LINE に送る（PNG/JPEG 以外や 10MB 超は自動変換・縮小、プレビューも自動生成。※アニメGIFは静止画になる→動かすなら `send_video`）
- **`send_video(path, caption="")`** — GIF/動画を mp4 に変換して LINE に送る（トークで自動再生。最大 1 分・200MB、preview 自動生成、**ffmpeg 必須**。H.264 baseline + 16の倍数解像度 + 無音音声トラックで生成し、スマホ/PC での再生互換を確保）
- **`send_stats(limit=20)`** — 今月の使用通数（無料枠の安全上限つき）と、最近の送信記録・遅延を返す（下記参照）

---

## 送信ログ・遅延の管理

すべての送信は `logs/sends.jsonl` に1行1イベントで記録される。

- **send イベント**：`send_text`/`send_image`/`send_video` が push を投げた記録。`push_ms`（LINE API の応答時間）を含む。
- **fetch イベント**：配信サーバーが（実在する）メディアを返した記録。**外部クライアント（通常は LINE）が取りに来た瞬間**。

画像・動画は send と fetch を**ファイル名**で突き合わせることで、**「push受理 → 初回取得（通常は LINE）までの遅延」**（＝体感に近い実配信遅延）が分かる。LINE はサムネ(preview)を先に取りに来るので preview→original の順で最初の取得を採用し、成功送信のみ・負値（クロックずれ）は除外する。テキストは LINE が即時配信するため、指標は push 応答時間のみ。

確認方法（どちらも同じ集計）：
- ターミナル：`uv run python server.py --stats`
- スマホから：Claude に「`send_stats` で最近の送信の遅延を見せて」と頼む

出力例：
```
今月の送信: 3/180 通（無料枠を使い切らないための安全上限。LINE 実上限は 200通/月）

直近 2 件（時刻 | 種別 | 成否 | push応答 | 初回取得遅延）
2026-05-31T18:51:36+09:00 | video | OK | 252ms | 0.4s
2026-05-31T18:19:02+09:00 | text  | OK | 120ms | -
画像/動画 1 件中 1 件が取得済み / 平均初回取得遅延 0.4s
```
（自前の配信確認 `verify_served` は `X-Self-Check` ヘッダ付きで送るため、遅延計測のノイズにならない。）

> ⚠️ **コード更新時の注意**：`server.py` を編集したら、**launchd 常駐サーバーを再起動**しないと配信側（fetchログ・Range対応など）は古いコードのまま動き続ける：
> `launchctl kickstart -k gui/$(id -u)/com.line-bridge.static`

---

## 月間送信クォータ（無料枠の安全装置）

LINE 無料プラン（コミュニケーションプラン）は **200通/月**。**push リクエスト1回 = 1通**で、1回のリクエストに最大5吹き出し詰めても消費は1通（メッセージオブジェクトの数は通数に影響しない）。テキストの長文分割で 25000 字を超えると複数 push に分かれ、その push 回数分カウントされる。

このツールは、無料枠を**使い切らないため**にローカルで月間送信数を管理し、**送信の直前に上限超過を止める**：

- 既定の安全上限は **180通/月**（実上限 200 に対し 20 通のバッファ）。`.env` の `LINE_MONTHLY_LIMIT` で変更できる。
- カウントは `logs/usage.json`（`{"month":"YYYY-MM","count":N}`）。**月が変わると自動で 0 にリセット**。
- 送信が成功した分だけ加算し、失敗したら戻す（楽観的予約＋ロールバック）。複数セッション同時起動でも `fcntl` のファイルロックで二重計上しない。
- 残量は `send_stats` の先頭、または `uv run python server.py --stats` の先頭で確認できる。残りわずか／使い切りは送信結果に ⚠️ で表示される。

> 無料プランは上限到達で**送信できなくなるだけで課金は発生しない**（従量課金はスタンダードプランのみ）。この制限は「課金回避」ではなく「**肝心なときに枠を使い切っていて送れない**」を防ぐためのもの。安全側に倒したいので既定を 180 にしてある。

---

## セットアップ

`./setup.sh` で依存・配信サーバー・`.env` の雛形は用意される。残りは LINE 側の設定だ。

### 1. LINE 側：Bot（Messaging API チャネル）を作る

> 📖 **初めての人は詳細ガイド [docs/SETUP_LINE.md](docs/SETUP_LINE.md) を参照**（公式アカウント作成からトークン取得・友だち追加・userId取得まで、画面の場所つきで丁寧に説明）。以下は要点のみ。

1. [LINE Official Account Manager](https://manager.line.biz/) で公式アカウントを1つ作る（無料）。
2. 設定 → **Messaging API** を有効化。これで [LINE Developers Console](https://developers.line.biz/console/) に Messaging API チャネルが作られる（プロバイダーは適当に1つ作る）。
3. 対象チャネル → **Messaging API** タブ:
   - **Channel access token (long-lived)** を「発行」→ これを `.env` の `LINE_CHANNEL_ACCESS_TOKEN` に貼る。
   - 同じタブの **QR コード**をスマホの LINE で読み、自分の Bot を**友だち追加**する（push の宛先になるために必須）。
   - （任意）**Channel secret** を `.env` の `LINE_CHANNEL_SECRET` に入れておくと userId 取得時の署名検証ができる。

### 2. Tailscale Funnel でメディアを公開 URL 化

1. **管理コンソールで前提を有効化**（一度きり）: [Tailscale 管理コンソール](https://login.tailscale.com/admin/dns) → **MagicDNS** と **HTTPS 証明書** を有効化。
2. **Funnel 権限**を ACL に付与（管理コンソール → Access controls）:
   ```jsonc
   "nodeAttrs": [
     { "target": ["autogroup:member"], "attr": ["funnel"] }
   ]
   ```
3. **静的サーバーのポート(既定 8910)を公開**（バックグラウンド常駐）:
   ```bash
   tailscale funnel --bg 8910
   tailscale funnel status     # https://<your-host>.ts.net → 127.0.0.1:8910 を確認
   ```
   > ポートを変える場合は `.env` の `LINE_STATIC_PORT` と `tailscale funnel` の両方を一致させること。
4. `tailscale funnel status` に出る公開 URL（`https://<your-host>.ts.net`）を、`.env` の `LINE_PUBLIC_BASE_URL` に設定する。

### 3. 自分の userId を取得

userId は `U` + 32桁の16進（アプリに見える @ID とは別物）。

- **手っ取り早い方法**: コンソールの **Basic settings → "Your user ID"**（Business ID と連携済みなら表示される）。
- **確実な方法（Webhook）**:
  ```bash
  # 先に LINE_CHANNEL_SECRET を .env に入れること（未設定だと起動を拒否する）
  uv run python get_user_id.py        # 127.0.0.1:PORT で Webhook 受信待ち
  ```
  - コンソールの Messaging API 設定で **Webhook URL** を `https://<your-host>.ts.net/callback` にし、**Use webhook を ON**。
  - スマホで Bot に何かメッセージを送る → ターミナルに userId が表示される → **自分が送ったメッセージ由来か確認**して `.env` の `LINE_USER_ID` に貼る。
  - 取得後は**必ず撤収**: スクリプト停止 → `tailscale funnel reset` → Webhook URL を削除/OFF。

### 4. `.env` を仕上げる

`setup.sh` がコピーした `.env` を編集して埋める:
```
LINE_CHANNEL_ACCESS_TOKEN=（手順1のトークン）
LINE_USER_ID=Uxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
LINE_PUBLIC_BASE_URL=https://<your-host>.ts.net
LINE_MONTHLY_LIMIT=180   # 任意。無料枠200通/月に対する安全上限（既定180）
```
`.env` は `.gitignore` 済み。コミットしないこと。

### 5. 配信サーバーの常駐（setup.sh が実施済み）

`tailscale funnel` の宛先(127.0.0.1:PORT)で**メディアを配信するプロセス**は、Claude Code とは別に常時生きている必要がある。理由: LINE は push の数秒後にメディア URL を「後から取りに来る」ため、送信直後に Claude Code を閉じると取得前に配信が止まり、**送ったのに表示されない**ことがある。

`setup.sh` が `com.line-bridge.static.plist` を実値で生成して `~/Library/LaunchAgents/` に配置・load 済み。確認・操作:
```bash
launchctl list | grep line-bridge                                  # 常駐確認
launchctl kickstart -k gui/$(id -u)/com.line-bridge.static         # 再起動（コード更新後に必要）
launchctl unload -w ~/Library/LaunchAgents/com.line-bridge.static.plist  # 停止
```
こうすると MCP は起動時に「既に PORT が動いている」ことを検知して常駐サーバーに委譲する（自前では立てない）。

### 6. Claude Code に MCP を登録（user スコープ＝全プロジェクトで使える）

```bash
claude mcp add --scope user line-bridge -- \
  uv run --directory "$(pwd)" server.py
claude mcp list   # 登録確認
```

### 7. 動作確認

```bash
uv run python smoke_test.py   # 認証なしで通る自己テスト
```
新しい Claude Code セッションで:
> 「`send_text` で『テスト』と LINE に送って」
> 「このグラフ `/abs/path/plot.png` を `send_image` でLINEに送って」

スマホの LINE に届けば成功。

---

## スマホからの使い方

MCP は **Claude Code が動いているマシン（Mac）でしか動かない**。スマホは「窓口」として使う:

- **Remote Control**（推奨）: Mac で `claude remote-control`（`claude --rc`）→ 表示される QR / URL を Claude アプリで開く。セッションは Mac でローカルに動き続け、MCP もそのまま使える。
- **SSH + tmux**: スマホの SSH クライアントで Mac に入り、tmux 内で Claude Code を動かす。

どちらでも、Claude が `send_image`/`send_video` を呼べば、**スマホが Mac と同じネットワークに居なくても**結果が LINE に届く。これが「ローカルでしか見られない」問題の解消点。

### 無人実行（cron / headless）で使う場合
```bash
claude -p "…処理して結果を send_image で送って" \
  --mcp-config "{\"mcpServers\":{\"line-bridge\":{\"command\":\"uv\",\"args\":[\"run\",\"--directory\",\"$(pwd)\",\"server.py\"]}}}" \
  --allowedTools "mcp__line-bridge__send_text,mcp__line-bridge__send_image,mcp__line-bridge__send_video"
```

---

## 制約・注意

- **無料枠は 200通/月**（コミュニケーションプラン）。**push 1回 = 1通**（吹き出しを最大5つ詰めても1通／メッセージオブジェクト数は通数に無関係）。長文は 5000字×5＝25000字ごとに 1 push 増える。**上限到達で送信不可になるだけで課金は発生しない**（従量課金はスタンダードプランのみ）。本ツールは安全上限（既定180/月）で**送信前に自動で止める**（上記「月間送信クォータ」参照）。
- **画像・動画は「push成功＝受理」であって表示完了ではない**。LINE がこの直後に Funnel 経由で URL を取りに来た時点で表示される。その間 Mac が起きていて Funnel と配信サーバーが生きている必要がある。→ **launchd 常駐**を推奨。
- **アニメ GIF は image では動かない**（LINE 仕様で静止画化される）。動かすなら **`send_video`** を使う—GIF/動画を ffmpeg で mp4（H.264 baseline + yuv420p + 16の倍数解像度 + 無音音声トラック + faststart）に変換し、video メッセージとして送ると**トークで自動再生**される。`ffmpeg` が必要。
- **動画はスマホが「再生時」に直接 mp4 を取得する**（実測: iPhone の `AppleCoreMedia` が Range 取得）。画像は LINE がキャッシュ配信するが、**動画はキャッシュされず端末が都度取りに来る**ため、**再生する瞬間に Mac が起きていて配信が生きている必要がある**。送ったら**すぐ開く**、または **Mac を電源接続＋ふた開け**でスリープを防ぐと確実。これを怠ると「受信できたのに再生できない（音だけ/真っ黒/くるくる）」になる。
- **スリープの注意（正直なところ）**: 送信後はスリープ抑制(`caffeinate`)を試みるが、**バッテリー駆動で「ふたを閉じる」スリープは防げない**。確実にしたいなら電源接続のまま、または常時起動のサーバーで配信する。
- **Claude Code の Web 版（クラウドサンドボックス）では使えない**。Web 版はローカルMCPが存在しないため。ローカル/サーバーで動かすこと。
- 公開 URL は推測困難なファイル名（`secrets.token_hex`）を使い、配信サーバーは**そのパターンのファイルのみ**返す（ディレクトリ一覧・任意パス・シンボリックリンクは404）。それでも URL を知る者は取得できる。`public/` の古いファイルは**1時間**で自動削除（送信失敗時はその場で削除）。

## トラブルシュート

| 症状 | 対処 |
|---|---|
| 画像が届かない/サムネだけ出ない | `tailscale funnel status` で公開を確認。`curl https://<your-host>.ts.net/` が静的サーバーに届くか。 |
| `LINE API エラー 401` | `LINE_CHANNEL_ACCESS_TOKEN` が誤り or 失効。再発行。 |
| `LINE API エラー 400 ... invalid to` | `LINE_USER_ID` が誤り（@ID ではなく U... の userId か確認）。 |
| `送信中止: 今月の送信が安全上限に…` | ローカルの安全上限（既定180/月）に到達。翌月リセット。急ぐなら `.env` の `LINE_MONTHLY_LIMIT` を上げる（実上限200まで）。 |
| 送信できない・429系 | LINE 側の月間無料枠 200通 を超えた可能性。翌月まで待つかプラン変更。 |
| ポート競合 | 既定 `8910` が埋まっていたら `.env` の `LINE_STATIC_PORT` と `tailscale funnel` の両方を別ポートに。 |
| GIFが動かない（静止画になる） | LINE の image はアニメ非対応。`send_video` で送る（mp4 化して動画送信）。 |
| `動画の処理に失敗しました` | `ffmpeg` 未インストール（`brew install ffmpeg`）/ 1分超 / 変換不可な形式。 |
| 動画は受信できるが再生で固まる | mp4 の profile/解像度/音声トラック非互換。本ツールは baseline + 16の倍数 + 無音音声で生成し対応済み（古い形式で送ったものは再送）。 |
| 動画がスマホで再生できない（PCはOK／音だけ・真っ黒・くるくる） | スマホは**再生時に直接 mp4 を取得**するため、その瞬間に Mac が寝ていると失敗する。送ったら**すぐ開く**、または Mac を**電源接続＋ふた開け**でスリープ防止。`send_stats` で動画の取得（fetch）有無を確認できる。 |
| 動画(mp4)だけ届かない/サムネのみ | 配信側 launchd が旧コードで `.mp4` を404にしている可能性。`lsof -nP -i :8910` で残骸プロセスを確認し kill → `launchctl kickstart -k gui/$(id -u)/com.line-bridge.static`。 |

---

## 対応プラットフォーム

| OS | 状態 | セットアップ | 配信常駐 | スリープ抑制 |
|---|---|---|---|---|
| macOS | ✅ 実運用確認済み | `setup.sh` | launchd | caffeinate |
| Windows | 🧪 CI検証（実送信は要確認） | `setup.ps1` | タスクスケジューラ | SetThreadExecutionState |
| Linux | 🧪 コア動作（CI） | `setup.sh`（常駐は対象外） | systemd 等で各自 | なし |

クロスプラットフォームの自己テスト（`smoke_test.py`）は **GitHub Actions** で macOS / Windows / Linux すべてで自動実行される（import・画像処理・月間クォータ・Range配信・GIF→mp4変換を検証）。実際の LINE 送信は認証が要るため CI では行わないので、各環境で `.env` 設定後に確認すること。

---

## ライセンス

[MIT](LICENSE)

# LINE Bot セットアップガイド

sLINE が LINE にメッセージを送るには、**自分専用の LINE Bot（Messaging API チャネル）** が必要です。
所要 10〜15 分、すべて**無料**でできます。このガイドは初めての人向けに、つまずきやすい所を補足しながら順を追って説明します。

## 全体像

1. LINE公式アカウントを作る
2. Messaging API を有効化する
3. アクセストークンと Channel secret を取得する
4. Bot を自分の LINE に友だち追加する（**必須**）
5. 自分の userId を取得する
6. 自動応答メッセージを OFF にする（推奨）

取得した値は最終的に `.env` に書きます（対応は各手順に明記）。

> 📷 **実際の画面を見たいときは、LINE公式ドキュメントが画像付き・最新です**（自前スクショより正確で、UI変更にも追従。個人情報の心配もなし）:
> - [Messaging APIを始めよう](https://developers.line.biz/ja/docs/messaging-api/getting-started/) — 公式アカウント作成・Messaging API有効化（§1〜2）
> - [ボットを作成する](https://developers.line.biz/ja/docs/messaging-api/building-bot/) — チャネル設定・トークン発行・友だち追加（§3〜4）
> - [チャネルアクセストークン](https://developers.line.biz/ja/docs/basics/channel-access-token/) — トークンの種類と発行方法（§3）

---

## 1. LINE公式アカウントを作る

1. [LINE Official Account Manager](https://manager.line.biz/) を開く。
2. 普段使っている **LINE アカウントでログイン**（または LINE Business ID を作成）。
3. 「**アカウントを作成**」→ アカウント名（何でもよい）・業種（個人なら「個人」「その他」等）を入力して作成。

> これは「LINE公式アカウント」＝あなたの Bot の入れ物です。無料の「コミュニケーションプラン」で始まります（200通/月まで無料）。

---

## 2. Messaging API を有効化する

1. Official Account Manager の右上 **「設定」** を開く。
2. 左メニューの **「Messaging API」**。
3. **「Messaging API を利用する」** をクリック。
4. **プロバイダー**を選択または新規作成（初回は新規。名前は自分の名前やプロジェクト名でよい）。
5. 規約に同意して有効化。

これで [LINE Developers Console](https://developers.line.biz/console/) 側に **Messaging API チャネル**が作られます。

---

## 3. アクセストークンと Channel secret を取得する

[LINE Developers Console](https://developers.line.biz/console/) を開き、作られたプロバイダー → 該当チャネルを選択します。

> 💡 タブ名は画面の言語で変わります。本ガイドは英語UI表記です（日本語UIでは「Messaging API」→ **「Messaging API設定」**、「Basic settings」→ **「チャネル基本設定」**）。

### 3-1. Channel access token（テキスト・画像・動画の送信に必須）

1. **「Messaging API」タブ**を開く。
2. 一番下の **「Channel access token (long-lived)」** の **「発行」** を押す。
3. 表示された長いトークンをコピー → `.env` の **`LINE_CHANNEL_ACCESS_TOKEN`** に貼る。

> ⚠️ 「long-lived（長期）」のトークンを使います。短期トークンや別の項目と間違えないこと。

### 3-2. Channel secret（userId 取得時の署名検証に使う）

1. **「Basic settings」タブ**を開く。
2. **「Channel secret」** をコピー → `.env` の **`LINE_CHANNEL_SECRET`** に貼る。

---

## 4. Bot を自分の LINE に友だち追加する（必須）

push の宛先になるため、**自分のスマホで Bot を友だち追加**します。これを忘れると送信しても届きません（`400 invalid to` の原因にもなる）。

1. Developers Console の **「Messaging API」タブ**にある **QR コード**（または Bot basic ID `@xxxx`）を表示。
2. スマホの LINE で QR を読み取る → **「追加」**。

---

## 5. 自分の userId を取得する

userId は **`U` で始まる 32 桁の英数字**です（友だち追加で見える `@xxxx` の ID とは別物）。

### 方法A（簡単・表示される場合）

Developers Console → **「Basic settings」タブ**の一番下 **「Your user ID」**。表示されていればこれをコピー → `.env` の **`LINE_USER_ID`** へ。

### 方法B（確実・Webhook で受け取る）

`Your user ID` が表示されない場合はこちら。先に Tailscale Funnel（README の §2）を有効化しておく。

1. `.env` に `LINE_CHANNEL_SECRET` を設定済みにする（未設定だとスクリプトが起動を拒否）。
2. ターミナルで起動:
   ```bash
   uv run python get_user_id.py
   ```
3. Developers Console → Messaging API 設定の **Webhook URL** を
   `https://<your-host>.ts.net/callback` にし、**「Use webhook」を ON**。
4. スマホで Bot に**何かメッセージを送る** → ターミナルに `あなたの userId: U...` が表示される。
5. 表示された userId をコピー → `.env` の **`LINE_USER_ID`** へ。
6. **取得後は必ず撤収**:
   - `get_user_id.py` を停止（Ctrl-C）
   - `tailscale funnel reset` で公開を解除
   - Developers Console の Webhook URL を削除 / OFF

> get_user_id.py は署名検証（Channel secret）を必須にしており、なりすまし（偽 userId 送り込み）を防ぎます。表示された userId が「自分が送ったメッセージ由来」か確認してから使ってください。

---

## 6. 自動応答メッセージを OFF にする（推奨）

Bot は初期状態で、メッセージを受け取ると定型文を自動返信します。煩わしいので切っておくと快適です。

1. Official Account Manager → **「設定」→「応答設定」**。
2. **「応答メッセージ」** を OFF（チャットを使うなら「チャット」を ON）。
3. 「あいさつメッセージ」も任意で OFF。

---

## まとめ：`.env` に入る値

| `.env` のキー | 取得元 |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | §3-1 Messaging API タブ → Channel access token (long-lived) |
| `LINE_CHANNEL_SECRET` | §3-2 Basic settings タブ → Channel secret |
| `LINE_USER_ID` | §5 Your user ID または get_user_id.py |
| `LINE_PUBLIC_BASE_URL` | Tailscale Funnel の公開 URL（README §2） |

## つまずきポイント

- **友だち追加を忘れない**（§4）。届かない原因の筆頭。
- **userId（`U...`）と @ID は別物**。@ID を入れても送れない。
- トークンは **long-lived** を使う。
- 無料枠は **200通/月**。sLINE は安全上限（既定 180/月）で自動的に止めるので使い切る心配は少ない。
- `401` エラー → トークンが誤り/失効。再発行。
- `400 invalid to` → userId が誤り、または友だち未追加。

設定が終わったら、リポジトリ直下に戻って README の続き（Funnel・`.env`・MCP 登録）を進めてください。

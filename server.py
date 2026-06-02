#!/usr/bin/env python3
"""LINE ブリッジ MCP サーバー。

Claude Code が生成したテキスト/画像を、自分の LINE に push 送信する個人用 MCP。

仕組み:
  - LINE Messaging API の push エンドポイントにテキスト/画像メッセージを送る。
  - 画像は「公開 HTTPS URL」が必須なので、静的ファイルサーバー
    (127.0.0.1:LINE_STATIC_PORT) に画像を置き、Tailscale Funnel で
    公開した URL (LINE_PUBLIC_BASE_URL) 経由で LINE のサーバーに取得させる。
  - 重要: push が HTTP 200 を返すのは「受理」であって配信完了ではない。
    LINE のサーバーは push の数秒後に originalContentUrl/previewImageUrl を
    "後から取りに来る"。その取得が終わるまで、このマシン・静的サーバー・
    Tailscale Funnel が生きてネットに繋がっている必要がある。
    → 配信を Claude Code のセッション寿命に依存させないため、静的サーバーは
      launchd で `--static-only` 常駐させるのを推奨（README 参照）。

ツール:
  - send_text(message)            : テキストを LINE に送る
  - send_image(path, caption="")  : ローカル画像を公開 URL 化して LINE に送る

注意:
  - stdout は JSON-RPC 専用。ログ・print は必ず stderr へ。
  - LINE Notify はサービス終了済み(2025-03-31)。本サーバーは Messaging API を使う。
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import re
import secrets
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Windows 等で stdout/stderr が非UTF-8（cp1252等）だと、日本語ログや JSON-RPC 応答で
# UnicodeEncodeError になるため UTF-8 に統一する（stdout=JSON-RPC, stderr=ログ）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")  # サーバーと同じ場所の .env を読む（秘密情報はここ）

PUSH_URL = "https://api.line.me/v2/bot/message/push"

# LINE 仕様の上限（2025-2026 時点）
ORIGINAL_MAX_BYTES = 10 * 1000 * 1000   # originalContentUrl は最大 10 MB
PREVIEW_MAX_BYTES = 1 * 1000 * 1000     # previewImageUrl は最大 1 MB
TEXT_MAX_CHARS = 5000                   # text メッセージは最大 5000 文字
MAX_MESSAGES_PER_PUSH = 5               # 1 回の push に詰められるメッセージ数
URL_MAX_CHARS = 2000                    # originalContentUrl/previewImageUrl の最大長

# 動画（video メッセージ）の上限。image はアニメ GIF を表示できない（静止画になる）ため、
# 動く絵は GIF/動画を mp4 に変換して video メッセージで送る（send_video）。
VIDEO_MAX_BYTES = 200 * 1000 * 1000     # originalContentUrl(mp4) は最大 200 MB
VIDEO_MAX_SECONDS = 60                  # 動画の長さは最大 1 分

# 公開ディレクトリのファイルを掃除する保持時間（秒）。LINE の取得は数秒で済むので短くてよい。
RETENTION_SECONDS = 60 * 60             # 1 時間
CLEANUP_INTERVAL = 600                  # 掃除スレッドの実行間隔（秒）

# 画像送信直後にプロセスが落ちると LINE の取得前に配信が止まるため、MCP プロセス内で
# 静的サーバーを抱えている場合は終了をこの秒数だけ猶予する（best-effort）。
GRACE_SECONDS = 60

# token は secrets.token_hex(16) = 32 桁の16進。配信サーバーはこのパターンのみ許可する
# （original は jpg/png/mp4、preview は jpg/png）。
TOKEN_FILE_RE = re.compile(r"^/[0-9a-f]{32}(\.(jpg|png|mp4)|_preview\.(jpg|png))$")

_last_image_send = 0.0  # 直近の画像送信時刻（猶予判定用）


def _log(*args: object) -> None:
    """stderr へログ出力（stdout は JSON-RPC 専用なので汚さない）。"""
    print("[line-bridge]", *args, file=sys.stderr, flush=True)


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


def _require_env(name: str) -> str:
    val = _env(name)
    if not val:
        raise RuntimeError(
            f"環境変数 {name} が未設定です。{BASE_DIR / '.env'} か MCP の env 設定で指定してください。"
        )
    return val


def public_dir() -> Path:
    d = Path(_env("LINE_PUBLIC_DIR") or (BASE_DIR / "public")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def static_port() -> int:
    return int(_env("LINE_STATIC_PORT") or "8910")


def public_base_url() -> str:
    base = _require_env("LINE_PUBLIC_BASE_URL").rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError(
            f"LINE_PUBLIC_BASE_URL は https:// で始まる必要があります（現在: {base!r}）。"
            " LINE は HTTPS(TLS1.2+) の URL しか受け付けません。"
        )
    return base


# ---------------------------------------------------------------------------
# 送信・配信ログ（遅延の記録と管理）
# ---------------------------------------------------------------------------
# logs/sends.jsonl に1行1イベントで追記する。
#   - send  : send_text/send_image が push を投げた記録（push 応答時間つき）
#   - fetch : 配信サーバーが画像を返した記録（外部クライアントが取りに来た瞬間）
# send と fetch を「ファイル名」で突き合わせると push受理→初回取得の実遅延が出る。
#
# 並行性:
#   _log_lock(threading.Lock) は「同一プロセス内のスレッド」だけを直列化する
#   （配信サーバーは ThreadingHTTPServer なので複数の fetch ハンドラが同時に呼ぶ）。
#   MCP プロセス(send)と launchd 配信プロセス(fetch)は別プロセスで別ロックなので、
#   この2プロセス間の安全性は _log_lock ではなく O_APPEND（追記の原子性）に依存する。
#   保証されるのは「ローカルFS」かつ「1行が OS の PIPE_BUF(macOS/Linux=4096B)未満」
#   のときのみ。各フィールドは短く保つ（ua は 200 字に切詰め）。NFS/SMB 上では非保証
#   なので logs/ はローカルディスクに置くこと。compute_stats は壊れた末尾行を
#   try/except で握りつぶす（この保護は消さないこと）。

LOG_FILE = BASE_DIR / "logs" / "sends.jsonl"
LOG_MAX_BYTES = 5 * 1000 * 1000   # 超えたら1世代ローテート
STATS_TAIL_BYTES = 512 * 1024     # 統計は末尾このサイズだけ読む（全読み回避）
_log_lock = threading.Lock()

try:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)  # 起動時に一度だけ（両プロセス）
except OSError:
    pass


def _now() -> tuple[float, str]:
    """(epoch秒, ローカルtz ISO文字列) を返す。"""
    return time.time(), datetime.now().astimezone().isoformat(timespec="seconds")


def _iso(epoch: float) -> str:
    """epoch秒から ts と epoch がズレないよう ISO 文字列を作る。"""
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def log_event(rec: dict) -> None:
    try:
        line = json.dumps(rec, ensure_ascii=False)
        with _log_lock:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:  # ログ失敗で送信機能を止めない
        pass


def rotate_log_if_big() -> None:
    """ログが大きくなりすぎたら1世代だけローテート（os.replace は原子的）。"""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE.with_suffix(".jsonl.1"))
    except OSError:
        pass


def _read_log_tail() -> list[str]:
    """sends.jsonl の末尾 STATS_TAIL_BYTES 分の行を返す（全読み回避）。"""
    if not LOG_FILE.exists():
        return []
    try:
        size = LOG_FILE.stat().st_size
        with open(LOG_FILE, "rb") as f:
            if size > STATS_TAIL_BYTES:
                f.seek(size - STATS_TAIL_BYTES)
                f.readline()  # 途中で切れた先頭行を捨てる
            data = f.read()
        return data.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []


def compute_stats(limit: int = 20) -> list[dict]:
    """末尾ログを読み、send を fetch(実在ファイルの取得)と「ファイル名」で突合する。

    画像の遅延 = (preview→original の順で最初に取得された時刻) - (push受理時刻)。
    成功(ok)した送信のみ対象。負値(クロックずれ)や不正epochは除外する。
    """
    sends: list[dict] = []
    first_fetch: dict[str, float] = {}  # ファイル名 -> 最初の取得epoch
    for line in _read_log_tail():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue  # 末尾の半端な行など。意図的に握りつぶす（消さないこと）。
        ev = r.get("event")
        if ev == "fetch":
            name, e = r.get("name"), r.get("epoch")
            if name and isinstance(e, (int, float)) and e > 0:
                if name not in first_fetch or e < first_fetch[name]:
                    first_fetch[name] = e
        elif ev == "send":
            sends.append(r)
    rows = []
    for s in sends[-limit:]:
        row = {
            "ts": s.get("ts"),
            "type": s.get("type"),
            "ok": s.get("ok"),
            "push_ms": s.get("push_ms"),
            "fetch_delay_s": None,
        }
        epoch = s.get("epoch")
        if s.get("ok") and s.get("type") in ("image", "video") and isinstance(epoch, (int, float)) and epoch > 0:
            cands = [first_fetch[n] for n in (s.get("preview"), s.get("original"))
                     if n and n in first_fetch]
            if cands:
                d = min(cands) - epoch
                if d >= 0:  # クロックずれによる負値は採用しない
                    row["fetch_delay_s"] = round(d, 1)
        rows.append(row)
    return rows


def format_stats(limit: int = 20) -> str:
    rows = compute_stats(limit)
    if not rows:
        return "送信記録はまだありません。"
    out = [f"直近 {len(rows)} 件（時刻 | 種別 | 成否 | push応答 | 初回取得遅延）"]
    n_img = n_fetched = 0
    delays = []
    for r in rows:
        if r["fetch_delay_s"] is not None:
            delay = f"{r['fetch_delay_s']}s"
            delays.append(r["fetch_delay_s"])
            n_fetched += 1
        elif r["type"] in ("image", "video"):
            delay = "未取得"
        else:
            delay = "-"
        if r["type"] in ("image", "video"):
            n_img += 1
        out.append(
            f"{r['ts']} | {r['type']} | {'OK' if r['ok'] else 'NG'} "
            f"| {r.get('push_ms', '?')}ms | {delay}"
        )
    if delays:
        avg = round(sum(delays) / len(delays), 1)
        out.append(f"\n画像/動画 {n_img} 件中 {n_fetched} 件が取得済み / 平均初回取得遅延 {avg}s")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 月間送信クォータ（無料枠を使い切らないための安全装置）
# ---------------------------------------------------------------------------
# LINE 公式アカウントの無料プラン（コミュニケーションプラン）は「200通/月」。通数は
# "push リクエスト1回 = 宛先1人あたり1通" でカウントされ、1リクエストに最大5吹き出し
# 詰めても消費は1通（メッセージオブジェクト数は通数に影響しない）。このツールの宛先は
# 常に自分1人なので「push 回数 = 消費通数」。無料プランは上限到達で送信不可になるだけで
# 課金は発生しない（従量課金はスタンダードプランのみ）が、"肝心なときに使い切っていて
# 送れない" を避けるため、実上限 200 より控えめな安全上限をローカルに持ち、送信の直前に
# 超過を止める。
#
# 仕組み: logs/usage.json に {"month": "YYYY-MM", "count": N} を持ち、月が変わると 0 に
# リセットする。送信の直前に reserve_quota で予約（加算）し、送信が失敗したら release_quota
# で戻す（楽観的予約＋ロールバック）。複数の Claude Code セッションが同時に MCP を起動
# しうるため、スレッドロック（_usage_lock）に加えて OS 別のファイルロック（_lock_file /
# _unlock_file: Unix=fcntl.flock, Windows=msvcrt.locking）でプロセス間も直列化する。
# logs/ はローカルディスク前提（ファイルロックは NFS/SMB では非保証）。

USAGE_FILE = BASE_DIR / "logs" / "usage.json"
LINE_FREE_TIER_LIMIT = 200       # LINE 無料プランの実上限（参考表示用）
DEFAULT_MONTHLY_LIMIT = 180      # 既定の安全上限（実上限 200 に対し 20 通のバッファ）
_usage_lock = threading.Lock()


# クロスプラットフォームのファイルロック（複数プロセス間の排他）。
# Unix(macOS/Linux) は fcntl.flock、Windows は msvcrt.locking、どちらも無ければ no-op
# （その場合でも同一プロセス内は _usage_lock で守られ、別プロセス競合は安全マージンで吸収）。
try:
    import fcntl as _fcntl

    def _lock_file(f) -> None:
        _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)

    def _unlock_file(f) -> None:
        _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)
except ImportError:  # Windows には fcntl が無い
    try:
        import msvcrt as _msvcrt

        def _lock_file(f) -> None:
            f.seek(0)
            try:
                _msvcrt.locking(f.fileno(), _msvcrt.LK_LOCK, 1)
            except OSError:
                pass  # 取得できなくてもスレッドロックで概ね守られる

        def _unlock_file(f) -> None:
            f.seek(0)
            try:
                _msvcrt.locking(f.fileno(), _msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    except ImportError:
        def _lock_file(f) -> None:
            pass

        def _unlock_file(f) -> None:
            pass


def monthly_limit() -> int:
    """安全上限を返す。環境変数 LINE_MONTHLY_LIMIT が正の整数ならそれを優先する。"""
    raw = _env("LINE_MONTHLY_LIMIT")
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
        _log(f"LINE_MONTHLY_LIMIT が不正です（{raw!r}）。既定 {DEFAULT_MONTHLY_LIMIT} を使います。")
    return DEFAULT_MONTHLY_LIMIT


def _current_month() -> str:
    """ローカルタイムゾーンの 'YYYY-MM'。月替わりの判定に使う。"""
    return datetime.now().astimezone().strftime("%Y-%m")


def _with_usage_locked(fn):
    """usage.json を排他ロックして読み、fn(dict)->(新dict, 戻り値) を適用し書き戻す。

    fn 内では I/O しないこと（ロック保持を最小化）。JSON 破損や型不正は空 dict 扱い
    （安全側: その後の上限チェックを必ず通すので、破損を理由に送りすぎることはない）。
    I/O 失敗は OSError を送出する（呼び出し側が安全側＝送信中止に倒す）。
    """
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    USAGE_FILE.touch(exist_ok=True)
    with _usage_lock:                       # 同一プロセス内スレッドの直列化
        with open(USAGE_FILE, "r+", encoding="utf-8") as f:
            _lock_file(f)                            # 別プロセスとの直列化（OS別実装）
            try:
                raw = f.read()
                try:
                    d = json.loads(raw) if raw.strip() else {}
                    if not isinstance(d, dict):
                        d = {}
                except json.JSONDecodeError:
                    d = {}
                new_d, ret = fn(d)
                f.seek(0)
                f.truncate()
                f.write(json.dumps(new_d, ensure_ascii=False))
                f.flush()
                os.fsync(f.fileno())
                return ret
            finally:
                _unlock_file(f)


def peek_quota() -> tuple[int, int]:
    """(今月の使用通数, 安全上限) を返す（予約しない）。月替わりはリセットを反映。

    I/O 失敗時は (0, 上限) を返す。これは早期 return 用の参考値で、実際の送信可否は
    送信直前の reserve_quota が確定させる（peek が緩くても送りすぎは起きない）。
    """
    limit = monthly_limit()

    def fn(d):
        if d.get("month") != _current_month():
            d = {"month": _current_month(), "count": 0}
        return d, int(d.get("count", 0) or 0)

    try:
        return _with_usage_locked(fn), limit
    except OSError:
        return 0, limit


def reserve_quota(n: int) -> tuple[bool, int, int]:
    """送信直前に n 通を予約する。

    返り値 (granted, used, limit):
      granted=True  … 予約成立。used は加算後の今月通数。
      granted=False … 上限超過で予約せず。used は現在の今月通数。
    月替わりはリセットを反映。I/O 失敗は OSError を送出（呼び出し側で送信中止に倒す）。
    """
    limit = monthly_limit()

    def fn(d):
        if d.get("month") != _current_month():
            d = {"month": _current_month(), "count": 0}
        cur = int(d.get("count", 0) or 0)
        if cur + n > limit:
            return d, (False, cur, limit)
        d["count"] = cur + n
        return d, (True, cur + n, limit)

    return _with_usage_locked(fn)


def release_quota(n: int) -> None:
    """送信失敗時に予約した n 通を戻す（同月のときのみ）。"""
    if n <= 0:
        return

    def fn(d):
        if d.get("month") == _current_month():
            d["count"] = max(0, int(d.get("count", 0) or 0) - n)
        return d, None

    try:
        _with_usage_locked(fn)
    except OSError:
        pass  # 戻せなくても安全側（多めにカウント＝送信を絞る方向）なので無視


def _quota_note(used: int, limit: int) -> str:
    """送信結果メッセージに添える残量の注記。"""
    remaining = limit - used
    if remaining <= 0:
        return f" ⚠️ 今月の送信枠を使い切りました（{used}/{limit}通）。翌月まで送信できません。"
    if remaining <= max(10, limit // 10):
        return f" ⚠️ 今月の残り {remaining}通（{used}/{limit}）。"
    return f" 今月 {used}/{limit}通。"


# ---------------------------------------------------------------------------
# 静的ファイルサーバー（公開ディレクトリを 127.0.0.1:PORT で配信）
# ---------------------------------------------------------------------------

class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """token 形式のファイルだけを GET/HEAD で返す最小ハンドラ。

    Funnel で全インターネットに公開されるため、ディレクトリ一覧・任意パス・
    シンボリックリンクの追跡を禁止し、推測困難なファイル名の防御を機能させる。
    """

    # HTTP/1.1 にして Range/keep-alive を有効化（動画のシーク・ストリーミングに必須）。
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A003
        _log("static:", fmt % args)

    def list_directory(self, path):  # ディレクトリ一覧は一切出さない
        self.send_error(404)
        return None

    def translate_path(self, path: str) -> str:
        resolved = super().translate_path(path)  # ここで '..' は除去済み
        real = os.path.realpath(resolved)         # symlink を解決
        base = os.path.realpath(self.directory)
        try:
            if os.path.commonpath([real, base]) != base:
                return os.path.join(base, "__forbidden__")  # 範囲外 → 404
        except ValueError:
            return os.path.join(base, "__forbidden__")
        return real

    def _allowed(self) -> bool:
        return bool(TOKEN_FILE_RE.match(self.path.split("?", 1)[0]))

    def do_GET(self):  # noqa: N802
        if not self._allowed():
            self.send_error(404)
            return
        name = self.path.split("?", 1)[0].lstrip("/")
        fpath = public_dir() / name
        if not fpath.is_file():
            self.send_error(404)
            return
        # 自前の配信確認(verify_served)は X-Self-Check 付き → 記録しない。
        # それ以外の GET = 外部クライアント(通常はLINE)が取りに来た瞬間 → 遅延計測に使う。
        if not self.headers.get("X-Self-Check"):
            m = re.match(r"^([0-9a-f]{32})", name)
            t, iso = _now()
            log_event({
                "event": "fetch", "epoch": t, "ts": iso,
                "token": m.group(1) if m else None, "name": name,
                "ua": (self.headers.get("User-Agent", "") or "")[:200],
            })
        self._serve(fpath, head=False)

    def do_HEAD(self):  # noqa: N802
        if not self._allowed():
            self.send_error(404)
            return
        fpath = public_dir() / self.path.split("?", 1)[0].lstrip("/")
        if not fpath.is_file():
            self.send_error(404)
            return
        self._serve(fpath, head=True)

    def _serve(self, path, head: bool) -> None:
        """Range 対応でファイルを配信する。

        動画(mp4)は LINE/プレイヤーが Range(部分取得)でシークしようとするため、206
        Partial Content と Accept-Ranges/Content-Range を返さないと「受信できるが再生
        できない」ことがある。Python 標準の SimpleHTTPRequestHandler は Range 非対応
        （常に 200 で全体を返す）なので、ここで自前実装する。ファイル名は _allowed() の
        TOKEN_FILE_RE で検証済み（'/' や '..' を含まない固定パターン）なので安全。
        """
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error(404)
            return
        ctype = self.guess_type(str(path))
        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else size - 1
                else:  # bytes=-N （末尾 N バイト）
                    start = max(0, size - int(m.group(2)))
                    end = size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # クライアント切断などは無視（配信を止めない）


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # クライアント切断(BrokenPipe等)はノイズ。トレースバックを出さず1行だけ残す。
        _log(f"static: 接続処理中の例外を無視 ({client_address[0]})")


def _cleanup_loop() -> None:
    while True:
        time.sleep(CLEANUP_INTERVAL)
        try:
            cleanup_public_dir()
            rotate_log_if_big()
        except Exception:  # 掃除の失敗で配信を止めない
            pass


def start_static_server() -> bool:
    """公開ディレクトリを配信する静的サーバーを daemon スレッドで起動。

    すでにポートが使われている場合（launchd 常駐サーバー等）は何もしない。
    自前で起動した場合は True、既存サーバーに任せる場合は False を返す。
    """
    port = static_port()
    directory = str(public_dir())

    # ポートが既に listen 済みなら、外部の永続サーバーに任せる
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.3)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            _log(f"127.0.0.1:{port} は既に使用中。既存の配信プロセスに任せます。")
            return False

    handler = functools.partial(_QuietHandler, directory=directory)
    httpd = _ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, name="static-server", daemon=True).start()
    cleanup_public_dir()  # 前回の残骸を起動時に掃除
    threading.Thread(target=_cleanup_loop, name="cleanup", daemon=True).start()
    _log(f"静的サーバー起動: 127.0.0.1:{port} -> {directory}")
    return True


def cleanup_public_dir() -> None:
    """保持時間を過ぎた公開ファイルを削除（ディスク肥大・公開期間の抑制）。"""
    now = time.time()
    for p in public_dir().iterdir():
        try:
            if p.is_file() and (now - p.stat().st_mtime) > RETENTION_SECONDS:
                p.unlink()
        except OSError:
            pass


def verify_served(name: str) -> bool:
    """配信サーバー越しに実際に取得できるか（127.0.0.1）を確認する。

    別プロセスがポートを占有している／静的サーバーが起きていない、といった
    「送ったつもりで届かない」を送信前に検出する。
    """
    url = f"http://127.0.0.1:{static_port()}/{name}"
    try:
        req = urllib.request.Request(url, headers={"X-Self-Check": "1"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def keep_awake(seconds: int = 120) -> None:
    """送信後しばらくスリープを抑制する（best-effort）。

    macOS は caffeinate、Windows は SetThreadExecutionState、その他(Linux等)は no-op。
    注意: いずれもバッテリー駆動の「ふた閉じ」スリープは防げないことがある。確実にしたい
    場合は電源接続＋常時起動のサーバーで配信すること。特に動画はスマホが「再生時」に取りに
    来るため、送信後すぐ開くのが最も確実。
    """
    try:
        if sys.platform == "darwin":
            subprocess.Popen(
                ["caffeinate", "-dimsu", "-t", str(seconds)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif sys.platform == "win32":
            import ctypes

            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
            )
        # Linux 等は no-op（systemd-inhibit は環境依存のため入れない）
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 画像処理（LINE の制約に合わせて original / preview を用意）
# ---------------------------------------------------------------------------

def prepare_original(src: Path, token: str) -> Path:
    """original 用ファイルを公開ディレクトリに用意して返す。

    本物の JPEG/PNG・10MB 以下・安全なモード・回転なしならコピー（無劣化）。
    それ以外（CMYK/回転あり/別形式を誤った拡張子で持つ等）は再エンコード/縮小する。
    """
    dst_dir = public_dir()
    size = src.stat().st_size

    with Image.open(src) as im:
        fmt = im.format
        orient = im.getexif().get(274, 1)  # EXIF Orientation
        # 高速パス: 中身が本物の JPEG/PNG で、サイズ内・安全モード・回転なし
        if (
            fmt in ("JPEG", "PNG")
            and size <= ORIGINAL_MAX_BYTES
            and im.mode in ("RGB", "RGBA", "L", "LA", "P")
            and orient in (0, 1, None)
        ):
            ext = ".jpg" if fmt == "JPEG" else ".png"
            dst = dst_dir / f"{token}{ext}"
            shutil.copy2(src, dst)
            return dst

        # 再エンコードパス
        im.load()
        im = ImageOps.exif_transpose(im)  # EXIF 回転を画素に焼き込む

        # 透過の有無を厳密に判定（不透明なパレット画像を無駄に PNG にしない）
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            has_alpha = rgba.getchannel("A").getextrema()[0] < 255
        else:
            rgba = None
            has_alpha = False

        if has_alpha:
            dst = dst_dir / f"{token}.png"
            for max_dim in (4096, 3000, 2048, 1536, 1024, 768):
                tmp = rgba.copy()
                tmp.thumbnail((max_dim, max_dim))
                tmp.save(dst, "PNG", optimize=True)
                if dst.stat().st_size <= ORIGINAL_MAX_BYTES:
                    return dst
            return dst  # 最小版（事実上ほぼ無い）

        dst = dst_dir / f"{token}.jpg"
        work = im.convert("RGB")  # CMYK もここで正しく RGB 化される
        for max_dim in (4096, 3000, 2048, 1536, 1024):
            tmp = work.copy()
            tmp.thumbnail((max_dim, max_dim))
            for q in (90, 80, 70, 60):
                tmp.save(dst, "JPEG", quality=q, optimize=True)
                if dst.stat().st_size <= ORIGINAL_MAX_BYTES:
                    return dst
        return dst


def prepare_preview(src: Path, token: str) -> Path:
    """preview 用に 1 MB 以下のサムネイル(JPEG)を生成して返す。"""
    dst = public_dir() / f"{token}_preview.jpg"
    with Image.open(src) as im:
        im.load()
        im = ImageOps.exif_transpose(im)  # original と回転を一致させる
        work = im.convert("RGB")
        for max_dim in (1024, 800, 640, 480, 320):
            tmp = work.copy()
            tmp.thumbnail((max_dim, max_dim))
            for q in (85, 75, 65, 55, 45):
                tmp.save(dst, "JPEG", quality=q, optimize=True)
                if dst.stat().st_size <= PREVIEW_MAX_BYTES:
                    return dst
    return dst  # 最小版（事実上ほぼ無い）


# ---------------------------------------------------------------------------
# 動画処理（GIF/動画を LINE の video メッセージ用 mp4 + preview に変換）
# ---------------------------------------------------------------------------
# LINE の image メッセージはアニメ GIF を表示できない（静止画になる）。動く絵を送るには
# video メッセージ（mp4・最大200MB・最大1分）が必要なので、ffmpeg で GIF/動画を LINE
# 互換の mp4（H.264 / yuv420p / faststart）に変換し、最初のフレームを preview jpg にする。

def _ff_bin(name: str) -> str:
    """ffmpeg/ffprobe の実行パス。PATH 優先、無ければ Homebrew の既定場所を探す。

    Apple Silicon は /opt/homebrew、Intel Mac は /usr/local に入る。どちらにも無ければ
    名前だけ返し、実行時に分かりやすいエラー（要 brew install ffmpeg）で通知する。
    """
    found = shutil.which(name)
    if found:
        return found
    for cand in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        if os.path.exists(cand):
            return cand
    return name


def video_duration_seconds(src: Path) -> float | None:
    """動画/GIF の長さ（秒）を ffprobe で取得。取得できなければ None。"""
    try:
        out = subprocess.run(
            [_ff_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(src)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def _has_audio_stream(src: Path) -> bool:
    """入力に音声ストリームがあるか（無ければ mp4 化時に無音 AAC を足す判断に使う）。"""
    try:
        out = subprocess.run(
            [_ff_bin("ffprobe"), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=30,
        )
        return bool(out.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return False


def prepare_video(src: Path, token: str) -> Path:
    """GIF/動画を LINE 互換の mp4 に変換して公開ディレクトリに置き、返す。

    長さ超過（>1分）・変換失敗・サイズ超過（>200MB）は例外を送出する（呼び出し側が通知）。
    """
    dur = video_duration_seconds(src)
    if dur is not None and dur > VIDEO_MAX_SECONDS:
        raise ValueError(
            f"動画が長すぎます（{dur:.0f}秒）。LINE は最大 {VIDEO_MAX_SECONDS} 秒までです。"
        )

    dst = public_dir() / f"{token}.mp4"
    # LINE/モバイルで確実に再生させる設定: H.264 baseline + yuv420p + faststart。さらに
    # 音声トラックが無いと再生が固まる端末があるため、無音動画には無音 AAC を合成する。
    cmd = [_ff_bin("ffmpeg"), "-y", "-i", str(src)]
    if not _has_audio_stream(src):
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += [
        "-c:v", "libx264",
        "-profile:v", "baseline",                     # モバイル互換性が最も高いプロファイル
        "-level", "3.1",
        "-pix_fmt", "yuv420p",                        # 幅広い環境で再生できる画素形式
        # 幅・高さを 16 の倍数に黒帯パディング。モバイルの H.264 ハードデコーダは解像度が
        # 16 の倍数でないと「音は出るが映像が出ない」ことがある（Mac のソフトデコードは平気）。
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
               "pad=ceil(iw/16)*16:ceil(ih/16)*16:(ow-iw)/2:(oh-ih)/2:color=black",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",                    # moov atom を先頭へ（即時再生用）
        "-shortest",                                  # 合成した無音を映像長に合わせて切る
        str(dst),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as e:
        dst.unlink(missing_ok=True)
        raise RuntimeError("動画の変換がタイムアウトしました（300秒）。") from e
    except OSError as e:
        raise RuntimeError(f"ffmpeg を実行できません（インストール済みか確認）: {e}") from e
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"動画の mp4 変換に失敗しました: {(proc.stderr or '')[-300:]}")
    if dst.stat().st_size > VIDEO_MAX_BYTES:
        size_mb = dst.stat().st_size // (1000 * 1000)
        dst.unlink(missing_ok=True)
        raise ValueError(f"変換後の動画が大きすぎます（{size_mb}MB）。LINE は最大 200MB までです。")
    return dst


def prepare_video_preview(src: Path, token: str) -> Path:
    """動画/GIF の最初のフレームから preview 用 jpg（<=1MB）を生成して返す。"""
    frame = public_dir() / f"{token}_frame.png"
    cmd = [_ff_bin("ffmpeg"), "-y", "-i", str(src), "-frames:v", "1", str(frame)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as e:
        raise RuntimeError(f"動画のサムネイル抽出に失敗しました: {e}") from e
    if proc.returncode != 0 or not frame.exists():
        raise RuntimeError(f"動画のサムネイル抽出に失敗しました: {(proc.stderr or '')[-300:]}")
    try:
        return prepare_preview(frame, token)   # 既存の縮小ロジックを流用（<=1MB の jpg）
    finally:
        frame.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# LINE Messaging API
# ---------------------------------------------------------------------------

def _build_session() -> requests.Session:
    """一時的なネットワーク障害・5xx のみ自動リトライ（429 は除外）。"""
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=0.5,                     # 0.5s, 1s, 2s
        status_forcelist={500, 502, 503, 504},  # 一時的なサーバーエラーのみ
        allowed_methods={"POST"},
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


_session = _build_session()


def line_push(messages: list[dict]) -> None:
    """push API にメッセージ配列(最大5)を送る。失敗時は例外。"""
    if not messages:
        raise ValueError("messages が空です。")
    if len(messages) > MAX_MESSAGES_PER_PUSH:
        raise ValueError(f"1回の push は最大 {MAX_MESSAGES_PER_PUSH} メッセージまでです。")

    token = _require_env("LINE_CHANNEL_ACCESS_TOKEN")
    to = _require_env("LINE_USER_ID")
    try:
        resp = _session.post(
            PUSH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"to": to, "messages": messages},
            timeout=(5, 20),  # (connect, read)
        )
    except requests.RequestException as e:
        raise RuntimeError(f"LINE への接続に失敗しました（リトライ後）: {e}") from e

    if resp.status_code != 200:
        # 429 = 月間無料枠(200通/月)超過の可能性。待っても解消しないので自動リトライしない。
        hint = "（月間無料枠 200通/月 を超えた可能性があります）" if resp.status_code == 429 else ""
        raise RuntimeError(f"LINE API エラー {resp.status_code}{hint}: {resp.text}")


def _chunk_text(text: str) -> list[dict]:
    """長文を 5000 文字ごとに分割（切り捨てなし）。"""
    return [
        {"type": "text", "text": text[i : i + TEXT_MAX_CHARS]}
        for i in range(0, len(text), TEXT_MAX_CHARS)
    ]


# ---------------------------------------------------------------------------
# MCP ツール定義
# ---------------------------------------------------------------------------

from fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("line-bridge")


@mcp.tool
def send_text(message: str) -> str:
    """テキストメッセージを自分の LINE に送信する。

    Args:
        message: 送りたいテキスト。5000 文字超は自動分割し、25000 文字超は複数 push に分けて送る。
    Returns:
        送信結果の短い説明。
    """
    if not message or not message.strip():
        return "エラー: メッセージが空です。"
    all_msgs = _chunk_text(message)
    # 通数 = push リクエスト回数（宛先1人なので 1 push = 1通）。5吹き出し/push に詰める。
    needed = (len(all_msgs) + MAX_MESSAGES_PER_PUSH - 1) // MAX_MESSAGES_PER_PUSH
    try:
        granted, used, limit = reserve_quota(needed)
    except OSError as e:
        return f"エラー: 送信通数の管理に失敗し、安全のため送信を中止しました: {e}"
    if not granted:
        ep = time.time()
        log_event({"event": "send", "ts": _iso(ep), "epoch": ep, "type": "text",
                   "ok": False, "chars": len(message), "blocked": "quota",
                   "used": used, "limit": limit, "needed": needed})
        return (
            f"送信中止: 今月の送信が安全上限に達しています（{used}/{limit}通、必要 {needed}通）。"
            f"無料枠（{LINE_FREE_TIER_LIMIT}通/月）を使い切らないための制限です。翌月にリセットされます。"
            f" 上限の変更は .env の LINE_MONTHLY_LIMIT で。"
        )
    t0 = time.time()
    pushes = 0
    try:
        for i in range(0, len(all_msgs), MAX_MESSAGES_PER_PUSH):
            line_push(all_msgs[i : i + MAX_MESSAGES_PER_PUSH])
            pushes += 1
    except Exception as e:
        release_quota(needed - pushes)  # 成功した push 分だけはカウントに残す
        ep = time.time()
        log_event({"event": "send", "ts": _iso(ep), "epoch": ep, "type": "text",
                   "ok": False, "chars": len(message), "err": str(e)[:300]})
        return f"エラー: LINE への送信に失敗しました: {e}"
    ep = time.time()
    push_ms = int((ep - t0) * 1000)
    log_event({"event": "send", "ts": _iso(ep), "epoch": ep, "type": "text",
               "ok": True, "chars": len(message), "messages": len(all_msgs),
               "pushes": pushes, "push_ms": push_ms, "used": used, "limit": limit})
    return (
        f"LINE にテキストを送信しました"
        f"（{len(message)} 文字 / {len(all_msgs)} メッセージ / {pushes} push / {push_ms}ms）。"
        + _quota_note(used, limit)
    )


@mcp.tool
def send_image(path: str, caption: str = "") -> str:
    """ローカル画像ファイルを自分の LINE に送信する。

    画像はこのマシンの公開ディレクトリに置かれ、Tailscale Funnel 経由の公開 HTTPS URL
    として LINE に渡される。PNG/JPEG 以外・10MB 超・EXIF 回転・CMYK は自動で変換/縮小し、
    プレビュー(<=1MB)も自動生成する。

    Args:
        path: 送信したい画像の絶対パス（Pillow が開ける形式）。
        caption: 画像に添えるテキスト（任意。指定すると同じ push に同梱）。
    Returns:
        送信結果の短い説明。push 成功は「LINE が受理」を意味し、実際の表示は
        この直後に LINE がURLを取得した時点で行われる。
    """
    global _last_image_send

    src = Path(path).expanduser()
    if not src.is_absolute():
        src = (Path.cwd() / src).resolve()
    if not src.exists() or not src.is_file():
        return f"エラー: ファイルが見つかりません: {src}"

    # 先に残量を確認（上限なら重い画像処理を省く。最終判定は送信直前の reserve_quota）
    used, limit = peek_quota()
    if used >= limit:
        return (
            f"送信中止: 今月の送信が安全上限に達しています（{used}/{limit}通）。"
            f"無料枠（{LINE_FREE_TIER_LIMIT}通/月）を使い切らないための制限です。翌月にリセットされます。"
        )

    base = public_base_url()
    token = secrets.token_hex(16)

    # アニメ GIF は LINE の image では静止画になる。送信は通すが send_video を案内する。
    anim_hint = ""
    try:
        with Image.open(src) as probe:
            if getattr(probe, "is_animated", False) and getattr(probe, "n_frames", 1) > 1:
                anim_hint = " ℹ️ アニメGIFは静止画で送信しました。動かすなら send_video を使ってください。"
    except Exception:
        pass

    try:
        original = prepare_original(src, token)
        preview = prepare_preview(src, token)
    except Exception as e:  # 画像が壊れている / 巨大すぎる等
        return f"エラー: 画像の処理に失敗しました: {e}"

    original_url = f"{base}/{original.name}"
    preview_url = f"{base}/{preview.name}"
    if len(original_url) > URL_MAX_CHARS or len(preview_url) > URL_MAX_CHARS:
        _safe_unlink(original, preview)
        return "エラー: 生成された画像 URL が 2000 文字を超えました。"

    # 送る前に「実際に配信できているか」をローカルで確認（届かない送信を防ぐ）
    if not (verify_served(original.name) and verify_served(preview.name)):
        _safe_unlink(original, preview)
        return (
            f"エラー: ポート {static_port()} で画像を配信できていません"
            "（別プロセスがポート占有 / 静的サーバー未起動の可能性）。画像は送信しませんでした。"
        )

    messages: list[dict] = []
    if caption and caption.strip():
        messages.append({"type": "text", "text": caption[:TEXT_MAX_CHARS]})
    messages.append(
        {
            "type": "image",
            "originalContentUrl": original_url,
            "previewImageUrl": preview_url,
        }
    )

    # 送信直前に 1 通を予約（他セッションが先に枠を使い切っていないか最終確認）
    try:
        granted, used, limit = reserve_quota(1)
    except OSError as e:
        _safe_unlink(original, preview)
        return f"エラー: 送信通数の管理に失敗し、安全のため送信を中止しました: {e}"
    if not granted:
        _safe_unlink(original, preview)
        return (
            f"送信中止: 今月の送信が安全上限に達しています（{used}/{limit}通）。"
            f"無料枠（{LINE_FREE_TIER_LIMIT}通/月）を使い切らないための制限です。翌月にリセットされます。"
        )

    t0 = time.time()
    try:
        line_push(messages)
    except Exception as e:
        release_quota(1)  # 送信失敗 → 予約を戻す
        _safe_unlink(original, preview)  # 失敗した画像を公開したまま残さない
        ep = time.time()
        log_event({"event": "send", "ts": _iso(ep), "epoch": ep, "type": "image",
                   "ok": False, "token": token, "err": str(e)[:300]})
        return f"エラー: LINE への送信に失敗しました: {e}"

    ack = time.time()
    push_ms = int((ack - t0) * 1000)
    _last_image_send = ack
    keep_awake()  # 取得猶予の間スリープしにくくする（best-effort）
    log_event({"event": "send", "ts": _iso(ack), "epoch": ack, "type": "image", "ok": True,
               "token": token, "bytes": original.stat().st_size,
               "preview_bytes": preview.stat().st_size, "push_ms": push_ms,
               "original": original.name, "preview": preview.name,
               "used": used, "limit": limit})
    return (
        f"LINE に画像を送信キューしました（LINE が数秒以内に取得します）: {original_url} "
        f"[original {original.stat().st_size // 1024}KB / preview {preview.stat().st_size // 1024}KB / push {push_ms}ms]"
        + (" caption付き" if caption.strip() else "")
        + _quota_note(used, limit)
        + anim_hint
    )


@mcp.tool
def send_video(path: str, caption: str = "") -> str:
    """ローカルの動画やアニメーション GIF を自分の LINE に送信する（トークで自動再生）。

    LINE の image メッセージはアニメ GIF を表示できない（静止画になる）ため、動く絵を
    送りたいときはこちら。GIF/mp4/mov などを ffmpeg で LINE 互換の mp4 に変換し、
    Tailscale Funnel 経由の公開 HTTPS URL として video メッセージで送る（最大 1 分・200MB）。

    Args:
        path: 送信したい GIF/動画の絶対パス（ffmpeg が読める形式）。
        caption: 動画に添えるテキスト（任意。指定すると同じ push に同梱）。
    Returns:
        送信結果の短い説明。push 成功は「LINE が受理」で、実表示はこの直後の取得時。
    """
    global _last_image_send

    src = Path(path).expanduser()
    if not src.is_absolute():
        src = (Path.cwd() / src).resolve()
    if not src.exists() or not src.is_file():
        return f"エラー: ファイルが見つかりません: {src}"

    # 先に残量を確認（上限なら重い変換を省く。最終判定は送信直前の reserve_quota）
    used, limit = peek_quota()
    if used >= limit:
        return (
            f"送信中止: 今月の送信が安全上限に達しています（{used}/{limit}通）。"
            f"無料枠（{LINE_FREE_TIER_LIMIT}通/月）を使い切らないための制限です。翌月にリセットされます。"
        )

    base = public_base_url()
    token = secrets.token_hex(16)

    try:
        video = prepare_video(src, token)
        preview = prepare_video_preview(src, token)
    except Exception as e:  # 長すぎ / 変換失敗 / サイズ超過など
        _safe_unlink(public_dir() / f"{token}.mp4", public_dir() / f"{token}_preview.jpg")
        return f"エラー: 動画の処理に失敗しました: {e}"

    video_url = f"{base}/{video.name}"
    preview_url = f"{base}/{preview.name}"
    if len(video_url) > URL_MAX_CHARS or len(preview_url) > URL_MAX_CHARS:
        _safe_unlink(video, preview)
        return "エラー: 生成された URL が 2000 文字を超えました。"

    # 送る前に「実際に配信できているか」をローカルで確認（届かない送信を防ぐ）
    if not (verify_served(video.name) and verify_served(preview.name)):
        _safe_unlink(video, preview)
        return (
            f"エラー: ポート {static_port()} で動画を配信できていません"
            "（別プロセスがポート占有 / 静的サーバー未起動の可能性）。動画は送信しませんでした。"
        )

    messages: list[dict] = []
    if caption and caption.strip():
        messages.append({"type": "text", "text": caption[:TEXT_MAX_CHARS]})
    messages.append(
        {
            "type": "video",
            "originalContentUrl": video_url,
            "previewImageUrl": preview_url,
        }
    )

    # 送信直前に 1 通を予約（他セッションが先に枠を使い切っていないか最終確認）
    try:
        granted, used, limit = reserve_quota(1)
    except OSError as e:
        _safe_unlink(video, preview)
        return f"エラー: 送信通数の管理に失敗し、安全のため送信を中止しました: {e}"
    if not granted:
        _safe_unlink(video, preview)
        return (
            f"送信中止: 今月の送信が安全上限に達しています（{used}/{limit}通）。"
            f"無料枠（{LINE_FREE_TIER_LIMIT}通/月）を使い切らないための制限です。翌月にリセットされます。"
        )

    t0 = time.time()
    try:
        line_push(messages)
    except Exception as e:
        release_quota(1)  # 送信失敗 → 予約を戻す
        _safe_unlink(video, preview)  # 失敗した動画を公開したまま残さない
        ep = time.time()
        log_event({"event": "send", "ts": _iso(ep), "epoch": ep, "type": "video",
                   "ok": False, "token": token, "err": str(e)[:300]})
        return f"エラー: LINE への送信に失敗しました: {e}"

    ack = time.time()
    push_ms = int((ack - t0) * 1000)
    _last_image_send = ack
    keep_awake()  # 取得猶予の間スリープしにくくする（best-effort）
    log_event({"event": "send", "ts": _iso(ack), "epoch": ack, "type": "video", "ok": True,
               "token": token, "bytes": video.stat().st_size,
               "preview_bytes": preview.stat().st_size, "push_ms": push_ms,
               "original": video.name, "preview": preview.name,
               "used": used, "limit": limit})
    return (
        f"LINE に動画を送信キューしました（LINE が数秒以内に取得します）: {video_url} "
        f"[mp4 {video.stat().st_size // 1024}KB / preview {preview.stat().st_size // 1024}KB / push {push_ms}ms]"
        + (" caption付き" if caption.strip() else "")
        + _quota_note(used, limit)
    )


@mcp.tool
def send_images(paths: list[str], caption: str = "") -> str:
    """複数のローカル画像を1つの送信にまとめて自分の LINE に送る（push＝通数を節約）。

    LINE の1 push には最大5吹き出しまで詰められる。caption があれば先頭の1吹き出しを使い、
    残りに画像を入れる。合計が5を超えると自動的に複数 push に分割する（通数は push 回数分）。
    壊れている/見つからない画像はスキップし、送れた分だけ送る（スキップ分は結果に表示）。

    Args:
        paths: 画像の絶対パスのリスト。
        caption: 先頭に添えるテキスト（任意。最初の push に同梱）。
    Returns:
        送信結果の短い説明。
    """
    global _last_image_send

    if not paths or not isinstance(paths, list):
        return "エラー: paths は画像パスのリストで指定してください。"

    # 先に残量を確認（上限なら重い画像処理を省く。最終判定は送信直前の reserve_quota）
    used, limit = peek_quota()
    if used >= limit:
        return (
            f"送信中止: 今月の送信が安全上限に達しています（{used}/{limit}通）。"
            f"無料枠（{LINE_FREE_TIER_LIMIT}通/月）を使い切らないための制限です。翌月にリセットされます。"
        )

    base = public_base_url()
    prepared: list[tuple[Path, Path, str, str]] = []  # (original, preview, original_url, preview_url)
    errors: list[str] = []
    for p in paths:
        src = Path(str(p)).expanduser()
        if not src.is_absolute():
            src = (Path.cwd() / src).resolve()
        if not src.exists() or not src.is_file():
            errors.append(f"{p}（見つからない）")
            continue
        token = secrets.token_hex(16)
        try:
            original = prepare_original(src, token)
            preview = prepare_preview(src, token)
        except Exception:
            errors.append(f"{p}（画像処理失敗）")
            continue
        ou, pu = f"{base}/{original.name}", f"{base}/{preview.name}"
        if len(ou) > URL_MAX_CHARS or len(pu) > URL_MAX_CHARS:
            _safe_unlink(original, preview)
            errors.append(f"{p}（URL長超過）")
            continue
        if not (verify_served(original.name) and verify_served(preview.name)):
            _safe_unlink(original, preview)
            errors.append(f"{p}（配信不可）")
            continue
        prepared.append((original, preview, ou, pu))

    if not prepared:
        tail = (" — " + " / ".join(errors)) if errors else ""
        return f"エラー: 送信できる画像がありませんでした{tail}。"

    # メッセージ列を組み立て、5吹き出し/push に分割
    items: list[dict] = []
    if caption and caption.strip():
        items.append({"type": "text", "text": caption[:TEXT_MAX_CHARS]})
    for _o, _p, ou, pu in prepared:
        items.append({"type": "image", "originalContentUrl": ou, "previewImageUrl": pu})
    chunks = [items[i : i + MAX_MESSAGES_PER_PUSH] for i in range(0, len(items), MAX_MESSAGES_PER_PUSH)]
    needed = len(chunks)
    all_files = [f for pr in prepared for f in (pr[0], pr[1])]

    # 送信直前に push 数ぶんを予約
    try:
        granted, used, limit = reserve_quota(needed)
    except OSError as e:
        _safe_unlink(*all_files)
        return f"エラー: 送信通数の管理に失敗し、安全のため送信を中止しました: {e}"
    if not granted:
        _safe_unlink(*all_files)
        return (
            f"送信中止: 今月の送信が安全上限に達しています（{used}/{limit}通、必要 {needed}通）。"
            f"無料枠（{LINE_FREE_TIER_LIMIT}通/月）を使い切らないための制限です。翌月にリセットされます。"
        )

    t0 = time.time()
    pushes_done = 0
    try:
        for ch in chunks:
            line_push(ch)
            pushes_done += 1
    except Exception as e:
        release_quota(needed - pushes_done)  # 成功した push 分はカウントに残す
        ep = time.time()
        log_event({"event": "send", "ts": _iso(ep), "epoch": ep, "type": "images",
                   "ok": False, "count": len(prepared), "pushes": pushes_done, "err": str(e)[:300]})
        # 送信済み分は LINE の取得のため残し、未送信分だけ消す
        sent_imgs = pushes_done * MAX_MESSAGES_PER_PUSH
        leftover = [f for pr in prepared[max(0, sent_imgs - (1 if caption.strip() else 0)):] for f in (pr[0], pr[1])]
        _safe_unlink(*leftover)
        return f"エラー: 送信に失敗しました（{pushes_done}/{needed} push 成功）: {e}"

    ack = time.time()
    push_ms = int((ack - t0) * 1000)
    _last_image_send = ack
    keep_awake()
    log_event({"event": "send", "ts": _iso(ack), "epoch": ack, "type": "images", "ok": True,
               "count": len(prepared), "pushes": needed, "push_ms": push_ms,
               "used": used, "limit": limit})
    msg = f"LINE に画像 {len(prepared)} 枚を送信しました（{needed} push / {push_ms}ms）。"
    if errors:
        msg += f" ※{len(errors)}枚スキップ: " + " / ".join(errors)
    return msg + _quota_note(used, limit)


def _safe_unlink(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


@mcp.tool
def send_stats(limit: int = 20) -> str:
    """最近の LINE 送信の記録と今月の使用通数を返す（管理・遅延確認用）。

    先頭に「今月 何通 / 安全上限」を表示し、続けて各送信の push 応答時間、画像は
    「push受理→初回取得（通常は LINE）までの遅延」を表示する。
    スマホから「最近の送信の遅延と残り通数は？」と聞けば、この記録を確認できる。

    Args:
        limit: 表示する直近の件数（既定 20）。
    """
    used, lim = peek_quota()
    header = (
        f"今月の送信: {used}/{lim} 通"
        f"（無料枠を使い切らないための安全上限。LINE 実上限は {LINE_FREE_TIER_LIMIT}通/月）\n\n"
    )
    return header + format_stats(limit)


# ---------------------------------------------------------------------------
# 起動時の設定チェック（致命的にはしない。サマリを stderr に出すだけ）
# ---------------------------------------------------------------------------

def validate_config() -> None:
    required_text = ["LINE_CHANNEL_ACCESS_TOKEN", "LINE_USER_ID"]
    required_image = ["LINE_PUBLIC_BASE_URL"]
    set_vars = [n for n in required_text + required_image if _env(n)]
    missing = [n for n in required_text + required_image if not _env(n)]
    _log("設定チェック: 設定済み=", set_vars or "なし", "/ 未設定=", missing or "なし")
    if any(n in missing for n in required_text):
        _log("警告: send_text に必要な変数が未設定です。テキスト送信は失敗します。")
    if "LINE_PUBLIC_BASE_URL" in missing:
        _log("注意: LINE_PUBLIC_BASE_URL 未設定。send_image は使えません（テキストのみ可）。")
    base = _env("LINE_PUBLIC_BASE_URL")
    if base and not base.startswith("https://"):
        _log(f"警告: LINE_PUBLIC_BASE_URL は https:// 必須です（現在: {base!r}）。")
    used, lim = peek_quota()
    _log(f"月間送信の安全上限: {lim}通/月（LINE実上限 {LINE_FREE_TIER_LIMIT}通）。今月は現在 {used} 通。")


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    if "--stats" in sys.argv:
        used, lim = peek_quota()
        print(f"今月の送信: {used}/{lim} 通（LINE 実上限 {LINE_FREE_TIER_LIMIT}通/月）\n")
        print(format_stats(100))
        return
    if "--static-only" in sys.argv:
        # 静的サーバーだけを永続起動するモード（launchd 等で常駐させる用）
        start_static_server()
        _log("static-only モード。Ctrl-C で終了。")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
        return

    validate_config()
    started_own = start_static_server()
    if started_own:
        _log(
            "注意: 画像配信サーバーを MCP プロセス内で起動しました。"
            "Claude Code 終了直後に画像を送ると LINE の取得前にサーバーが落ちる場合があります。"
            "確実にするには README の launchd 常駐(--static-only)を推奨します。"
        )
    try:
        mcp.run()
    finally:
        # 画像送信直後に落ちると LINE の取得前に配信が止まるため、少しだけ待つ（best-effort）
        if started_own and _last_image_send:
            wait = GRACE_SECONDS - (time.time() - _last_image_send)
            if wait > 0:
                _log(f"直近の画像送信から {wait:.0f}s 待機します（LINE の取得猶予）。")
                time.sleep(wait)


if __name__ == "__main__":
    main()

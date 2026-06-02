"""LINE 認証情報なしで検証できる部分のスモークテスト。"""
import asyncio
import json
import os
import pathlib
import tempfile
import urllib.error
import urllib.request

os.environ.setdefault("LINE_PUBLIC_BASE_URL", "https://example.ts.net")

import server  # noqa: E402
from PIL import Image  # noqa: E402

# ログ・使用量は一時ファイルへ（本物の logs/ を汚さない）
_tmp = pathlib.Path(tempfile.mkdtemp())
server.LOG_FILE = _tmp / "sends.jsonl"
server.USAGE_FILE = _tmp / "usage.json"
# launchd常駐(8910)と衝突しないよう、テストは別ポートで自前サーバーを立てる
os.environ["LINE_STATIC_PORT"] = "8911"

ok = 0


def check(label, cond):
    global ok
    print(("  OK " if cond else "  FAIL ") + label)
    assert cond, label
    ok += 1


print("1) MCP ツール登録")
tools = asyncio.run(server.mcp.list_tools())
names = sorted(t.name for t in tools)
check(f"5ツール登録: {names}",
      names == ["send_image", "send_images", "send_stats", "send_text", "send_video"])

print("2) 画像処理: 透過PNG → original<=10MB / preview<=1MB")
tmp = server.BASE_DIR / "_test_input.png"
img = Image.new("RGBA", (3000, 2000))
px = img.load()
for y in range(2000):
    for x in range(0, 3000, 7):
        px[x, y] = ((x * 13) % 256, (y * 7) % 256, (x + y) % 256, 255)
img.save(tmp, "PNG")
orig = server.prepare_original(tmp, "a" * 32)
prev = server.prepare_preview(tmp, "a" * 32)
check(f"original {orig.stat().st_size//1024}KB <=10MB", orig.stat().st_size <= server.ORIGINAL_MAX_BYTES)
check(f"preview {prev.stat().st_size//1024}KB <=1MB", prev.stat().st_size <= server.PREVIEW_MAX_BYTES)
with Image.open(prev) as im:
    im.verify()
check("preview JPEG は正常に開ける", True)

print("3) 不透明パレット(GIF) は JPEG 経路（=.jpg）になる")
gif = server.BASE_DIR / "_test_input.gif"
Image.new("P", (50, 50)).save(gif, "GIF")
o = server.prepare_original(gif, "b" * 32)
check(f"不透明Pは .jpg 出力: {o.name}", o.suffix == ".jpg")

print("4) EXIF 回転が画素に焼き込まれる（縦横が入れ替わる）")
exif_jpg = server.BASE_DIR / "_test_exif.jpg"
base = Image.new("RGB", (100, 40), (123, 50, 200))
exif = base.getexif()
exif[274] = 6  # 90度回転
base.save(exif_jpg, "JPEG", exif=exif)
o2 = server.prepare_original(exif_jpg, "c" * 32)  # 回転ありなのでコピー高速路を回避→再エンコード
with Image.open(o2) as im2:
    rotated = im2.size == (40, 100)  # 100x40 が 40x100 になっていれば焼き込み済み
    no_exif = im2.getexif().get(274, 1) in (0, 1, None)
check(f"original が回転焼き込み済み size={im2.size}", rotated)
check("original から EXIF 回転タグが消えている", no_exif)
p2 = server.prepare_preview(exif_jpg, "c" * 32)
with Image.open(p2) as im3:
    check(f"preview も回転焼き込み済み size={im3.size}", im3.size[0] < im3.size[1])

print("5) テキスト分割: 35000文字は切り捨てず 7 メッセージ")
msgs = server._chunk_text("x" * 35000)
check(f"7 メッセージに分割（切り捨てなし）: {len(msgs)}", len(msgs) == 7)
check("合計文字数が保存される", sum(len(m["text"]) for m in msgs) == 35000)

print("6) 静的サーバー: token形式のみ配信 / 一覧・非tokenは404")
started = server.start_static_server()
port = server.static_port()


def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, None, b""


st, ct, body = get(f"/{prev.name}")
check(f"token preview GET 200 / image/jpeg: {st} {ct}", st == 200 and ct == "image/jpeg")
check("配信内容がファイルと一致", len(body) == prev.stat().st_size)
st_root, _, _ = get("/")
check(f"ディレクトリ一覧 / は 404: {st_root}", st_root == 404)
st_bad, _, _ = get("/secret.txt")
check(f"非token名は 404: {st_bad}", st_bad == 404)
st_trav, _, _ = get("/..%2f..%2fserver.py")
check(f"トラバーサルは 404: {st_trav}", st_trav == 404)

print("7) verify_served")
check("実在tokenファイルは配信確認OK", server.verify_served(prev.name))
check("非tokenは配信確認NG", not server.verify_served("nope.txt"))


def fetch_count():
    return server.LOG_FILE.read_text().count('"event": "fetch"') if server.LOG_FILE.exists() else 0


print("8) ログ/遅延計測")
before = fetch_count()
server.verify_served(orig.name)  # self-check GET（X-Self-Check付き）
check("self-checkはfetch記録しない", fetch_count() == before)
get(f"/{orig.name}")  # 実在ファイルへの通常GET = 外部取得相当
check("実在ファイルの通常GETはfetch記録する", fetch_count() == before + 1)
get("/" + "9" * 32 + ".jpg")  # token形式だが存在しない → 404
check("404(不在ファイル)はfetch記録しない", fetch_count() == before + 1)

tok = "e" * 32
prev_name = tok + "_preview.jpg"
server.log_event({"event": "send", "ts": "t", "epoch": 1000.0, "type": "image", "ok": True,
                  "token": tok, "original": tok + ".jpg", "preview": prev_name, "push_ms": 300})
server.log_event({"event": "fetch", "epoch": 1003.2, "ts": "t", "token": tok, "name": prev_name})
rows = server.compute_stats(50)
check("send×fetchをファイル名で突合し遅延3.2sを算出", any(r.get("fetch_delay_s") == 3.2 for r in rows))

# 失敗送信は遅延計上しない / 送信前fetch（負の遅延）は採用しない
tok2 = "1" * 32
server.log_event({"event": "send", "ts": "t", "epoch": 5000.0, "type": "image", "ok": False,
                  "token": tok2, "original": tok2 + ".jpg", "preview": tok2 + "_preview.jpg"})
server.log_event({"event": "fetch", "epoch": 5001.0, "ts": "t", "token": tok2, "name": tok2 + "_preview.jpg"})
tok3 = "2" * 32
server.log_event({"event": "send", "ts": "t", "epoch": 9000.0, "type": "image", "ok": True,
                  "token": tok3, "original": tok3 + ".jpg", "preview": tok3 + "_preview.jpg", "push_ms": 222})
server.log_event({"event": "fetch", "epoch": 8990.0, "ts": "t", "token": tok3, "name": tok3 + "_preview.jpg"})
rows = server.compute_stats(50)
check("失敗送信は遅延を計上しない", all(r.get("fetch_delay_s") is None for r in rows if r.get("ok") is False))
neg = [r for r in rows if r.get("push_ms") == 222]
check("負の遅延(クロックずれ)は採用しない", bool(neg) and neg[0].get("fetch_delay_s") is None)

server.log_event({"event": "send", "ts": "t", "epoch": 2000.0, "type": "text", "ok": True, "push_ms": 120})
out = server.format_stats(50)
check("format_stats が遅延サマリを含む", "平均初回取得遅延" in out and "3.2s" in out)
print("---- format_stats 出力例 ----")
print(out)
print("-----------------------------")

print("9) 月間クォータ: 予約・ロールバック・上限ブロック・月リセット")
server.USAGE_FILE.unlink(missing_ok=True)
os.environ["LINE_MONTHLY_LIMIT"] = "5"
u, lim = server.peek_quota()
check(f"初期 0/5 を返す: {u}/{lim}", u == 0 and lim == 5)
granted, used, lim = server.reserve_quota(3)
check(f"3通予約OK → 3/5: granted={granted} used={used}", granted and used == 3 and lim == 5)
granted2, used2, _ = server.reserve_quota(3)  # 3+3=6 > 5 → 拒否
check(f"追加3通は上限超過で拒否（3のまま）: granted={granted2} used={used2}",
      (not granted2) and used2 == 3)
granted3, used3, _ = server.reserve_quota(2)  # 3+2=5 = 上限ちょうど → OK
check(f"ちょうど上限まではOK → 5/5: granted={granted3} used={used3}", granted3 and used3 == 5)
server.release_quota(1)  # 5 → 4
u4, _ = server.peek_quota()
check(f"1通ロールバック → 4/5: {u4}", u4 == 4)
server.release_quota(99)  # 下限0でクランプ
u5, _ = server.peek_quota()
check(f"戻し過ぎは0でクランプ: {u5}", u5 == 0)
# 月替わりリセット: ファイルを過去の月に書き換える
server.USAGE_FILE.write_text(json.dumps({"month": "2000-01", "count": 99}))
u6, _ = server.peek_quota()
check(f"先月のカウントは0にリセット: {u6}", u6 == 0)
# 不正な LINE_MONTHLY_LIMIT は既定値にフォールバック
os.environ["LINE_MONTHLY_LIMIT"] = "abc"
check(f"不正な上限は既定 {server.DEFAULT_MONTHLY_LIMIT} に: {server.monthly_limit()}",
      server.monthly_limit() == server.DEFAULT_MONTHLY_LIMIT)
del os.environ["LINE_MONTHLY_LIMIT"]
check("既定の安全上限は実上限200未満", server.DEFAULT_MONTHLY_LIMIT < server.LINE_FREE_TIER_LIMIT)

print("10) 動画(send_video): GIF→mp4変換 / preview / 配信許可")
import shutil as _sh
if _sh.which("ffmpeg") and _sh.which("ffprobe"):
    anim = server.BASE_DIR / "_test_anim.gif"
    frames = [Image.new("RGB", (70, 50), (i * 80 % 256, 0, 0)) for i in range(4)]  # 16非倍数サイズ
    frames[0].save(anim, save_all=True, append_images=frames[1:], duration=120, loop=0)
    vtok = "d" * 32
    mp4 = server.prepare_video(anim, vtok)
    check(f"GIF→mp4 生成: {mp4.name} ({mp4.stat().st_size}B)",
          mp4.suffix == ".mp4" and mp4.stat().st_size > 0)
    dur = server.video_duration_seconds(mp4)
    check(f"mp4 の長さを取得できる: {dur}s", dur is not None and dur > 0)
    import subprocess as _sp
    a = _sp.run([server._ff_bin("ffprobe"), "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(mp4)],
                capture_output=True, text=True).stdout.strip()
    check(f"無音GIFでも音声トラック付与(再生互換): {a or 'なし'}", bool(a))
    vp = _sp.run([server._ff_bin("ffprobe"), "-v", "error", "-select_streams", "v",
                  "-show_entries", "stream=profile", "-of", "csv=p=0", str(mp4)],
                 capture_output=True, text=True).stdout.strip()
    check(f"H.264 baseline profile: {vp}", "baseline" in vp.lower())
    wh = _sp.run([server._ff_bin("ffprobe"), "-v", "error", "-select_streams", "v",
                  "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", str(mp4)],
                 capture_output=True, text=True).stdout.strip()
    w, h = (int(x) for x in wh.split("x"))
    check(f"解像度を16の倍数に整形(モバイル互換): 70x50→{wh}", w % 16 == 0 and h % 16 == 0)
    vprev = server.prepare_video_preview(anim, vtok)
    check(f"preview {vprev.stat().st_size // 1024}KB <=1MB / .jpg",
          vprev.suffix == ".jpg" and vprev.stat().st_size <= server.PREVIEW_MAX_BYTES)
    rx = server.TOKEN_FILE_RE
    check("配信許可: {token}.mp4 はOK", bool(rx.match("/" + "a" * 32 + ".mp4")))
    check("配信許可: {token}_preview.jpg はOK", bool(rx.match("/" + "a" * 32 + "_preview.jpg")))
    check("配信拒否: {token}_preview.mp4 はNG", not rx.match("/" + "a" * 32 + "_preview.mp4"))
    check("配信拒否: 非tokenの .mp4 はNG", not rx.match("/evil.mp4"))
    st_mp4, ct_mp4, _ = get(f"/{mp4.name}")
    check(f"mp4 を静的サーバーが video/mp4 で配信: {st_mp4} {ct_mp4}",
          st_mp4 == 200 and ct_mp4 == "video/mp4")
    import urllib.request as _ur
    req = _ur.Request(f"http://127.0.0.1:{port}/{mp4.name}", headers={"Range": "bytes=0-99"})
    with _ur.urlopen(req, timeout=3) as rr:
        rcode, body = rr.status, rr.read()
        crange, aranges = rr.headers.get("Content-Range"), rr.headers.get("Accept-Ranges")
    check(f"Range要求→206/100B/Accept-Ranges/Content-Range: {rcode} {len(body)}B CR={crange} AR={aranges}",
          rcode == 206 and len(body) == 100 and aranges == "bytes"
          and crange == f"bytes 0-99/{mp4.stat().st_size}")
    for p in (anim, mp4, vprev):
        p.unlink(missing_ok=True)
else:
    print("  SKIP: ffmpeg/ffprobe が無いため動画テストは省略")

# 後片付け
for p in (tmp, gif, exif_jpg, orig, prev, o, o2, p2):
    p.unlink(missing_ok=True)

print(f"\nOK: 全 {ok} チェック合格")

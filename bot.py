import os
import re
import glob
import json
import time
import shutil
import html as html_mod
import asyncio
import logging
import tempfile
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urljoin, urlparse

import requests
from yt_dlp import YoutubeDL
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dlbot")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", 10000))
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()}
MAX_BYTES = int(os.environ.get("MAX_MB", "1900")) * 1024 * 1024  # MTProto bot limit is 2GB
MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "720"))  # keeps files/time reasonable on Render

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
MEDIA_RE = re.compile(r"""https?://[^"'\s<>\\]+?\.(?:mp4|m3u8|webm|mkv)(?![\w.\-/])(?:\?[^"'\s<>\\]*)?""", re.I)
IFRAME_RE = re.compile(r"""<iframe[^>]+src=["']([^"']+)["']""", re.I)
URL_RE = re.compile(r"https?://\S+")


# ---------- finding & downloading ----------
SRC_RE = re.compile(r"""(?:<(?:video|source)[^>]+src|og:video(?::url|:secure_url)?["'][^>]+content|href|data-src|data-video|file|src)\s*[=:]\s*["']([^"']+?\.(?:mp4|m3u8|webm|mkv)(?![\w.\-/])(?:\?[^"']*)?)["']""", re.I)


def fetch(url, referer=None):
    h = {"User-Agent": UA, "Accept": "text/html,*/*", "Accept-Language": "en-US,en;q=0.9,fa;q=0.8"}
    if referer:
        h["Referer"] = referer
    r = requests.get(url, headers=h, timeout=25)
    r.raise_for_status()
    return html_mod.unescape(r.text.replace("\\/", "/"))


def find_media(page_url, notes):
    found = []
    try:
        html = fetch(page_url)
    except Exception as e:
        notes.append(f"باز کردن صفحه: {str(e)[:120]}")
        return found
    found += MEDIA_RE.findall(html)
    found += [urljoin(page_url, u) for u in SRC_RE.findall(html)]
    for src in IFRAME_RE.findall(html)[:5]:
        try:
            iframe_url = urljoin(page_url, src)
            ih = fetch(iframe_url, referer=page_url)
            found += MEDIA_RE.findall(ih)
            found += [urljoin(iframe_url, u) for u in SRC_RE.findall(ih)]
        except Exception:
            pass
    seen, out = set(), []
    for u in found:
        u = u.strip()
        if u not in seen:
            seen.add(u)
            out.append(u)
    host = urlparse(page_url).netloc
    return sorted(out, key=lambda x: (".m3u8" in x, urlparse(x).netloc == host))


def direct_download(url, referer, outdir):
    p = urlparse(referer)
    root = f"{p.scheme}://{p.netloc}/"
    base = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    variants = [
        {**base, "Referer": referer, "Origin": root.rstrip("/")},
        {**base, "Referer": root, "Range": "bytes=0-"},
        {**base, "Referer": referer, "Range": "bytes=0-"},
        {**base},
    ]
    path = os.path.join(outdir, "video.mp4")
    last = None
    for headers in variants:
        try:
            with requests.get(url, headers=headers, stream=True, timeout=30) as r:
                r.raise_for_status()
                if "text/html" in r.headers.get("content-type", ""):
                    raise RuntimeError("سرور به‌جای ویدیو صفحه HTML داد")
                total = int(r.headers.get("content-length") or 0)
                if total > MAX_BYTES:
                    raise RuntimeError("حجم فایل از حد مجاز بیشتره")
                written = 0
                with open(path, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        written += len(chunk)
                        if written > MAX_BYTES:
                            raise RuntimeError("حجم فایل از حد مجاز بیشتره")
            return path
        except Exception as e:
            last = e
    raise last


def ytdlp(url, outdir, referer=None):
    base = {
        "outtmpl": os.path.join(outdir, "%(id).50s.%(ext)s"),
        "max_filesize": MAX_BYTES,
        "noplaylist": True,
        "quiet": True,
        "merge_output_format": "mp4",
        "http_headers": {"User-Agent": UA, **({"Referer": referer} if referer else {})},
    }
    err = None
    for fmt in (f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]", "best"):
        try:
            with YoutubeDL({**base, "format": fmt}) as ydl:
                ydl.download([url])
            err = None
            break
        except Exception as e:
            err = e
    if err:
        raise err
    files = [f for f in glob.glob(os.path.join(outdir, "*")) if not f.endswith((".part", ".ytdl"))]
    return files[0] if files else None


def download(page_url, outdir):
    """returns (path or None, notes, candidates)"""
    notes = []
    cands = find_media(page_url, notes)
    log.info("candidates: %s", cands)
    for media in cands[:6]:
        short = media[:60]
        try:
            if ".m3u8" in media:
                f = ytdlp(media, outdir, referer=page_url)
            else:
                try:
                    f = direct_download(media, page_url, outdir)
                except Exception as e1:
                    notes.append(f"مستقیم: {str(e1)[:100]}")
                    f = ytdlp(media, outdir, referer=page_url)
            if f:
                return f, notes, cands
        except Exception as e:
            notes.append(f"{short}: {str(e)[:100]}")
    try:
        f = ytdlp(page_url, outdir)
        if f:
            return f, notes, cands
    except Exception as e:
        notes.append(f"yt-dlp: {str(e)[:100]}")
    return None, notes, cands


def probe(path, outdir):
    """duration/width/height + thumbnail via ffmpeg (best effort)."""
    dur = w = h = 0
    thumb = None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=60).stdout
        j = json.loads(out)
        w = j["streams"][0]["width"]
        h = j["streams"][0]["height"]
        dur = int(float(j["format"]["duration"]))
    except Exception:
        pass
    try:
        t = os.path.join(outdir, "thumb.jpg")
        subprocess.run(["ffmpeg", "-y", "-ss", str(min(3, max(dur // 2, 0))), "-i", path,
                        "-frames:v", "1", "-vf", "scale=320:-2", t],
                       capture_output=True, timeout=60)
        if os.path.exists(t):
            thumb = t
    except Exception:
        pass
    return dur, w, h, thumb


# ---------- bot ----------
client = TelegramClient(StringSession(), API_ID, API_HASH)


def allowed(event):
    return not ALLOWED or event.sender_id in ALLOWED


@client.on(events.NewMessage(incoming=True, pattern=r"^/start"))
async def start(event):
    if allowed(event):
        await event.reply("لینک صفحه ویدیو رو بفرست تا برات دانلود کنم 🎬")


@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private and not (e.raw_text or "").startswith("/")))
async def handle(event):
    if not allowed(event):
        return
    m = URL_RE.search(event.raw_text or "")
    if not m:
        await event.reply("یه لینک معتبر بفرست.")
        return
    status = await event.reply("⏳ در حال دانلود...")
    tmp = tempfile.mkdtemp()
    try:
        path, notes, cands = await asyncio.to_thread(download, m.group(0), tmp)
        if not path:
            # last resort: let Telegram's own servers fetch the URL (works if only our IP is blocked; ~20MB limit)
            for u in [c for c in cands if ".m3u8" not in c][:2]:
                try:
                    await status.edit("⏳ دانلود از سرور ما نشد، تلاش از طریق خود تلگرام...")
                    await client.send_file(event.chat_id, u, supports_streaming=True)
                    await status.delete()
                    return
                except Exception as e:
                    notes.append(f"تلگرام: {str(e)[:80]}")
            msg = "❌ دانلود نشد."
            if notes:
                msg += "\n\nدلیل:\n" + "\n".join(notes[-3:])
            if cands:
                msg += f"\n\n🔗 لینک پیدا‌شده:\n{cands[0]}"
            await status.edit(msg[:3500], link_preview=False)
            return
        size = os.path.getsize(path)
        if size > MAX_BYTES:
            await status.edit("❌ حجم فایل از حد مجاز (حدود ۲ گیگ) بیشتره.")
            return

        dur, w, h, thumb = await asyncio.to_thread(probe, path, tmp)
        last = [0.0]

        async def progress(sent, total):
            if time.time() - last[0] > 5:
                last[0] = time.time()
                try:
                    await status.edit(f"📤 در حال آپلود... {sent * 100 // total}٪")
                except Exception:
                    pass

        await client.send_file(
            event.chat_id, path,
            thumb=thumb,
            attributes=[DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)],
            supports_streaming=True,
            progress_callback=progress,
        )
        await status.delete()
    except Exception as e:
        log.exception("error")
        try:
            await status.edit(f"❌ خطا: {str(e)[:200]}")
        except Exception:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- health server (Render needs an open port) ----------
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_HEAD = do_GET

    def log_message(self, *a):
        pass


def main():
    srv = HTTPServer(("0.0.0.0", PORT), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    client.start(bot_token=TOKEN)
    log.info("bot started")
    client.run_until_disconnected()


if __name__ == "__main__":
    main()

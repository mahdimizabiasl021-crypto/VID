import os
import re
import glob
import json
import time
import shutil
import asyncio
import logging
import tempfile
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urljoin

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
MEDIA_RE = re.compile(r"""https?://[^"'\s<>\\]+?\.(?:mp4|m3u8|webm|mkv)(?:\?[^"'\s<>\\]*)?""", re.I)
IFRAME_RE = re.compile(r"""<iframe[^>]+src=["']([^"']+)["']""", re.I)
URL_RE = re.compile(r"https?://\S+")


# ---------- finding & downloading ----------
def fetch(url, referer=None):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    r = requests.get(url, headers=h, timeout=20)
    r.raise_for_status()
    return r.text.replace("\\/", "/")


def find_media(page_url):
    found = []
    try:
        html = fetch(page_url)
    except Exception as e:
        log.warning("fetch failed: %s", e)
        return found
    found += MEDIA_RE.findall(html)
    for src in IFRAME_RE.findall(html)[:5]:
        try:
            found += MEDIA_RE.findall(fetch(urljoin(page_url, src), referer=page_url))
        except Exception:
            pass
    seen, out = set(), []
    for u in sorted(found, key=lambda x: ".m3u8" not in x):
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def ytdlp(url, outdir, referer=None):
    base = {
        "outtmpl": os.path.join(outdir, "%(id).50s.%(ext)s"),
        "max_filesize": MAX_BYTES,
        "noplaylist": True,
        "quiet": True,
        "merge_output_format": "mp4",
        "http_headers": {"User-Agent": UA, **({"Referer": referer} if referer else {})},
    }
    for fmt in (f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]", "best"):
        try:
            with YoutubeDL({**base, "format": fmt}) as ydl:
                ydl.download([url])
            break
        except Exception as e:
            err = e
    else:
        raise err
    files = [f for f in glob.glob(os.path.join(outdir, "*")) if not f.endswith((".part", ".ytdl"))]
    return files[0] if files else None


def download(page_url, outdir):
    try:
        f = ytdlp(page_url, outdir)
        if f:
            return f
    except Exception as e:
        log.info("yt-dlp on page failed: %s", e)
    for media in find_media(page_url)[:6]:
        try:
            f = ytdlp(media, outdir, referer=page_url)
            if f:
                return f
        except Exception as e:
            log.info("candidate failed %s: %s", media, e)
    return None


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
        path = await asyncio.to_thread(download, m.group(0), tmp)
        if not path:
            await status.edit("❌ ویدیویی پیدا نشد یا حجمش از حد مجاز بیشتره.")
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

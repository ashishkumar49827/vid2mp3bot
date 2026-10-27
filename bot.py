"""Video to MP3 Telegram bot - coded runtime (no n8n).

Webhook mode (default; for free hosts such as Render) or long-poll mode
(--poll) for local runs. Raw Bot API over aiohttp; conversion via
ffmpeg/libmp3lame (real MP3 encode).

Delivery semantics (explicit, no stronger claim is made):
- At-least-once per update while the service is up. An in-memory per-update
  state machine (processing/done/failed) drives webhook answers:
  processing -> 503 (Telegram keeps retrying), done -> 200, failed ->
  re-run once. The conversion job runs as a tracked task that survives the
  webhook connection closing, so Telegram's ~60s response timeout cannot
  kill a long conversion.
- NOT durable across instance restarts: if the host kills the instance
  after sendAudio succeeds but before Telegram's final ack is consumed,
  one duplicate MP3 can be sent after restart. Jobs are not silently lost
  while Telegram keeps retrying (its retry window is far longer than any
  conversion), but a permanently down host means a permanently down bot.
- User-visible failures (oversize, no audio, unreadable file, busy) are
  final answers: the user is told and the update is marked done.
"""
import asyncio
import time, os, sys, tempfile, time
from aiohttp import web, ClientSession, FormData, ClientTimeout

from converter import (video_to_mp3, ConvertError,
                       MAX_DOWNLOAD_BYTES)

TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{TOKEN}"
FILE_API = f"https://api.telegram.org/file/bot{TOKEN}"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
PORT = int(os.environ.get("PORT", "8080"))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL", "2"))
ACK_DEADLINE = 45          # answer Telegram within this many seconds
JOB_TIMEOUT = 480          # hard cap for one conversion job
DOWNLOAD_TIMEOUT = ClientTimeout(total=180)
UPLOAD_TIMEOUT = ClientTimeout(total=180)

HELP = ("Send me a video (or a video file/document) and I will reply with "
        "its audio as an MP3. Max video size 20 MB.")

# update_id -> [state, ts]; state in {"processing", "done", "failed"}
_seen: dict[int, list] = {}
_jobs = asyncio.Semaphore(MAX_PARALLEL)


def _state(update_id: int):
    now = time.time()
    for k, v in list(_seen.items()):
        if now - v[1] > 24 * 3600:
            _seen.pop(k, None)
    entry = _seen.get(update_id)
    return entry[0] if entry else None


def _set(update_id: int, state: str):
    _seen[update_id] = [state, time.time()]


async def call(session, method, **kwargs):
    async with session.post(f"{API}/{method}", json=kwargs) as r:
        data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {data}")
    return data["result"]


async def reply(session, chat_id, text):
    await call(session, "sendMessage", chat_id=chat_id, text=text)


def _extract_file(message: dict):
    for key in ("video", "video_note"):
        if key in message:
            v = message[key]
            return v["file_id"], v.get("file_size", 0), f"{key}.mp4"
    doc = message.get("document")
    if doc and str(doc.get("mime_type", "")).startswith("video/"):
        return doc["file_id"], doc.get("file_size", 0), doc.get("file_name", "video")
    return None


class FinalAnswer(Exception):
    """Job concluded with a user-visible answer; mark update done."""


async def process(session, update: dict):
    """Full job for one update. Raises FinalAnswer when the user was given
    a conclusive reply (success or explained failure). Any other exception
    means the job did not conclude and may be retried."""
    msg = update.get("message") or update.get("channel_post") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    if not chat_id:
        raise FinalAnswer()
    if any(msg.get("text", "").startswith(c) for c in ("/start", "/help")):
        await reply(session, chat_id, HELP)
        raise FinalAnswer()
    found = _extract_file(msg)
    if not found:
        await reply(session, chat_id,
                    "Please send a video file (max 20 MB). " + HELP)
        raise FinalAnswer()
    file_id, fsize, fname = found
    print(f"update {update.get('update_id', -1)}: incoming video "
          f"name={fname} size={fsize}", flush=True)
    if fsize and fsize > MAX_DOWNLOAD_BYTES:
        await reply(session, chat_id,
                    "Sorry, this video is over 20 MB. Telegram bots cannot "
                    "download files larger than 20 MB, so I cannot convert it.")
        raise FinalAnswer()
    await reply(session, chat_id, "Converting to MP3, one moment...")
    if _jobs.locked():  # no queue: at most MAX_PARALLEL jobs, excess rejected
        await reply(session, chat_id,
                    "I am busy with another conversion right now. "
                    "Please try again in a minute.")
        raise FinalAnswer()
    await _jobs.acquire()
    t0 = time.monotonic()
    uid = update.get("update_id", -1)
    try:
        finfo = await call(session, "getFile", file_id=file_id)
        fpath = finfo["file_path"]
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "input")
            dst = os.path.join(td, "output.mp3")
            async with session.get(f"{FILE_API}/{fpath}",
                                   timeout=DOWNLOAD_TIMEOUT) as r:
                if r.status != 200:
                    raise ConvertError("could not download the video")
                with open(src, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 20):
                        f.write(chunk)
            t1 = time.monotonic()
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, video_to_mp3, src, dst),
                timeout=JOB_TIMEOUT)
            t2 = time.monotonic()
            title = os.path.splitext(os.path.basename(fname))[0] or "audio"
            form = FormData()
            form.add_field("chat_id", str(chat_id))
            form.add_field("title", title)
            form.add_field("caption", "Here is your MP3.")
            with open(dst, "rb") as f:
                form.add_field("audio", f.read(),
                               filename=title + ".mp3",
                               content_type="audio/mpeg")
            async with session.post(f"{API}/sendAudio", data=form,
                                    timeout=UPLOAD_TIMEOUT) as r:
                data = await r.json()
            if not data.get("ok"):
                raise ConvertError("Telegram rejected the MP3 upload")
            t3 = time.monotonic()
            print(f"update {uid}: timing download={t1-t0:.1f}s "
                  f"ffmpeg={t2-t1:.1f}s upload={t3-t2:.1f}s "
                  f"total={t3-t0:.1f}s", flush=True)
            a = (data.get("result") or {}).get("audio") or {}
            print(f"update {update.get('update_id', -1)}: sent MP3 "
                  f"file_id={a.get('file_id')} duration={a.get('duration')}s "
                  f"size={a.get('file_size')} mime={a.get('mime_type')}",
                  flush=True)
    except ConvertError as e:
        await reply(session, chat_id, f"Sorry, conversion failed: {e}")
        raise FinalAnswer()
    finally:
        _jobs.release()
    raise FinalAnswer()  # MP3 sent successfully


# ---------- webhook mode ----------
async def webhook(request: web.Request):
    if WEBHOOK_SECRET:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            return web.Response(status=403)
        if request.match_info.get("secret", "") != WEBHOOK_SECRET:
            return web.Response(status=404)
    try:
        update = await request.json()
    except Exception:
        return web.json_response({"ok": True})
    uid = update.get("update_id", -1)
    state = _state(uid)
    if state == "done":
        return web.json_response({"ok": True})
    if state == "processing":
        return web.Response(status=503)  # still working; Telegram retries
    _set(uid, "processing")
    session = request.app["session"]

    async def run():
        try:
            await process(session, update)
        except FinalAnswer:
            _set(uid, "done")
        except Exception as e:
            print(f"update {uid} error: {e}", file=sys.stderr, flush=True)
            _set(uid, "failed")
            try:
                chat_id = ((update.get("message") or {}).get("chat") or {}).get("id")
                if chat_id:
                    await reply(session, chat_id,
                                "Sorry, something went wrong while converting. "
                                "Please try again.")
            except Exception:
                pass

    task = asyncio.ensure_future(run())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=ACK_DEADLINE)
    except asyncio.TimeoutError:
        if _state(uid) == "processing":
            return web.Response(status=503)  # long job; Telegram will retry
    except asyncio.CancelledError:
        pass
    if _state(uid) == "failed":
        return web.Response(status=500)  # let Telegram retry -> re-run
    return web.json_response({"ok": True})


async def health(request):
    return web.json_response({"ok": True})


async def on_startup(app):
    app["session"] = ClientSession()


async def on_cleanup(app):
    await app["session"].close()


# ---------- long-poll mode (local testing) ----------
async def poll_loop():
    async with ClientSession() as session:
        offset = 0
        print("polling...", flush=True)
        while True:
            try:
                async with session.get(f"{API}/getUpdates",
                                       params={"offset": offset,
                                               "timeout": 30}) as r:
                    data = await r.json()
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    uid = upd.get("update_id", -1)
                    if _state(uid) is None:
                        _set(uid, "processing")
                        try:
                            await process(session, upd)
                        except FinalAnswer:
                            _set(uid, "done")
                        except Exception as e:
                            print(f"update {uid} error: {e}",
                                  file=sys.stderr, flush=True)
                            _set(uid, "failed")
            except Exception as e:
                print("poll error:", e, file=sys.stderr, flush=True)
                await asyncio.sleep(3)


if __name__ == "__main__":
    if "--poll" in sys.argv:
        asyncio.run(poll_loop())
    else:
        app = web.Application()
        app.on_startup.append(on_startup)
        app.on_cleanup.append(on_cleanup)
        app.router.add_get("/health", health)
        app.router.add_post(f"/webhook/{{secret}}", webhook)
        web.run_app(app, host="0.0.0.0", port=PORT)

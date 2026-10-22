"""You-Cev yerel sunucusu: python app.py → http://127.0.0.1:8765"""
import asyncio
import hashlib
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from config import CACHE_DIR, EXTENSION_DIR, WEB_DIR, cfg
from jobs import JobManager, to_srt
from pipeline import asr, downloader, local_models, tts
from pipeline.translator import google_breaker, resolve_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
for noisy in ("httpx", "httpx2", "huggingface_hub", "faster_whisper"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("youcev")

ALLOWED_ORIGIN = re.compile(r"^(chrome-extension://[a-z]{32}|http://(127\.0\.0\.1|localhost)(:\d+)?)$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}~[0-9a-f]{8}$")
SEG_RE = re.compile(r"^\d{4}_\d{3}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
UPLOAD_DIR = CACHE_DIR / "uploads"

manager = JobManager()


@asynccontextmanager
async def lifespan(app):
    manager.start()
    loop = asyncio.get_running_loop()
    # Whisper modelini arka planda yükle: ilk video beklemesin
    loop.run_in_executor(None, asr.get_model)
    # İnternet servisleri kısıtlarsa devreye girecek yerel çeviri/ses modellerini hazır tut
    loop.run_in_executor(None, local_models.ensure_downloaded)
    log.info("You-Cev hazır: http://%s:%s", cfg["server"]["host"], cfg["server"]["port"])
    yield
    await manager.stop()


app = FastAPI(title="You-Cev", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=ALLOWED_ORIGIN.pattern,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _job_or_404(key):
    job = manager.get(key) if KEY_RE.match(key) else None
    if not job:
        raise HTTPException(404, "İş bulunamadı")
    return job


class JobRequest(BaseModel):
    url: str


@app.post("/api/jobs")
async def create_job(req: JobRequest):
    vid = downloader.parse_video_id(req.url)
    if not vid:
        raise HTTPException(400, "Geçerli bir YouTube linki değil")
    job = await manager.open_youtube(vid)
    return job.summary()


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = Path(file.filename or "video")
    suffix = re.sub(r"[^a-z0-9.]", "", name.suffix.lower())[:8] or ".bin"
    tmp = UPLOAD_DIR / f"tmp_{uuid.uuid4().hex}{suffix}"
    h = hashlib.sha1()
    with open(tmp, "wb") as f:
        while chunk := await file.read(1 << 20):
            h.update(chunk)
            f.write(chunk)
    source_id = "f_" + h.hexdigest()[:16]
    dest = UPLOAD_DIR / f"{source_id}{suffix}"
    if dest.exists():
        tmp.unlink()
    else:
        tmp.replace(dest)
    job = await manager.open_file(source_id, dest, name.stem)
    return {**job.summary(), "media_url": f"/media/{source_id}"}


@app.get("/media/{source_id}")
async def media(source_id: str):
    if not re.fullmatch(r"f_[0-9a-f]{16}", source_id):
        raise HTTPException(404)
    found = list(UPLOAD_DIR.glob(f"{source_id}.*"))
    if not found:
        raise HTTPException(404)
    return FileResponse(found[0])


_video_locks: dict[str, asyncio.Lock] = {}


@app.post("/api/youtube/{video_id}/media")
async def youtube_media(video_id: str):
    """Gömülmesi kapalı videolar: yerel kopyayı indir (bir kez), web oynatıcı onu oynatsın."""
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(400, "Geçersiz video")
    async with _video_locks.setdefault(video_id, asyncio.Lock()):
        try:
            await asyncio.to_thread(downloader.download_video, video_id, CACHE_DIR / video_id)
        except Exception as e:
            raise HTTPException(502, str(e)[:300])
    return {"url": f"/media/yt/{video_id}"}


@app.get("/media/yt/{video_id}")
async def youtube_media_file(video_id: str):
    path = downloader.finished_file(CACHE_DIR / video_id, "video") if VIDEO_ID_RE.match(video_id) else None
    if not path:
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/api/jobs/{key}")
async def job_info(key: str):
    job = _job_or_404(key)
    return {**job.summary(), "progress": job.progress_msg()}


@app.get("/api/jobs/{key}/subtitles.srt")
async def subtitles(key: str, lang: str = "tr"):
    job = _job_or_404(key)
    if lang not in ("tr", "en"):
        raise HTTPException(400)
    return PlainTextResponse(
        to_srt(job.segments.values(), lang),
        headers={"Content-Disposition": f'attachment; filename="{job.source_id}.{lang}.srt"'},
    )


@app.get("/api/clips/{key}/{seg_id}.mp3")
async def clip(key: str, seg_id: str):
    if not KEY_RE.match(key) or not SEG_RE.match(seg_id):
        raise HTTPException(404)
    source_id, profile = key.split("~")
    path = CACHE_DIR / source_id / profile / "clips" / f"{seg_id}.mp3"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/settings")
async def get_settings():
    return config.public_settings()


@app.post("/api/settings")
async def set_settings(patch: dict):
    return config.update_settings(patch)


@app.get("/api/health")
async def health():
    down = [v for v in tts.TURKISH_VOICES if not tts.health.usable(v)]
    engine = await resolve_engine()
    if engine == "google" and not google_breaker.usable():
        engine = "local (Google kısıtlı)"
    return {"ok": True, "asr": asr.status(), "translator": engine, "jobs": len(manager.jobs),
            "voices_down": down, "local": local_models.status()}


@app.websocket("/ws/{key}")
async def ws_endpoint(websocket: WebSocket, key: str):
    origin = websocket.headers.get("origin", "")
    if origin and not ALLOWED_ORIGIN.match(origin):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    job = manager.get(key) if KEY_RE.match(key) else None
    if not job:
        await websocket.send_json({"type": "progress", "state": "error", "error": "İş bulunamadı", "ready": [],
                                   "done": False, "percent": 0, "duration": 0, "message": "İş bulunamadı"})
        await websocket.close()
        return
    queue = job.subscribe()

    async def sender():
        while True:
            await websocket.send_json(await queue.get())

    send_task = asyncio.create_task(sender())
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("type") == "position":
                manager.set_position(job, data.get("t", 0), viewer=id(queue))
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        send_task.cancel()
        job.unsubscribe(queue)


# Eklentideki oynatıcı ve altyazı stili web oynatıcıyla paylaşılır (tek kaynak)
@app.get("/sync-player.js")
async def sync_player():
    return FileResponse(EXTENSION_DIR / "sync-player.js", media_type="text/javascript")


@app.get("/overlay.css")
async def overlay_css():
    return FileResponse(EXTENSION_DIR / "overlay.css", media_type="text/css")


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


if __name__ == "__main__":
    uvicorn.run(app, host=cfg["server"]["host"], port=cfg["server"]["port"], log_level="warning")

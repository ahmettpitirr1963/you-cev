"""yt-dlp ile video bilgisi, elle yüklenmiş altyazı, ses ve (gerekirse) video indirme."""
import html
import json
import logging
import re
import shutil
from pathlib import Path

import httpx
import yt_dlp

log = logging.getLogger("youcev.download")

# YouTube çoğu istemci için indirmede PO token istiyor (403). Bu istemciler token'sız çalışıyor;
# ilk ikisi yalnızca ses verir (küçük dosya), sonrakiler sesli 360p video.
DOWNLOAD_CLIENTS = ["web_embedded", "tv_embedded", "mweb", "android", "tv_simply", "default"]

# Gömülmesi kapalı videolar için web oynatıcıda oynatılacak yerel kopya (sesli, tek dosya).
# "embedded" istemcileri bu videolarda çalışmadığı için sona alındı.
VIDEO_CLIENTS = ["mweb", "android", "tv_simply", "web_embedded", "tv_embedded", "default"]
VIDEO_FORMAT = "best[ext=mp4][acodec!=none][vcodec!=none][height<=720]/18/best[acodec!=none][vcodec!=none]"

_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/|live/|/v/)([A-Za-z0-9_-]{11})")
_BARE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_video_id(text: str) -> str | None:
    text = (text or "").strip()
    if _BARE_ID.match(text):
        return text
    m = _ID_RE.search(text)
    return m.group(1) if m else None


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _base_opts():
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "retries": 5,
            "socket_timeout": 20}
    # YouTube imza çözümü için JavaScript çalıştırıcısı (Node.js) gerekiyor
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
    return opts


def fetch_info(video_id: str) -> dict:
    with yt_dlp.YoutubeDL({**_base_opts(), "skip_download": True}) as ydl:
        return ydl.extract_info(watch_url(video_id), download=False)


def manual_english_subtitle(info: dict):
    """Elle yüklenmiş İngilizce altyazı (otomatik değil). Döner: (url, ext) veya None"""
    subs = info.get("subtitles") or {}
    langs = [k for k in subs if k == "en" or k.startswith("en-")]
    langs.sort(key=lambda k: (k != "en", k))
    for lang in langs:
        for ext in ("json3", "vtt"):
            for f in subs[lang]:
                if f.get("ext") == ext and f.get("url"):
                    return f["url"], ext
    return None


_NOISE = re.compile(r"\[[^\]]*\]|\([^)]*\)|♪[^♪]*♪|♪|<[^>]+>")
_SPEAKER = re.compile(r"^(?:-\s*)?[A-Z][A-Z .'\-]{1,30}:\s*")


def _clean_cue(text: str) -> str:
    text = html.unescape(text.replace("\n", " "))
    text = _NOISE.sub(" ", text)
    text = _SPEAKER.sub("", text.strip())
    text = re.sub(r"^\s*-\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_json3(data: dict):
    cues = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs or "tStartMs" not in ev:
            continue
        text = "".join(s.get("utf8", "") for s in segs)
        start = ev["tStartMs"] / 1000
        cues.append((start, start + ev.get("dDurationMs", 0) / 1000, text))
    return cues


_VTT_TIME = re.compile(r"(\d+:)?(\d{1,2}):(\d{2})[.,](\d{3})")


def _vtt_seconds(stamp: str) -> float:
    m = _VTT_TIME.match(stamp.strip())
    h = int(m.group(1)[:-1]) if m.group(1) else 0
    return h * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 1000


def _parse_vtt(text: str):
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = block.strip().split("\n")
        for i, line in enumerate(lines):
            if "-->" in line:
                a, b = line.split("-->")[:2]
                cues.append((_vtt_seconds(a), _vtt_seconds(b.split()[0]), "\n".join(lines[i + 1 :])))
                break
    return cues


def download_subtitle_words(url: str, ext: str) -> list[dict]:
    """Altyazıyı indirir, kelimelere böler; zamanları karakter uzunluğuna göre dağıtır."""
    r = httpx.get(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    cues = _parse_json3(r.json()) if ext == "json3" else _parse_vtt(r.text)
    words = []
    for start, end, raw in cues:
        text = _clean_cue(raw)
        if not text or end <= start:
            continue
        tokens = text.split()
        total = sum(len(t) + 1 for t in tokens)
        t = start
        for tok in tokens:
            d = (end - start) * (len(tok) + 1) / total
            words.append({"w": " " + tok, "s": round(t, 3), "e": round(t + d, 3)})
            t += d
    words.sort(key=lambda w: w["s"])
    return words


def finished_file(dest_dir: Path, stem: str) -> Path | None:
    found = [p for p in dest_dir.glob(f"{stem}.*") if p.suffix not in (".part", ".ytdl", ".tmp")]
    return found[0] if found else None


def _download(video_id: str, dest_dir: Path, stem: str, fmt: str, clients, what: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    if found := finished_file(dest_dir, stem):
        return found
    errors = []
    for client in clients:
        opts = {
            **_base_opts(),
            "format": fmt,
            "outtmpl": str(dest_dir / f"{stem}.%(ext)s"),
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
        }
        if client != "default":
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([watch_url(video_id)])
        except Exception as e:
            errors.append(f"{client}: {str(e).replace('ERROR: ', '')[:120]}")
            for p in dest_dir.glob(f"{stem}.*"):
                p.unlink(missing_ok=True)
            continue
        if found := finished_file(dest_dir, stem):
            log.info("%s indirildi (%s istemcisi): %s", what, client, found.name)
            return found
    raise RuntimeError(f"{what} indirilemedi. " + " | ".join(errors[-3:]))


def download_audio(video_id: str, dest_dir: Path) -> Path:
    return _download(video_id, dest_dir, "audio", "bestaudio/18/best", DOWNLOAD_CLIENTS, "Ses")


def download_video(video_id: str, dest_dir: Path) -> Path:
    return _download(video_id, dest_dir, "video", VIDEO_FORMAT, VIDEO_CLIENTS, "Video")


def save_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

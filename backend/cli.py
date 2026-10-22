"""Komut satırından test: python cli.py <youtube-linki | video-dosyası>
Videoyu baştan sona işler, hız raporu verir ve SRT dosyalarını yazar."""
import asyncio
import hashlib
import sys
import time
from pathlib import Path

from jobs import JobManager, fmt_time, to_srt
from pipeline import downloader


async def main(arg):
    manager = JobManager()
    manager.start()
    path = Path(arg)
    if path.is_file():
        source_id = "f_" + hashlib.sha1(path.read_bytes()).hexdigest()[:16]
        job = await manager.open_file(source_id, path.resolve(), path.stem)
    else:
        vid = downloader.parse_video_id(arg)
        if not vid:
            print("Geçerli bir YouTube linki veya dosya yolu verin.")
            return 1
        job = await manager.open_youtube(vid)

    queue = job.subscribe()
    t0 = time.monotonic()
    first_audio = None
    while job.state not in ("done", "error"):
        msg = await queue.get()
        if msg["type"] == "segments" and first_audio is None:
            first_audio = time.monotonic() - t0
        if msg["type"] == "warning":
            print("\n[uyarı]", msg["message"])
        if msg["type"] == "progress":
            print(f"\r%{msg['percent']:5.1f}  {msg['message'][:60]:<60}", end="", flush=True)
    print()
    if job.state == "error":
        print("HATA:", job.error)
        return 1

    elapsed = time.monotonic() - t0
    segs = job.sorted_segments()
    for lang in ("tr", "en"):
        out = job.profile_dir / f"{job.source_id}.{lang}.srt"
        out.write_text(to_srt(segs, lang), encoding="utf-8")
    silent = sum(1 for s in segs if not s["audio"])
    print(f"Başlık      : {job.title}")
    print(f"Süre        : {fmt_time(job.duration)}  |  işlem: {elapsed:.1f} sn  ({job.duration / max(elapsed, .1):.1f}x)")
    if first_audio is not None:
        print(f"İlk çeviri  : {first_audio:.1f} sn")
    print(f"Cümle       : {len(segs)}  (sessiz kalan: {silent}, hızlandırılan: {sum(1 for s in segs if s.get('rate', 0) > job.base_rate)})")
    print(f"Hazır bölge : {job.ready_ranges()}")
    print(f"SRT         : {job.profile_dir}")
    print("\nİlk cümleler:")
    for s in segs[:8]:
        print(f"  [{fmt_time(s['start'])}] {s['en']}\n           → {s['tr']}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    sys.exit(asyncio.run(main(sys.argv[1])))

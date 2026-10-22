"""İş kuyruğu: videoyu parçalara böler, izleyicilerin konumuna göre öncelik verir ve her parçayı
ASR → cümle birleştirme → çeviri → seslendirme adımlarından geçirip abonelere gönderir."""
import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from config import CACHE_DIR, cfg, profile_id
from pipeline import downloader, segmenter, tts
from pipeline.audio import ProgressiveAudio
from pipeline.downloader import load_json, save_json
from pipeline.sources import BOUNDARY_RADIUS, AsrSource, SubtitleSource
from pipeline.translator import get_translator, resolve_engine

log = logging.getLogger("youcev.jobs")

MAX_FAILURES = 3
CHUNK_TIMEOUT = 240     # sn
TTS_SOFT_DEADLINE = 10  # sn; parça, seslerin hepsini bu süreden fazla beklemez
IDLE_EVICT = 1800       # sn; izleyicisi olmayan iş bu kadar sonra bellekten atılır


def fmt_time(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def nominal_bounds(duration):
    p = cfg["pipeline"]
    bounds = [0.0]
    t = float(p["first_chunk_seconds"])
    while t < duration - 15:
        bounds.append(t)
        t += p["chunk_seconds"]
    bounds.append(round(float(duration), 2))
    return bounds


@dataclass
class Chunk:
    idx: int
    state: str = "pending"  # pending | running | done
    cover_start: float | None = None
    cover_end: float | None = None
    failures: int = 0
    gave_up: int = 0  # kaç kez "çevrilemedi" diye atlandı (sonra yeniden denenir)


class Job:
    def __init__(self, source_id, profile, engine, kind, ref, title=""):
        self.source_id = source_id
        self.profile = profile
        self.engine = engine
        self.key = f"{source_id}~{profile}"
        self.kind = kind  # youtube | file
        self.ref = ref    # video id veya dosya yolu
        self.title = title
        self.voice = cfg["tts"]["voice"]
        self.base_rate = cfg["tts"]["base_rate"]
        self.duration = 0.0
        self.state = "preparing"  # preparing | processing | done | error
        self.message = "Hazırlanıyor"
        self.error = None
        self.source = None
        self.prep_task = None
        self.nominal = []
        self.bounds = []
        self.refined = set()
        self.chunks: list[Chunk] = []
        self.segments: dict[str, dict] = {}
        self.tails: dict[int, list] = {}  # parça sonunda bekletilen yarım cümle (bir sonraki parçayla birleşir)
        self.subscribers: set[asyncio.Queue] = set()
        self.position = 0.0
        self.viewers: dict[int, float] = {}  # izleyici → son bildirdiği konum
        self.last_active = time.monotonic()
        self.created = time.monotonic()
        self.source_dir = CACHE_DIR / source_id
        self.profile_dir = self.source_dir / profile
        self.clips_dir = self.profile_dir / "clips"

    # ---- abonelik ----
    def subscribe(self):
        q = asyncio.Queue()
        self.subscribers.add(q)
        self.last_active = time.monotonic()
        q.put_nowait(self.meta_msg())
        if self.segments:
            q.put_nowait({"type": "segments", "items": self.sorted_segments()})
        q.put_nowait(self.progress_msg())
        return q

    def unsubscribe(self, q):
        self.subscribers.discard(q)
        self.viewers.pop(id(q), None)
        self.last_active = time.monotonic()

    def publish(self, msg):
        for q in list(self.subscribers):
            q.put_nowait(msg)

    def set_message(self, message):
        self.message = message
        self.publish(self.progress_msg())

    # ---- durum ----
    def summary(self):
        return {
            "key": self.key,
            "source_id": self.source_id,
            "kind": self.kind,
            "title": self.title,
            "duration": self.duration,
            "state": self.state,
            "engine": self.engine,
            "voice": self.voice,
        }

    def meta_msg(self):
        return {"type": "meta", **self.summary()}

    def sorted_segments(self):
        return sorted(self.segments.values(), key=lambda s: s["start"])

    def ready_ranges(self):
        spans = sorted(
            (c.cover_start, c.cover_end) for c in self.chunks if c.state == "done" and c.cover_start is not None
        )
        merged = []
        for a, b in spans:
            if merged and a <= merged[-1][1] + 0.05:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return [[round(a, 2), round(b, 2)] for a, b in merged]

    def progress_msg(self):
        ranges = self.ready_ranges()
        covered = sum(b - a for a, b in ranges)
        if self.state == "done":
            pct = 100.0
        else:
            pct = min(99.9, covered / self.duration * 100) if self.duration else 0.0
        return {
            "type": "progress",
            "state": self.state,
            "message": self.message,
            "error": self.error,
            "ready": ranges,
            "done": self.state == "done",
            "percent": round(pct, 1),
            "duration": self.duration,
        }

    # ---- parçalar ----
    def chunk_at(self, t):
        for i in range(len(self.chunks)):
            if t < self.bounds[i + 1]:
                return i
        return max(0, len(self.chunks) - 1)

    def _positions(self):
        return list(self.viewers.values()) or [self.position]

    def next_chunk(self, exclude=None):
        """En acil bekleyen parça: hangi izleyici bir sonraki çevrilmemiş parçaya en yakınsa onunki.
        İzleyicilerin önü tamamen hazırsa kalan parçalar baştan sırayla."""
        if not self.chunks:
            return None
        best, best_urgency = None, None
        for t in self._positions():
            for i in range(self.chunk_at(t), len(self.chunks)):
                c = self.chunks[i]
                if c.state == "pending" and i != exclude:
                    urgency = self.bounds[i] - t  # izleyicinin bu parçaya varmasına kalan süre
                    if best is None or urgency < best_urgency:
                        best, best_urgency = c, urgency
                    break
        if best:
            return best
        return next((c for c in self.chunks if c.state == "pending" and c.idx != exclude), None)

    def needed_next(self, idx):
        """Bir izleyicinin hemen ihtiyaç duyacağı (bulunduğu ya da sıradaki) parça mı?"""
        for t in self._positions():
            k = self.chunk_at(t)
            first = next((i for i in range(k, len(self.chunks)) if self.chunks[i].state != "done"), None)
            if first == idx and idx in (k, k + 1):
                return True
        return False

    async def chunk_range(self, k):
        """Parça sınırlarını (gerekirse ses gelince) en sessiz noktaya kaydırıp döner."""
        last = len(self.bounds) - 1
        for b in (k, k + 1):
            if b in self.refined or b == 0 or b == last:
                continue
            nominal = self.nominal[b]
            await self.source.wait_until(min(self.duration, nominal + BOUNDARY_RADIUS))
            if b not in self.refined:
                self.bounds[b] = self.source.refine(nominal)
                self.refined.add(b)
                save_json(
                    self.source_dir / "bounds.json",
                    {"nominal": self.nominal, "refined": {str(i): self.bounds[i] for i in self.refined}},
                )
        return self.bounds[k], self.bounds[k + 1]

    def load_bounds(self):
        self.nominal = nominal_bounds(self.duration)
        self.bounds = list(self.nominal)
        saved = load_json(self.source_dir / "bounds.json")
        if saved and saved.get("nominal") == self.nominal:
            for k, v in saved.get("refined", {}).items():
                self.bounds[int(k)] = v
                self.refined.add(int(k))
        self.chunks = [Chunk(i) for i in range(len(self.nominal) - 1)]

    def save_state(self):
        save_json(
            self.profile_dir / "state.json",
            {
                "nominal": self.nominal,
                "chunks": [asdict(c) for c in self.chunks],
                "tails": {str(k): v for k, v in self.tails.items()},
                "segments": self.sorted_segments(),
            },
        )

    def load_state(self):
        data = load_json(self.profile_dir / "state.json")
        if not data or data.get("nominal") != self.nominal or len(data.get("chunks", [])) != len(self.chunks):
            return
        segments = {s["id"]: s for s in data.get("segments", [])}
        for row in data["chunks"]:
            c = Chunk(**{k: v for k, v in row.items() if k in Chunk.__dataclass_fields__})
            prefix = f"{c.idx:04d}_"
            if c.state == "done" and (c.gave_up or c.failures >= MAX_FAILURES):
                c.state, c.gave_up = "pending", 0  # önceki oturumda atlanmıştı: yeniden dene
            if c.state == "done":
                mine = [s for sid, s in segments.items() if sid.startswith(prefix)]
                # Sesi eksik kalan (ör. sunucu kapanırken geciken) cümle varsa parçayı yeniden işle
                if any(not s["audio"] or not (self.clips_dir / f"{s['id']}.mp3").exists() for s in mine):
                    c.state = "pending"
            if c.state != "done":
                c.state, c.failures, c.cover_start, c.cover_end = "pending", 0, None, None
                segments = {sid: s for sid, s in segments.items() if not sid.startswith(prefix)}
            self.chunks[c.idx] = c
        self.segments = segments
        self.tails = {int(k): v for k, v in data.get("tails", {}).items() if self.chunks[int(k)].state == "done"}

    def all_done(self):
        return all(c.state == "done" for c in self.chunks)


class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self._wake = None
        self._task = None
        self._current = None  # (job, chunk, task): işlenmekte olan parça
        self._prefetch: dict[tuple, asyncio.Task] = {}

    def start(self):
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
        for t in self._prefetch.values():
            t.cancel()

    def get(self, key):
        return self.jobs.get(key)

    def wake(self):
        if self._wake:
            self._wake.set()

    def set_position(self, job, t, viewer=None):
        t = max(0.0, float(t))
        jumped = True
        if viewer is not None:
            prev = job.viewers.get(viewer)
            # Konum ~1 sn'de bir gelir; 5 sn'den büyük sıçrama = izleyici sardı
            jumped = prev is None or abs(t - prev) > 5
            job.viewers[viewer] = t
        job.position = t
        job.last_active = time.monotonic()
        if jumped:
            self._maybe_preempt(job)
        self.wake()

    def _maybe_preempt(self, job):
        """İzleyici çevrilmemiş bir yere sardıysa, o an işlenen uzak parçayı bırak ve hemen
        izleyicinin olduğu yere geç (yoksa önce mevcut parçanın bitmesi beklenirdi).
        İşlenen parça herhangi bir izleyiciye hemen lazımsa asla bırakılmaz (birden çok
        izleyici varken gidip gelmeyi önler)."""
        cur = self._current
        if not cur or cur[0] is not job or not job.chunks:
            return
        running = cur[1].idx
        if job.needed_next(running):
            return
        target = job.next_chunk(exclude=running)
        if target is not None and job.needed_next(target.idx):
            cur[2].cancel()

    # ---- iş açma ----
    async def open_youtube(self, video_id):
        return await self._open(video_id, "youtube", video_id, "")

    async def open_file(self, source_id, path, title):
        return await self._open(source_id, "file", str(path), title)

    async def _open(self, source_id, kind, ref, title):
        engine = await resolve_engine()
        key = f"{source_id}~{profile_id(engine)}"
        job = self.jobs.get(key)
        if job and job.state != "error":
            job.last_active = time.monotonic()
            return job
        job = Job(source_id, profile_id(engine), engine, kind, ref, title)
        self.jobs[key] = job
        job.prep_task = asyncio.create_task(self._prepare(job))
        return job

    async def _prepare(self, job):
        try:
            job.clips_dir.mkdir(parents=True, exist_ok=True)
            shared = next(
                (j for j in self.jobs.values()
                 if j is not job and j.source_id == job.source_id and j.source is not None and j.duration),
                None,
            )
            if shared:
                job.source, job.duration, job.title = shared.source, shared.duration, shared.title
                job.load_bounds()
                job.load_state()
            elif job.kind == "youtube":
                await self._prepare_youtube(job)
            else:
                await self._prepare_file(job)
            job.state = "done" if job.all_done() else "processing"
            job.message = "Tamamlandı" if job.state == "done" else "Çeviri başlıyor"
            job.publish(job.meta_msg())
            if job.segments:
                job.publish({"type": "segments", "items": job.sorted_segments()})
            job.publish(job.progress_msg())
            log.info("Hazır: %s '%s' (%s, %d parça, kaynak: %s, çeviri: %s)", job.key, job.title,
                     fmt_time(job.duration), len(job.chunks), job.source.kind if job.source else "önbellek",
                     job.engine)
            self.wake()
        except Exception as e:
            log.exception("Hazırlık başarısız: %s", job.key)
            job.state = "error"
            job.error = str(e).replace("ERROR: ", "")[:300] or type(e).__name__
            job.message = job.error
            job.publish(job.progress_msg())

    async def _prepare_youtube(self, job):
        meta_path = job.source_dir / "source.json"
        subs_path = job.source_dir / "subs_v2.json"
        meta = load_json(meta_path)
        if not meta:
            job.set_message("Video bilgisi alınıyor")
            info = await asyncio.to_thread(downloader.fetch_info, job.ref)
            if info.get("is_live"):
                raise RuntimeError("Canlı yayınlar desteklenmiyor")
            meta = {"title": info.get("title") or job.ref, "duration": float(info.get("duration") or 0)}
            sub = downloader.manual_english_subtitle(info) if cfg["pipeline"]["use_manual_subtitles"] else None
            if sub:
                try:
                    words = await asyncio.to_thread(downloader.download_subtitle_words, *sub)
                    if len(words) > 20:
                        save_json(subs_path, words)
                        meta["subs"] = True
                except Exception as e:
                    log.warning("Altyazı indirilemedi, ASR kullanılacak: %s", e)
            save_json(meta_path, meta)
        job.title, job.duration = meta["title"], meta["duration"]
        job.publish(job.meta_msg())
        if job.duration:
            job.load_bounds()
            job.load_state()
            if job.all_done():
                return  # tamamı önbellekte: ses indirmeye gerek yok

        if meta.get("subs") and cfg["pipeline"]["use_manual_subtitles"] and subs_path.exists():
            job.source = SubtitleSource(load_json(subs_path))
            return
        job.set_message("Ses indiriliyor")
        path = await asyncio.to_thread(downloader.download_audio, job.ref, job.source_dir)
        audio = ProgressiveAudio(path, job.duration or None)
        if not job.duration:
            await asyncio.to_thread(audio.wait_for, float("inf"))
            job.duration = round(audio.seconds, 2)
            job.load_bounds()
            job.load_state()
        job.source = AsrSource(audio, job.source_dir)

    async def _prepare_file(self, job):
        path = Path(job.ref)
        duration = await asyncio.to_thread(ProgressiveAudio.probe_duration, path)
        if duration:
            job.duration = round(duration, 2)
            job.load_bounds()
            job.load_state()
            if job.all_done():
                return
        audio = ProgressiveAudio(path, duration)
        await asyncio.to_thread(audio.wait_for, 1.0)
        if audio.done and audio.seconds == 0:
            raise RuntimeError(f"Dosyada ses bulunamadı ({audio.error})")
        if not duration:
            job.set_message("Ses çözülüyor")
            await asyncio.to_thread(audio.wait_for, float("inf"))
            job.duration = round(audio.seconds, 2)
            job.load_bounds()
            job.load_state()
        job.source = AsrSource(audio, job.source_dir)

    # ---- işçi ----
    async def _run(self):
        last_evict = time.monotonic()
        while True:
            try:
                if time.monotonic() - last_evict > 60:
                    self._evict()
                    last_evict = time.monotonic()
                picked = self._pick()
                if picked is None:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), 5)
                    except asyncio.TimeoutError:
                        pass
                    continue
                job, chunk = picked
                task = asyncio.create_task(self._process_safe(job, chunk))
                self._current = (job, chunk, task)
                try:
                    await asyncio.wait({task})
                finally:
                    self._current = None
                if task.cancelled():
                    log.info("Parça %d bırakıldı: izleyici başka yere sardı", chunk.idx + 1)
            except asyncio.CancelledError:
                if self._current:
                    self._current[2].cancel()
                raise
            except Exception:
                log.exception("İşçi hatası")
                await asyncio.sleep(1)

    def _pick(self):
        now = time.monotonic()
        candidates = [j for j in self.jobs.values() if j.state == "processing" and j.source is not None]
        active = [j for j in candidates if j.subscribers]
        pool = active or [j for j in candidates if now - j.last_active < IDLE_EVICT]
        for job in sorted(pool, key=lambda j: j.last_active, reverse=True):
            chunk = job.next_chunk()
            if chunk:
                return job, chunk
        return None

    def _evict(self):
        now = time.monotonic()
        for key, job in list(self.jobs.items()):
            if job.subscribers or now - job.last_active < IDLE_EVICT:
                continue
            if job.prep_task and not job.prep_task.done():
                continue
            del self.jobs[key]
            if not any(j.source_id == job.source_id for j in self.jobs.values()):
                for pk in [p for p in self._prefetch if p[0] == job.source_id]:
                    self._prefetch.pop(pk).cancel()
            log.info("Bellekten atıldı: %s", key)

    async def _process_safe(self, job, chunk):
        chunk.state = "running"
        try:
            # Güvenlik ağı: hiçbir dış servis takılması akışı sonsuza kadar durduramasın
            async with asyncio.timeout(CHUNK_TIMEOUT):
                await self._process(job, chunk)
        except asyncio.CancelledError:
            chunk.state = "pending"  # öne alma: sonra kaldığı yerden (ASR önbellekte) tekrar işlenir
            raise
        except Exception as e:
            chunk.failures += 1
            log.exception("Parça %d başarısız (%s), deneme %d", chunk.idx + 1, job.key, chunk.failures)
            if chunk.failures < MAX_FAILURES:
                chunk.state = "pending"
                await asyncio.sleep(3 * chunk.failures)  # servise yüklenmeden, aralıklı tekrar
                return
            # Akış durmasın: bu parça şimdilik çevirisiz geçilir, sonra yeniden denenir
            start, end = job.bounds[chunk.idx], job.bounds[chunk.idx + 1]
            chunk.state, chunk.cover_start, chunk.cover_end = "done", start, end
            chunk.gave_up += 1
            job.publish({"type": "warning", "message": f"{fmt_time(start)}-{fmt_time(end)} arası çevrilemedi: {e}"})
            self._finish_check(job)
            job.save_state()
            job.publish(job.progress_msg())
            # Artan aralıklarla yeniden dene (ör. internet servisleri kısıtlıyken yerel model inince düzelir)
            if chunk.gave_up < 12:
                asyncio.get_running_loop().call_later(min(300, 45 * chunk.gave_up), self._retry_chunk, job, chunk)

    def _retry_chunk(self, job, chunk):
        if self.jobs.get(job.key) is not job or chunk.state != "done" or not chunk.gave_up:
            return
        log.info("Atlanan parça %d yeniden deneniyor", chunk.idx + 1)
        chunk.state, chunk.failures, chunk.cover_start, chunk.cover_end = "pending", 0, None, None
        if job.state == "done":
            job.state = "processing"
        if job.source is None:
            # Kaynak bırakılmışsa (iş bitti sanılmıştı) yeniden hazırla
            job.prep_task = asyncio.create_task(self._prepare(job))
        job.publish(job.progress_msg())
        self.wake()

    def _prompt(self, job, k):
        if k == 0:
            return None
        prev = job.source.cached_words(job.bounds[k - 1], job.bounds[k])
        return segmenter.join_words(prev[-40:]) if prev else None

    async def _words(self, job, k, start, end):
        task = self._prefetch.pop((job.source_id, k), None)
        if task:
            try:
                return await task
            except Exception as e:
                log.warning("Ön transkripsiyon başarısız, tekrar deneniyor: %s", e)
        return await job.source.words(start, end, self._prompt(job, k))

    def _prefetch_words(self, job, k):
        """Bir sonraki parçanın ASR'sini, mevcut parçanın çeviri/seslendirmesiyle paralel başlatır."""
        key = (job.source_id, k)
        if key in self._prefetch or job.source.kind != "asr":
            return

        async def run():
            start, end = await job.chunk_range(k)
            return await job.source.words(start, end, self._prompt(job, k))

        task = asyncio.create_task(run())
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        self._prefetch[key] = task

    async def _process(self, job, chunk):
        k = chunk.idx
        t0 = time.monotonic()
        start, end = await job.chunk_range(k)
        job.set_message(f"Çevriliyor: {fmt_time(start)}")
        words = await self._words(job, k, start, end)
        nxt = job.next_chunk(exclude=k)
        if nxt is not None:
            self._prefetch_words(job, nxt.idx)

        # Önceki parçanın sonunda bekletilen yarım cümleyi bu parçanın başına ekle
        cover_start = start
        prev = job.chunks[k - 1] if k > 0 else None
        prev_tail = job.tails.get(k - 1) if prev and prev.state == "done" else None
        if prev_tail:
            words = prev_tail + words
            cover_start = prev_tail[0]["s"]

        sentences = segmenter.segment(words)
        cover_end = end
        nextc = job.chunks[k + 1] if k + 1 < len(job.chunks) else None
        held = None
        if sentences and nextc and nextc.state != "done" and not segmenter.is_complete(sentences[-1]):
            held = sentences.pop()
            cover_end = max(cover_start, held["start"])

        segs = await self._translate_and_speak(job, k, sentences, held)

        for s in segs:
            job.segments[s["id"]] = s
        if prev_tail:
            job.tails.pop(k - 1, None)
            prev.cover_end = job.bounds[k]
        if held:
            job.tails[k] = held["words"]
        chunk.cover_start, chunk.cover_end = cover_start, cover_end
        chunk.state = "done"
        self._finish_check(job)
        job.save_state()
        if segs:
            job.publish({"type": "segments", "items": segs})
        job.publish(job.progress_msg())
        log.info("Parça %d/%d hazır (%s-%s, %d cümle, %.1f sn)", k + 1, len(job.chunks),
                 fmt_time(start), fmt_time(end), len(segs), time.monotonic() - t0)

    def _finish_check(self, job):
        if job.all_done() and job.state == "processing":
            job.state = "done"
            job.message = "Tamamlandı"
            elapsed = time.monotonic() - job.created
            log.info("Tamamlandı: %s — %s video, %.0f sn'de (%.1fx)", job.key, fmt_time(job.duration),
                     elapsed, job.duration / max(elapsed, 0.1))
            job.source = None  # sesi bellekten bırak

    async def _translate_and_speak(self, job, k, sentences, held):
        if not sentences:
            return []
        n_ctx = cfg["translate"]["context_sentences"]
        first = sentences[0]["start"]
        before = [s for s in job.sorted_segments() if s["start"] < first]
        context = [s["en"] for s in before[-n_ctx:]] if n_ctx else []
        translator = get_translator(job.engine)
        items = [{"en": s["text"], "dur": s["end"] - s["start"]} for s in sentences]
        trs = await translator.translate(items, context, job.title)

        later = [s["start"] for s in job.segments.values() if s["start"] > sentences[-1]["start"]]
        specs = []
        for i, (s, tr) in enumerate(zip(sentences, trs)):
            if i + 1 < len(sentences):
                nxt = sentences[i + 1]["start"]
            elif held:
                nxt = held["start"]
            elif later:
                nxt = min(later)
            else:
                nxt = s["end"] + 1.0
            # Klip, bir sonraki cümle başlayana kadarki süreye sığmalı (aradaki sessizlik dahil)
            slot = max(nxt - s["start"], s["end"] - s["start"], 0.6)
            specs.append((f"{k:04d}_{i:03d}", s, tr, slot))
        tasks = [asyncio.create_task(self._speak_retry(job, *spec, translator)) for spec in specs]
        # Tek bir yavaş seslendirme isteği parçayı bekletmesin: süre dolunca parça yayınlanır,
        # geciken sesler hazır olunca ayrıca gönderilir.
        _, pending = await asyncio.wait(tasks, timeout=TTS_SOFT_DEADLINE)
        segs = []
        for spec, task in zip(specs, tasks):
            if task in pending:
                segs.append(self._seg_base(*spec[:3]))
                task.add_done_callback(lambda t, job=job: self._late_segment(job, t))
            else:
                segs.append(task.result())
        if pending:
            log.info("Parça %d: %d ses gecikti, arka planda tamamlanacak", k + 1, len(pending))
        return segs

    def _late_segment(self, job, task):
        if task.cancelled() or task.exception():
            return
        seg = task.result()
        job.segments[seg["id"]] = seg
        job.save_state()
        job.publish({"type": "segments", "items": [seg]})

    async def _speak_retry(self, job, seg_id, s, tr, slot, translator):
        seg = await self._speak(job, seg_id, s, tr, slot, translator)
        if not seg["audio"]:
            await asyncio.sleep(2)  # servis yoğunsa biraz bekleyip bir kez daha dene
            seg = await self._speak(job, seg_id, s, tr, slot, translator)
        return seg

    @staticmethod
    def _seg_base(seg_id, s, tr):
        return {
            "id": seg_id,
            "start": round(s["start"], 3),
            "end": round(s["end"], 3),
            "en": s["text"],
            "tr": (tr or "").strip() or s["text"],
            "dur": 0.0,
            "audio": False,
        }

    async def _speak(self, job, seg_id, s, tr, slot, translator):
        seg = self._seg_base(seg_id, s, tr)
        tr = seg["tr"]
        shorten = None
        if translator.can_shorten:
            def shorten(seconds):
                return translator.shorten(s["text"], tr, seconds)
        try:
            audio, lead, speech, rate, spoken = await tts.synth_fit(
                tr, job.voice, job.base_rate, cfg["tts"]["max_rate"], slot, shorten
            )
            path = job.clips_dir / f"{seg_id}.mp3"
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(audio)
            tmp.replace(path)
            # dur: gerçek konuşma süresi; lead: oynatıcının atlayacağı baştaki sessizlik
            seg.update(dur=speech, lead=lead, audio=True, rate=rate)
            if spoken != tr:
                seg["spoken"] = spoken
        except Exception as e:
            log.warning("Seslendirme başarısız (%s): %s", seg_id, e)
        return seg


def to_srt(segments, lang="tr"):
    def stamp(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    out = []
    for i, s in enumerate(sorted(segments, key=lambda x: x["start"]), 1):
        out.append(f"{i}\n{stamp(s['start'])} --> {stamp(max(s['end'], s['start'] + 1))}\n{s[lang]}\n")
    return "\n".join(out)

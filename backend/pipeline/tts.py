"""Türkçe seslendirme (edge-tts nöral sesler, yedek: yerel Piper) ve cümle süresine sığdırma."""
import asyncio
import io
import logging
import math
import re
import time

import av
import edge_tts
import numpy as np

from config import cfg
from pipeline import local_models

log = logging.getLogger("youcev.tts")

# Ölçüm: Türkçe nöral sesler %0 hızda saniyede ~17.5 karakter konuşur (sessizlikler hariç)
CHARS_PER_SECOND = 17.0
SILENCE_LEVEL = 0.01

TURKISH_VOICES = ["tr-TR-AhmetNeural", "tr-TR-EmelNeural"]
VOICE_DOWN_AFTER = 3      # art arda bu kadar hata → ses "arızalı" sayılır
VOICE_DOWN_SECONDS = 300  # arızalı ses bu süre denenmez, yedek ses kullanılır


def clean(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]|♪", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def analyze(audio: bytes):
    """mp3'ün başındaki sessizlik ve gerçek konuşma süresi. edge-tts başa ~0.4 sn, sona ~1.1 sn
    sessizlik ekler; oynatıcı baştakini atlar, sondakini yok sayar."""
    with av.open(io.BytesIO(audio)) as c:
        sr = c.streams.audio[0].rate
        samples = np.concatenate([f.to_ndarray().reshape(-1) for f in c.decode(audio=0)])
    loud = np.flatnonzero(np.abs(samples) > SILENCE_LEVEL)
    if len(loud) == 0:
        return 0.0, len(samples) / sr
    lead = max(0.0, loud[0] / sr - 0.03)
    speech = (loud[-1] - loud[0]) / sr + 0.06
    return round(lead, 3), round(speech, 3)


_sem = None


def _semaphore():
    # Ölçüm: 4 eşzamanlı istek ~1.4 cümle/sn verir; 8'de servis isteklerin çoğunu reddediyor
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(cfg["tts"]["concurrency"])
    return _sem


class _VoiceHealth:
    """Microsoft zaman zaman tek bir sesi günlerce bozuk bırakabiliyor (diğerleri çalışırken).
    Bozuk sese istek atıp vakit kaybetmemek için basit bir devre kesici."""

    def __init__(self):
        self.fails = {}
        self.down_until = {}

    def usable(self, voice):
        return time.monotonic() >= self.down_until.get(voice, 0)

    def ok(self, voice):
        if self.fails.get(voice) or voice in self.down_until:
            log.info("Ses yeniden çalışıyor: %s", voice)
        self.fails[voice] = 0
        self.down_until.pop(voice, None)

    def failed(self, voice):
        self.fails[voice] = self.fails.get(voice, 0) + 1
        if self.fails[voice] >= VOICE_DOWN_AFTER and self.usable(voice):
            self.down_until[voice] = time.monotonic() + VOICE_DOWN_SECONDS
            log.warning("%s sesi şu an çalışmıyor; %d dk boyunca yedek ses kullanılacak",
                        voice, VOICE_DOWN_SECONDS // 60)


health = _VoiceHealth()


async def _synth_once(text: str, voice: str, rate: int) -> bytes:
    buf = bytearray()
    async with _semaphore():  # yalnızca istek sırasında; beklerken kota tutulmaz
        comm = edge_tts.Communicate(text, voice, rate=f"{rate:+d}%")
        # Servis bazen yanıt vermeden asılı kalıyor: zaman aşımı olmazsa tüm akış durur
        async with asyncio.timeout(10 + len(text) / 20):
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
    if not buf:
        raise RuntimeError("boş ses döndü")
    return bytes(buf)


def _voice_order(voice):
    """Seçilen ses önce; arızalıysa sona. Ardından yedek Türkçe ses."""
    voices = [voice] + [v for v in TURKISH_VOICES if v != voice]
    return sorted(voices, key=lambda v: not health.usable(v))


async def synth(text: str, voice: str, rate: int) -> bytes:
    """Önce Microsoft nöral sesleri (seçilen, sonra diğer Türkçe ses); ikisi de çalışmıyorsa
    beklemeden yerel Piper sesi. Böylece hiçbir cümle sessiz kalmaz."""
    if cfg["tts"].get("engine", "auto") != "local" and any(health.usable(v) for v in TURKISH_VOICES):
        for attempt in range(2):
            for v in _voice_order(voice):
                if not health.usable(v) and any(health.usable(x) for x in TURKISH_VOICES):
                    continue  # çalışan bir ses varken arızalıyı deneme
                try:
                    audio = await _synth_once(text, v, rate)
                    health.ok(v)
                    return audio
                except Exception as e:
                    health.failed(v)
                    log.debug("edge-tts hatası (%s): %s", v, e)
            if not any(health.usable(v) for v in TURKISH_VOICES):
                break
            await asyncio.sleep(0.8 * (attempt + 1))
    return await asyncio.to_thread(local_models.synth_local, text, rate)


def _rate_for(speech_at_zero: float, slot: float, base_rate: int, max_rate: int) -> int:
    # edge-tts'te +%x hız, konuşmayı yaklaşık (1 + x/100) kat hızlandırır
    needed = speech_at_zero / max(slot * 0.95, 0.3)
    return int(min(max_rate, max(base_rate, math.ceil((needed - 1) * 100))))


async def synth_fit(text, voice, base_rate, max_rate, slot, shorten=None):
    """Sesi üretir ve cümleye ayrılan süreye (slot) sığdırır: hız önceden tahmin edilir,
    yine sığmazsa hız artırılır, o da yetmezse metin kısaltılır.
    Döner: (mp3, baştaki sessizlik, konuşma süresi, hız, seslendirilen metin)"""
    text = clean(text)
    rate = _rate_for(len(text) / CHARS_PER_SECOND, slot, base_rate, max_rate)
    audio = await synth(text, voice, rate)
    lead, speech = analyze(audio)
    if speech <= slot * 1.05:
        return audio, lead, speech, rate, text

    new_rate = _rate_for(speech * (1 + rate / 100), slot, base_rate, max_rate)
    if new_rate > rate:
        audio = await synth(text, voice, new_rate)
        lead, speech = analyze(audio)
        rate = new_rate
    if speech <= slot * 1.1 or shorten is None:
        return audio, lead, speech, rate, text

    short = await shorten(slot)
    short = clean(short) if short else ""
    if short and len(short) < len(text):
        audio2 = await synth(short, voice, rate)
        lead2, speech2 = analyze(audio2)
        if speech2 < speech:
            return audio2, lead2, speech2, rate, short
    return audio, lead, speech, rate, text

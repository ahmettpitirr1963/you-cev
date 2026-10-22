"""Metin kaynakları: ses üzerinden ASR veya elle yüklenmiş altyazı. İkisi de aynı arayüzü sunar."""
import asyncio
from pathlib import Path

import numpy as np

from pipeline import asr
from pipeline.audio import SR, ProgressiveAudio
from pipeline.downloader import load_json, save_json

BOUNDARY_RADIUS = 6.0  # parça sınırı, nominal noktanın ±6 sn'sindeki en sessiz yere kaydırılır


class AsrSource:
    kind = "asr"

    def __init__(self, audio: ProgressiveAudio, source_dir: Path):
        self.audio = audio
        self.cache_dir = source_dir / "asr_v2"  # v2: kelimeler baştaki boşluğuyla saklanır
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._mem = {}

    async def wait_until(self, t):
        await asyncio.to_thread(self.audio.wait_for, t)
        if self.audio.error and self.audio.seconds < t - 1:
            raise RuntimeError(f"Ses çözülemedi: {self.audio.error}")

    def refine(self, nominal):
        a = max(0.0, nominal - BOUNDARY_RADIUS)
        seg = self.audio.slice(a, nominal + BOUNDARY_RADIUS)
        win, hop = int(0.4 * SR), int(0.05 * SR)
        if len(seg) < win * 2:
            return nominal
        sq = np.concatenate([[0.0], np.cumsum(seg.astype(np.float64) ** 2)])
        idx = np.arange((len(seg) - win) // hop + 1) * hop
        rms = np.sqrt((sq[idx + win] - sq[idx]) / win)
        centers = a + (idx + win / 2) / SR
        score = rms * (1 + 0.04 * np.abs(centers - nominal))
        return round(float(centers[int(np.argmin(score))]), 2)

    def cached_words(self, start, end):
        key = f"{start:.2f}_{end:.2f}"
        if key not in self._mem:
            data = load_json(self.cache_dir / f"{key}.json")
            if data is not None:
                self._mem[key] = data
        return self._mem.get(key)

    async def words(self, start, end, prompt=None):
        if (cached := self.cached_words(start, end)) is not None:
            return cached
        await self.wait_until(end)

        async def run():
            words = await asyncio.to_thread(asr.transcribe, self.audio.slice(start, end), start, prompt)
            key = f"{start:.2f}_{end:.2f}"
            save_json(self.cache_dir / f"{key}.json", words)
            self._mem[key] = words
            return words

        # Parça öne alma ile iptal edilse de transkripsiyon tamamlanıp önbelleğe yazılsın
        return await asyncio.shield(asyncio.ensure_future(run()))


class SubtitleSource:
    kind = "subs"

    def __init__(self, words: list[dict]):
        self.all_words = words

    async def wait_until(self, t):
        return

    def refine(self, nominal):
        best, best_score = nominal, 0.0
        for a, b in zip(self.all_words, self.all_words[1:]):
            mid = (a["e"] + b["s"]) / 2
            if abs(mid - nominal) > BOUNDARY_RADIUS:
                continue
            score = (b["s"] - a["e"]) / (1 + 0.04 * abs(mid - nominal))
            if score > best_score:
                best, best_score = round(mid, 2), score
        return best

    def cached_words(self, start, end):
        return [w for w in self.all_words if start <= w["s"] < end]

    async def words(self, start, end, prompt=None):
        return self.cached_words(start, end)

"""Sesi arka planda 16 kHz mono'ya çözer; çözülen kısım hemen kullanılabilir."""
import threading

import av
import numpy as np

SR = 16000


class ProgressiveAudio:
    def __init__(self, path, duration_hint=None):
        self.path = str(path)
        self.duration_hint = duration_hint
        # int16 saklanır (float32'nin yarısı kadar bellek); dilimler float32'ye çevrilerek verilir
        self._buf = np.zeros(int(((duration_hint or 600) + 10) * SR), dtype=np.int16)
        self._n = 0
        self.done = False
        self.error = None
        self._cond = threading.Condition()
        threading.Thread(target=self._run, name="audio-decode", daemon=True).start()

    @staticmethod
    def probe_duration(path):
        try:
            with av.open(str(path)) as c:
                if c.duration:
                    return c.duration / av.time_base
                s = c.streams.audio[0]
                if s.duration and s.time_base:
                    return float(s.duration * s.time_base)
        except Exception:
            pass
        return None

    def _append(self, arr):
        with self._cond:
            need = self._n + len(arr)
            if need > len(self._buf):
                grown = np.zeros(max(need, int(len(self._buf) * 1.5)), dtype=np.int16)
                grown[: self._n] = self._buf[: self._n]
                self._buf = grown
            self._buf[self._n : need] = arr
            self._n = need
            self._cond.notify_all()

    def _run(self):
        try:
            with av.open(self.path) as container:
                stream = container.streams.audio[0]
                resampler = av.AudioResampler(format="s16", layout="mono", rate=SR)
                pending, pending_len = [], 0

                def take(frames):
                    nonlocal pending_len
                    for f in frames:
                        a = f.to_ndarray().reshape(-1)
                        pending.append(a)
                        pending_len += len(a)

                for packet in container.demux(stream):
                    try:
                        frames = packet.decode()
                    except av.error.InvalidDataError:
                        continue
                    for frame in frames:
                        take(resampler.resample(frame))
                    if pending_len >= SR * 5:
                        self._append(np.concatenate(pending))
                        pending, pending_len = [], 0
                take(resampler.resample(None))
                if pending:
                    self._append(np.concatenate(pending))
        except Exception as e:  # bozuk dosya vb.
            self.error = e
        finally:
            with self._cond:
                self.done = True
                self._cond.notify_all()

    @property
    def seconds(self):
        return self._n / SR

    def wait_for(self, t, timeout=None):
        """t saniyesine kadar çözülene (veya çözme bitene) kadar bekler."""
        with self._cond:
            return self._cond.wait_for(lambda: self.done or self._n >= t * SR, timeout)

    def slice(self, a, b):
        with self._cond:
            i = max(0, int(a * SR))
            j = min(int(b * SR), self._n)
            if j <= i:
                return np.zeros(0, dtype=np.float32)
            return self._buf[i:j].astype(np.float32) / 32768.0

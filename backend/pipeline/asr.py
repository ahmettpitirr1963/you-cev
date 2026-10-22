"""faster-whisper ile konuşma → kelime zaman damgalı metin."""
import logging
import os
import sys
import threading
from pathlib import Path

import numpy as np

from config import cfg
from pipeline import local_models

log = logging.getLogger("youcev.asr")

_model = None
_status = {"state": "yüklenmedi", "device": None, "model": None}
_load_lock = threading.Lock()
_run_lock = threading.Lock()  # GPU'da aynı anda tek transkripsiyon


def status():
    return dict(_status)


def _add_nvidia_dlls():
    """pip ile kurulan nvidia-cublas / nvidia-cudnn DLL'lerini Windows'ta bulunur yapar."""
    if sys.platform != "win32":
        return
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if not nv.is_dir():
            continue
        for sub in nv.iterdir():
            dll_dir = sub / "bin"
            if dll_dir.is_dir():
                os.add_dll_directory(str(dll_dir))
                os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")


def get_model():
    global _model
    with _load_lock:
        if _model is not None:
            return _model
        _status["state"] = "yükleniyor"
        _add_nvidia_dlls()
        from faster_whisper import WhisperModel

        a = cfg["asr"]
        if a["device"] != "cpu":
            try:
                model = WhisperModel(local_models.whisper_dir(a["model"]), device="cuda",
                                     compute_type=a["compute_type"])
                # Sessizlikle ısınma: cuDNN/cuBLAS eksikse hata burada çıkar
                segs, _ = model.transcribe(np.zeros(16000, dtype=np.float32), language="en")
                list(segs)
                _model = model
                _status.update(state="hazır", device="cuda", model=a["model"])
                log.info("Whisper GPU'da hazır: %s", a["model"])
                return _model
            except Exception as e:
                log.warning("CUDA kullanılamadı (%s). CPU'ya geçiliyor.", e)
        _model = WhisperModel(local_models.whisper_dir(a["cpu_model"]), device="cpu", compute_type="int8")
        _status.update(state="hazır", device="cpu", model=a["cpu_model"])
        log.info("Whisper CPU'da hazır: %s", a["cpu_model"])
        return _model


def transcribe(audio: np.ndarray, offset: float, prompt: str | None = None) -> list[dict]:
    if len(audio) < 8000:
        return []
    model = get_model()
    a = cfg["asr"]
    with _run_lock:
        segments, _ = model.transcribe(
            audio,
            language=a.get("language") or None,
            beam_size=a["beam_size"],
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
            word_timestamps=True,
            condition_on_previous_text=False,
            initial_prompt=prompt or None,
        )
        words = []
        for s in segments:
            if s.no_speech_prob > 0.6 and s.avg_logprob < -1.0:
                continue
            for w in s.words or []:
                # Baştaki boşluk korunur: "light" + "-sensitive" doğru birleşsin
                if w.word.strip():
                    words.append({"w": w.word, "s": round(w.start + offset, 3), "e": round(w.end + offset, 3)})
    return words

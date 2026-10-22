"""İnternet servisleri kısıtlama yaptığında devreye giren yerel (çevrimdışı) modeller:
- Çeviri: NLLB-200 1.3B (CTranslate2, GPU) — OpenNMT'nin resmi dönüşümü
- Seslendirme: Piper tr_TR-dfki-medium (CPU)
Modeller sunucu açılışında arka planda indirilir; ilk ihtiyaçta yüklenir."""
import io
import logging
import threading
import time

import av

from config import CACHE_DIR, cfg

log = logging.getLogger("youcev.local")

MODELS_DIR = CACHE_DIR / "models"
NLLB_REPO = "OpenNMT/nllb-200-distilled-1.3B-ct2-int8"
NLLB_FILES = ["config.json", "model.bin", "shared_vocabulary.json", "tokenizer.json", "special_tokens_map.json"]
PIPER_REPO = "rhasspy/piper-voices"
PIPER_FILES = ["tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx", "tr/tr_TR/dfki/medium/tr_TR-dfki-medium.onnx.json"]

_status = {"whisper": "indirilmedi", "nllb": "indirilmedi", "piper": "indirilmedi"}
_lock = threading.Lock()
_nllb = None
_piper = None
_nllb_run = threading.Lock()
_piper_run = threading.Lock()


def status():
    return dict(_status)


def _target(repo):
    return MODELS_DIR / repo.replace("/", "__")


def _fetch(repo, fname, dest, name):
    """Kaldığı yerden devam edebilen düz HTTP indirme (huggingface_hub bu sistemde asılı kalıyor)."""
    import httpx

    url = f"https://huggingface.co/{repo}/resolve/main/{fname}"
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for attempt in range(30):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with httpx.stream("GET", url, headers=headers, follow_redirects=True,
                              timeout=httpx.Timeout(30, read=90)) as r:
                if r.status_code == 416:  # zaten tamamı inmiş
                    break
                r.raise_for_status()
                if have and r.status_code != 206:  # sunucu devam etmeyi desteklemedi: baştan
                    have = 0
                total = have + int(r.headers.get("content-length") or 0)
                with open(part, "ab" if have else "wb") as f:
                    done = have
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        if total > 10_000_000:
                            _status[name] = f"indiriliyor %{done * 100 // total}"
            if total and part.stat().st_size < total:
                raise IOError("indirme yarım kaldı")
            break
        except Exception as e:
            last = e
            log.info("%s indirmesi kesildi (%s), devam edilecek", fname, e)
            time.sleep(min(30, 2 ** min(attempt, 5)))
    else:
        raise last
    part.replace(dest)


def _download(repo, files, name):
    target = _target(repo)
    paths = []
    for f in files:
        dest = target / f
        if not dest.exists():
            _fetch(repo, f, dest, name)
        paths.append(str(dest))
    _status[name] = "hazır"
    return target, paths


WHISPER_FILES = ["config.json", "model.bin", "preprocessor_config.json", "tokenizer.json", "vocabulary.json",
                 "vocabulary.txt"]


def whisper_dir(model_name: str) -> str:
    """Whisper modelini kendi indiricimizle indirir ve klasörünü döner (huggingface_hub takılmasın)."""
    import httpx
    from faster_whisper.utils import _MODELS

    repo = _MODELS.get(model_name, model_name)
    target = _target(repo)
    done_flag = target / ".tamam"
    if not done_flag.exists():
        _status["whisper"] = "indiriliyor"
        # Depoda hangi dosyaların olduğuna bak (bazı modellerde preprocessor_config yok)
        info = httpx.get(f"https://huggingface.co/api/models/{repo}", timeout=30, follow_redirects=True).json()
        existing = {s["rfilename"] for s in info.get("siblings", [])}
        _download(repo, [f for f in WHISPER_FILES if f in existing], "whisper")
        done_flag.write_text("ok")
    _status["whisper"] = "hazır"
    return str(target)


def _local_files(repo, files, name):
    """Model diskte tamsa yollarını döner; değilse beklemeden hata verir (indirme arka planda sürer)."""
    target = _target(repo)
    paths = [target / f for f in files]
    if not all(p.exists() for p in paths):
        raise RuntimeError(f"yerel {name} modeli henüz indirilmedi ({_status[name]})")
    return target, [str(p) for p in paths]


def available(name):
    repo, files = (NLLB_REPO, NLLB_FILES) if name == "nllb" else (PIPER_REPO, PIPER_FILES)
    return all((_target(repo) / f).exists() for f in files)


def ensure_downloaded():
    """Sunucu açılışında arka planda çalışır; zaten varsa anında döner. Önce küçük olan (Piper)."""
    for repo, files, name in ((PIPER_REPO, PIPER_FILES, "piper"), (NLLB_REPO, NLLB_FILES, "nllb")):
        if available(name):
            _status[name] = "hazır"
            continue
        try:
            _status[name] = "indiriliyor"
            _download(repo, files, name)
            log.info("Yerel yedek hazır: %s", name)
        except Exception as e:
            _status[name] = f"hata: {e}"
            log.warning("Yerel model indirilemedi (%s): %s", name, e)


# ---------- Çeviri (NLLB) ----------

def _load_nllb():
    global _nllb
    with _lock:
        if _nllb is not None:
            return _nllb
        import ctranslate2
        from tokenizers import Tokenizer

        model_dir, _ = _local_files(NLLB_REPO, NLLB_FILES, "nllb")
        tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        try:
            device = "cuda" if cfg["asr"]["device"] != "cpu" and ctranslate2.get_cuda_device_count() else "cpu"
            translator = ctranslate2.Translator(str(model_dir), device=device,
                                                compute_type="int8_float16" if device == "cuda" else "int8")
        except Exception as e:
            log.warning("NLLB GPU'da açılamadı (%s), CPU kullanılıyor", e)
            device = "cpu"
            translator = ctranslate2.Translator(str(model_dir), device="cpu", compute_type="int8")
        _nllb = (tokenizer, translator)
        _status["nllb"] = f"yüklü ({device})"
        log.info("Yerel çeviri modeli yüklendi (%s)", device)
        return _nllb


def translate_local(texts: list[str]) -> list[str]:
    if not texts:
        return []
    tokenizer, translator = _load_nllb()
    sources = []
    for t in texts:
        tokens = tokenizer.encode(t, add_special_tokens=False).tokens
        sources.append(["eng_Latn"] + tokens[:400] + ["</s>"])
    with _nllb_run:
        results = translator.translate_batch(
            sources,
            target_prefix=[["tur_Latn"]] * len(sources),
            beam_size=4,
            max_decoding_length=300,
            repetition_penalty=1.1,
        )
    out = []
    for r in results:
        tokens = [tok for tok in r.hypotheses[0] if tok != "tur_Latn"]
        ids = [tokenizer.token_to_id(tok) for tok in tokens]
        out.append(tokenizer.decode([i for i in ids if i is not None], skip_special_tokens=True).strip())
    return out


# ---------- Seslendirme (Piper) ----------

def _load_piper():
    global _piper
    with _lock:
        if _piper is not None:
            return _piper
        from piper import PiperVoice

        _, paths = _local_files(PIPER_REPO, PIPER_FILES, "piper")
        _piper = PiperVoice.load(paths[0], config_path=paths[1])
        _status["piper"] = "yüklü"
        log.info("Yerel ses (Piper) yüklendi")
        return _piper


def _encode_mp3(pcm: bytes, sample_rate: int) -> bytes:
    """Piper'ın 16-bit mono PCM çıktısını mp3'e çevirir (oynatıcı tek biçimle çalışsın)."""
    import numpy as np

    out = io.BytesIO()
    with av.open(out, mode="w", format="mp3") as container:
        stream = container.add_stream("libmp3lame", rate=24000)
        stream.layout = "mono"
        stream.bit_rate = 64000
        resampler = av.AudioResampler(format="s16p", layout="mono", rate=24000)
        frame = av.AudioFrame.from_ndarray(np.frombuffer(pcm, dtype=np.int16).reshape(1, -1),
                                           format="s16", layout="mono")
        frame.sample_rate = sample_rate
        for f in resampler.resample(frame) + resampler.resample(None):
            for packet in stream.encode(f):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return out.getvalue()


def synth_local(text: str, rate: int) -> bytes:
    from piper import SynthesisConfig

    voice = _load_piper()
    # Piper'da length_scale < 1 konuşmayı hızlandırır; edge-tts'teki +%rate ile eşleştir
    config = SynthesisConfig(length_scale=1 / (1 + max(-50, rate) / 100))
    pcm = bytearray()
    sample_rate = 22050
    with _piper_run:
        for chunk in voice.synthesize(text, syn_config=config):
            pcm.extend(chunk.audio_int16_bytes)
            sample_rate = chunk.sample_rate
    if not pcm:
        raise RuntimeError("Piper boş ses üretti")
    return _encode_mp3(bytes(pcm), sample_rate)

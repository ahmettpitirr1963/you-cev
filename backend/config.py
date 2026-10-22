"""Ayarlar: config.yaml + cache/settings.json (arayüzden değiştirilen ayarlar)."""
import copy
import hashlib
import json
import os
import threading
from pathlib import Path

import yaml

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
CONFIG_PATH = BACKEND_DIR / "config.yaml"
CACHE_DIR = PROJECT_DIR / "cache"
SETTINGS_PATH = CACHE_DIR / "settings.json"
WEB_DIR = PROJECT_DIR / "web"
EXTENSION_DIR = PROJECT_DIR / "extension"

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# Hugging Face'in "xet" indiricisi bu sistemde büyük dosyalarda 0 baytta asılı kalabiliyor; düz HTTP kullan
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

VOICES = {
    "tr-TR-AhmetNeural": "Ahmet (erkek)",
    "tr-TR-EmelNeural": "Emel (kadın)",
}

DEFAULTS = {
    "server": {"host": "127.0.0.1", "port": 8765},
    "asr": {
        "model": "large-v3-turbo",
        "device": "cuda",
        "compute_type": "int8_float16",
        "cpu_model": "small.en",
        "language": "en",
        "beam_size": 5,
    },
    "pipeline": {"first_chunk_seconds": 20, "chunk_seconds": 60, "use_manual_subtitles": True},
    "translate": {
        "engine": "auto",
        "ollama_url": "http://127.0.0.1:11434",
        "ollama_model": "gemma3:4b-it-qat",
        "ollama_think": None,
        "context_sentences": 3,
    },
    "tts": {"engine": "auto", "voice": "tr-TR-AhmetNeural", "base_rate": 0, "max_rate": 30, "concurrency": 4},
    "glossary": {},
}

# Arayüzden değiştirilebilen alanlar ve doğrulayıcıları
SETTABLE = {
    ("translate", "engine"): lambda v: v in ("auto", "ollama", "google", "local"),
    ("translate", "ollama_model"): lambda v: isinstance(v, str) and 0 < len(v) < 100,
    ("tts", "voice"): lambda v: v in VOICES,
    ("tts", "base_rate"): lambda v: isinstance(v, int) and -20 <= v <= 30,
}

_lock = threading.Lock()


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _load():
    data = copy.deepcopy(DEFAULTS)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = _merge(data, yaml.safe_load(f) or {})
    data["glossary"] = data.get("glossary") or {}
    return _merge(data, _read_json(SETTINGS_PATH))


cfg = _load()


def public_settings():
    return {
        "translate": {"engine": cfg["translate"]["engine"], "ollama_model": cfg["translate"]["ollama_model"]},
        "tts": {"voice": cfg["tts"]["voice"], "base_rate": cfg["tts"]["base_rate"]},
        "voices": VOICES,
    }


def update_settings(patch: dict) -> dict:
    with _lock:
        saved = _read_json(SETTINGS_PATH)
        for section, values in (patch or {}).items():
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                check = SETTABLE.get((section, key))
                if check and check(value):
                    cfg[section][key] = value
                    saved.setdefault(section, {})[key] = value
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    return public_settings()


def profile_id(engine: str) -> str:
    """Çeviri + ses ayarlarının kısa özeti. Ayar değişirse çeviri/ses önbelleği ayrı tutulur."""
    parts = {
        "engine": engine,
        "voice": cfg["tts"]["voice"],
        "rate": cfg["tts"]["base_rate"],
        "glossary": cfg["glossary"] if engine == "ollama" else {},
    }
    if engine == "ollama":
        parts["model"] = cfg["translate"]["ollama_model"]
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:8]

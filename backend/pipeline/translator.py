"""İngilizce → Türkçe çeviri.
Motorlar: Google (hızlı, iyi), Ollama (yerel LLM, bağlamlı), yerel NLLB (çevrimdışı, her zaman çalışır).
İnternet servisleri kısıtlama yaparsa (429) hemen yedeğe geçilir; servis bir süre hiç denenmez."""
import asyncio
import json
import logging
import time

import httpx

from config import cfg
from pipeline import local_models

log = logging.getLogger("youcev.translate")

GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
GOOGLE_LIMIT = 4500  # karakter


class Breaker:
    """Devre kesici: servis kısıtlama yaparsa ona yüklenmeyi bırak (yüklenmek kısıtlamayı uzatır)."""

    def __init__(self, name, down_seconds=600, fails_to_open=3):
        self.name = name
        self.down_seconds = down_seconds
        self.fails_to_open = fails_to_open
        self.fails = 0
        self.down_until = 0.0

    def usable(self):
        return time.monotonic() >= self.down_until

    def ok(self):
        if self.down_until:
            log.info("%s yeniden çalışıyor", self.name)
        self.fails = 0
        self.down_until = 0.0

    def failed(self, rate_limited=False):
        self.fails += 1
        if (rate_limited or self.fails >= self.fails_to_open) and self.usable():
            self.down_until = time.monotonic() + self.down_seconds
            log.warning("%s kullanılamıyor (%s); %d dk boyunca yedek kullanılacak", self.name,
                        "kısıtlama" if rate_limited else "art arda hata", self.down_seconds // 60)


class RateLimited(Exception):
    pass


def _parse_single(data):
    return "".join(part[0] for part in data[0] if part and part[0])


def _parse_t(data):
    item = data[0] if isinstance(data, list) else data
    return item[0] if isinstance(item, list) else item


# Aynı servisin ayrı kotalı uç noktaları: biri kısıtlarsa (429) diğerine geçilir
GOOGLE_ENDPOINTS = [
    ("Google gtx", GOOGLE_URL, {"client": "gtx", "sl": "en", "tl": "tr", "dt": "t"}, _parse_single),
    ("Google dict", "https://translate.googleapis.com/translate_a/t",
     {"client": "dict-chrome-ex", "sl": "en", "tl": "tr"}, _parse_t),
    ("Google dict (clients5)", "https://clients5.google.com/translate_a/t",
     {"client": "dict-chrome-ex", "sl": "en", "tl": "tr"}, _parse_t),
]
_endpoint_breakers = {name: Breaker(name) for name, *_ in GOOGLE_ENDPOINTS}


class _AnyGoogle:
    """En az bir Google uç noktası kullanılabilir mi?"""

    def usable(self):
        return any(b.usable() for b in _endpoint_breakers.values())


google_breaker = _AnyGoogle()


class GoogleEngine:
    name = "google"
    can_shorten = False

    def __init__(self):
        self.client = httpx.Client(timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"})

    def _one(self, text):
        last = None
        for name, url, params, parse in GOOGLE_ENDPOINTS:
            breaker = _endpoint_breakers[name]
            if not breaker.usable():
                continue
            for attempt in range(2):
                try:
                    r = self.client.post(url, params=params, data={"q": text})
                    if r.status_code == 429:
                        breaker.failed(rate_limited=True)
                        last = RateLimited(f"{name} kısıtlama yaptı (429)")
                        break  # bu uç noktaya yüklenme, sıradakine geç
                    r.raise_for_status()
                    result = parse(r.json()).strip()
                    if not result:
                        raise ValueError("boş çeviri")
                    breaker.ok()
                    return result
                except Exception as e:
                    last = e
                    if attempt == 0:
                        time.sleep(0.5)
            else:
                breaker.failed()
        raise last or RateLimited("Tüm Google uç noktaları kısıtlı")

    def _group(self, group):
        if len(group) == 1:
            return [self._one(group[0])]
        # Satırları tek istekte çevirmek hem hızlı hem bağlam sağlar
        parts = [p.strip() for p in self._one("\n".join(group)).split("\n") if p.strip()]
        if len(parts) == len(group):
            return parts
        # Satırlar birleşmiş: yerel model hazırsa onunla, değilse satır satır çevir
        if local_models.available("nllb"):
            return local_models.translate_local(group)
        return [self._one(t) for t in group]

    def _batch(self, texts):
        out, group, size = [], [], 0
        for t in texts:
            if group and size + len(t) + 1 > GOOGLE_LIMIT:
                out += self._group(group)
                group, size = [], 0
            group.append(t)
            size += len(t) + 1
        if group:
            out += self._group(group)
        return out

    async def translate(self, items, context, title):
        if not items:
            return []
        return await asyncio.to_thread(self._batch, [it["en"] for it in items])

    async def shorten(self, en, tr, seconds):
        return None


class LocalEngine:
    """NLLB-200 (GPU, çevrimdışı). İnternete ve kısıtlamaya bağlı değil."""
    name = "local"
    can_shorten = False

    async def translate(self, items, context, title):
        if not items:
            return []
        return await asyncio.to_thread(local_models.translate_local, [it["en"] for it in items])

    async def shorten(self, en, tr, seconds):
        return None


class ResilientEngine:
    """Asıl motoru dener; kısıtlama/hata olursa (veya devre açıksa) yerel modele geçer."""

    def __init__(self, primary, breaker=None):
        self.primary = primary
        self.breaker = breaker
        self.local = LocalEngine()
        self.name = primary.name
        self.can_shorten = primary.can_shorten

    async def translate(self, items, context, title):
        if local_models.available("nllb") and self.breaker is not None and not self.breaker.usable():
            return await self.local.translate(items, context, title)
        last = None
        # Yerel model henüz yoksa: anlık yoğunluk kısıtlamaları genelde birkaç saniyede geçer
        for attempt in range(1 if local_models.available("nllb") else 2):
            try:
                return await self.primary.translate(items, context, title)
            except Exception as e:
                last = e
                log.warning("%s çevirisi başarısız (%s)", self.primary.name, e)
                if not local_models.available("nllb") and attempt == 0:
                    await asyncio.sleep(4)
        if local_models.available("nllb"):
            return await self.local.translate(items, context, title)
        raise last

    async def shorten(self, en, tr, seconds):
        return await self.primary.shorten(en, tr, seconds)


SYSTEM_PROMPT = """You are a professional English-to-Turkish simultaneous interpreter and dubbing translator.
Translate every line into natural, fluent, spoken Turkish, exactly as a native Turkish narrator would say it.
Rules:
- Keep the meaning exact. Do not add, explain or omit information.
- Be concise: each line must be speakable within its "seconds" (Turkish speech is about 14 characters per second).
- Use "previous_context" only to understand the topic; translate only "lines".
- Keep person names, brands, product names and code identifiers as they are.
- Use the glossary translations when the terms appear.
- A line may be an incomplete sentence; translate it naturally as a fragment.
Return JSON: {"translations": [{"id": <id>, "tr": "<Turkish>"}]} with one entry for every id."""

TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "tr": {"type": "string"}},
                "required": ["id", "tr"],
            },
        }
    },
    "required": ["translations"],
}

SHORTEN_SCHEMA = {"type": "object", "properties": {"tr": {"type": "string"}}, "required": ["tr"]}


class OllamaEngine:
    name = "ollama"
    can_shorten = True

    def __init__(self):
        self.fallback = ResilientEngine(GoogleEngine(), google_breaker)

    async def _chat(self, system, user, schema, timeout=180):
        t = cfg["translate"]
        body = {
            "model": t["ollama_model"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "options": {"temperature": 0.2, "num_ctx": 4096},
            "keep_alive": "30m",
        }
        if t.get("ollama_think") is not None:
            body["think"] = t["ollama_think"]
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(f"{t['ollama_url']}/api/chat", json=body)
            r.raise_for_status()
            return json.loads(r.json()["message"]["content"])

    async def translate(self, items, context, title):
        if not items:
            return []
        lines = [{"id": i + 1, "en": it["en"], "seconds": round(it["dur"], 1)} for i, it in enumerate(items)]
        payload = {
            "video_title": title,
            "previous_context": context,
            "glossary": cfg["glossary"],
            "lines": lines,
        }
        result = [None] * len(items)
        try:
            data = await self._chat(SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False), TRANSLATION_SCHEMA)
            for row in data.get("translations", []):
                idx = row.get("id", 0) - 1
                if 0 <= idx < len(result) and str(row.get("tr", "")).strip():
                    result[idx] = row["tr"].strip()
        except Exception as e:
            log.warning("Ollama çevirisi başarısız (%s), yedek kullanılıyor", e)
        missing = [i for i, r in enumerate(result) if r is None]
        if missing:
            filled = await self.fallback.translate([items[i] for i in missing], context, title)
            for i, tr in zip(missing, filled):
                result[i] = tr
        return result

    async def shorten(self, en, tr, seconds):
        max_chars = max(8, int(seconds * 14))
        user = json.dumps(
            {"english": en, "turkish": tr, "seconds": round(seconds, 1), "max_characters": max_chars},
            ensure_ascii=False,
        )
        system = (
            "You shorten Turkish dubbing lines. Rewrite 'turkish' so it keeps the core meaning of 'english' "
            "but fits within max_characters and sounds natural when spoken. Return JSON {\"tr\": \"...\"}."
        )
        try:
            data = await self._chat(system, user, SHORTEN_SCHEMA, timeout=60)
            text = str(data.get("tr", "")).strip()
            return text or None
        except Exception as e:
            log.warning("Kısaltma başarısız: %s", e)
            return None


_engines = {}


def get_translator(engine: str):
    if engine not in _engines:
        if engine == "ollama":
            _engines[engine] = OllamaEngine()  # kendi içinde Google → yerel yedeğine düşer
        elif engine == "local":
            _engines[engine] = LocalEngine()
        else:
            _engines[engine] = ResilientEngine(GoogleEngine(), google_breaker)
    return _engines[engine]


async def ollama_ready() -> bool:
    t = cfg["translate"]
    try:
        async with httpx.AsyncClient(timeout=1.5) as client:
            r = await client.get(f"{t['ollama_url']}/api/tags")
            r.raise_for_status()
            names = {m.get("name") for m in r.json().get("models", [])}
    except Exception:
        return False
    model = t["ollama_model"]
    return model in names or f"{model}:latest" in names


async def resolve_engine() -> str:
    engine = cfg["translate"]["engine"]
    if engine in ("google", "local"):
        return engine
    if await ollama_ready():
        return "ollama"
    if engine == "ollama":
        log.warning("Ollama veya '%s' modeli bulunamadı; Google kullanılıyor", cfg["translate"]["ollama_model"])
    return "google"

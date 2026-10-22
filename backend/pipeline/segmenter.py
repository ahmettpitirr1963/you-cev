"""Kelimeleri tam cümlelere birleştirir. Yarım cümle çevirisi en büyük hata kaynağıdır."""
import re

MAX_SENTENCE = 12.0  # sn; daha uzun cümleler virgülden bölünür
PAUSE_BREAK = 1.5    # sn; bu kadar uzun sessizlik cümleyi bitirir
SHORT_WORDS = 3

ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.", "vs.", "etc.", "e.g.", "i.e.",
    "inc.", "ltd.", "co.", "no.", "approx.", "fig.", "mt.", "u.s.", "u.k.", "a.m.", "p.m.",
}
_TERMINAL = re.compile(r"[.!?…][\"'”’)\]]*$")
_CLAUSE = re.compile(r"[,;:—–][\"'”’)\]]*$")


def _ends_sentence(word: str) -> bool:
    word = word.strip()
    if word.lower() in ABBREVIATIONS:
        return False
    return bool(_TERMINAL.search(word))


def join_words(words) -> str:
    """Kelimeler kendi baştaki boşluklarını taşır (Whisper biçimi)."""
    return re.sub(r"\s+", " ", "".join(w["w"] for w in words)).strip()


def _make(words, complete):
    return {
        "start": words[0]["s"],
        "end": words[-1]["e"],
        "text": join_words(words),
        "words": list(words),
        "complete": complete,
    }


def is_complete(sentence) -> bool:
    return sentence["complete"]


def segment(words: list[dict]) -> list[dict]:
    sentences = []
    cur = []
    for w in words:
        if cur and w["s"] - cur[-1]["e"] > PAUSE_BREAK:
            sentences.append(_make(cur, True))
            cur = []
        cur.append(w)
        if _ends_sentence(w["w"]):
            sentences.append(_make(cur, True))
            cur = []
        elif cur[-1]["e"] - cur[0]["s"] > MAX_SENTENCE:
            cut = next((i for i in range(len(cur) - 2, 2, -1) if _CLAUSE.search(cur[i]["w"].strip())), None)
            if cut is None:
                sentences.append(_make(cur, True))
                cur = []
            else:
                sentences.append(_make(cur[: cut + 1], True))
                cur = cur[cut + 1 :]
    if cur:
        sentences.append(_make(cur, False))
    return _merge_short(sentences)


def _merge_short(sentences):
    """'Okay.' 'Yeah.' gibi çok kısa cümleleri bir sonrakiyle birleştirir."""
    out = []
    i = 0
    while i < len(sentences):
        s = sentences[i]
        if i + 1 < len(sentences):
            n = sentences[i + 1]
            if (
                len(s["words"]) < SHORT_WORDS
                and n["start"] - s["end"] < 0.5
                and n["end"] - s["start"] <= MAX_SENTENCE * 0.7
            ):
                sentences[i + 1] = _make(s["words"] + n["words"], n["complete"])
                i += 1
                continue
        out.append(s)
        i += 1
    return out

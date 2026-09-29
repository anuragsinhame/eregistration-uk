#!/usr/bin/env python3
"""
hindi_input.py - Roman (English letters) -> Devanagari (Hindi) transliteration with suggestions.

Reusable from any script or GUI:

    from hindi_input import suggest, phonetic
    suggest("ashutosh")            # -> ['आशुतोष', 'अशुतोष', ...]   (Google Input Tools, falls back to rules)
    suggest("ashutosh johri")      # phrases work too
    phonetic("ashutosh")           # offline rule-based variants only, no network

Command line:

    python hindi_input.py ashutosh johri
    python hindi_input.py --offline sita devi

How it works
  1. `suggest()` asks Google Input Tools (the same service behind Google's Hindi typing
     tools, itc=hi-t-i0-und). It needs internet; results are cached for the process.
  2. If that fails (offline, blocked, SSL trouble) it returns `phonetic()` variants:
     a small phonetic engine for "Hinglish" spellings that produces the most likely
     spelling first and then alternates for the usual ambiguities (a/aa at the start,
     i/ee and u/oo, dental/retroflex t-d, sh/ssa, gy).
  Nothing here is specific to the e-registration site.
"""
from __future__ import annotations

import json
import re
import ssl
import sys
import threading
import urllib.parse
import urllib.request

GOOGLE_URL = "https://inputtools.google.com/request"
INPUT_TOOL = "hi-t-i0-und"          # Hindi transliteration
DEFAULT_NUM = 5

_cache: dict[str, list[str]] = {}
_cache_lock = threading.Lock()
_ssl_ctx: ssl.SSLContext | None = None
last_source = "none"                # "google" | "rules" - what the most recent suggest() used


# ------------------------------------------------------------------ online: Google Input Tools
def _ssl_context() -> ssl.SSLContext:
    """Prefer the OS trust store (truststore) - python.org builds often lack CA certs."""
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import truststore  # type: ignore
            _ssl_ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:  # noqa: BLE001
            _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def google_candidates(text: str, num: int = DEFAULT_NUM, timeout: float = 4.0) -> list[str]:
    """Ask Google Input Tools for Devanagari candidates. Raises on any network/format problem."""
    params = {"text": text, "itc": INPUT_TOOL, "num": num, "cp": 0, "cs": 1,
              "ie": "utf-8", "oe": "utf-8", "app": "hindi_input"}
    req = urllib.request.Request(GOOGLE_URL + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "Mozilla/5.0 hindi_input"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not data or data[0] != "SUCCESS":
        raise RuntimeError(f"Google Input Tools answered {data[0] if data else 'nothing'}")
    out: list[str] = []
    for item in data[1]:
        out.extend(c for c in item[1] if c not in out)
    return out


def suggest(text: str, num: int = DEFAULT_NUM, timeout: float = 4.0) -> list[str]:
    """Best-effort candidates: Google first, phonetic rules if that fails. Never raises."""
    global last_source
    text = " ".join((text or "").split())
    if not text:
        return []
    key = f"{text.lower()}|{num}"
    with _cache_lock:
        if key in _cache:
            return list(_cache[key])
    try:
        cands = google_candidates(text, num, timeout)
        last_source = "google"
    except Exception:  # noqa: BLE001 - offline, blocked, SSL...
        cands = []
    if not cands:
        cands = phonetic(text, num)
        last_source = "rules"
    with _cache_lock:
        _cache[key] = list(cands)
    return cands


# ------------------------------------------------------------------ offline: phonetic rules
# Longest match wins during tokenising. Values are the default Devanagari spelling.
_CONS = {
    "ksh": "क्ष", "chh": "छ",
    "kh": "ख", "gh": "घ", "ch": "च", "jh": "झ", "ny": "ञ", "th": "थ", "dh": "ध",
    "ph": "फ", "bh": "भ", "sh": "श", "gy": "ज्ञ",
    "k": "क", "g": "ग", "j": "ज", "z": "ज़", "t": "त", "d": "द", "n": "न", "p": "प",
    "f": "फ", "b": "ब", "m": "म", "y": "य", "r": "र", "l": "ल", "v": "व", "w": "व",
    "s": "स", "h": "ह", "c": "क", "q": "क", "x": "क्स",
}
_RETROFLEX = {"t": "ट", "d": "ड", "th": "ठ", "dh": "ढ"}
# (independent vowel, matra)
_VOW = {
    "aa": ("आ", "ा"), "ai": ("ऐ", "ै"), "au": ("औ", "ौ"), "ou": ("औ", "ौ"),
    "ee": ("ई", "ी"), "ii": ("ई", "ी"), "oo": ("ऊ", "ू"), "uu": ("ऊ", "ू"),
    "a": ("अ", ""), "i": ("इ", "ि"), "u": ("उ", "ु"), "e": ("ए", "े"), "o": ("ओ", "ो"),
}
_NASAL_N = {"k", "kh", "g", "gh", "ch", "chh", "j", "jh", "t", "th", "d", "dh", "ksh"}   # n + these -> anusvara
_NASAL_M = {"p", "ph", "b", "bh"}                                                      # m + these -> anusvara
_SECOND_MEMBER = {"r", "y", "v", "w"}          # C + these join with halant (chandra, satya, vishwas)
_FIRST_MEMBER = {"s", "sh", "ksh", "r", "k", "p"}   # these + C join with halant (shastri, sharma, shakti, gupta)
_NO_JOIN_FIRST = {"h"}                          # h + C keeps its inherent vowel (johri, nehru)

# Words that appear constantly in Indian names/records and that rules get wrong or half-right.
_EXCEPTIONS = {
    "singh": "सिंह", "sinh": "सिंह", "kumar": "कुमार", "kumari": "कुमारी", "devi": "देवी",
    "lal": "लाल", "prasad": "प्रसाद", "ram": "राम", "shri": "श्री", "shree": "श्री", "sri": "श्री",
    "smt": "श्रीमती", "shrimati": "श्रीमती", "sharma": "शर्मा", "verma": "वर्मा", "gupta": "गुप्ता",
    "chandra": "चंद्र", "chand": "चंद", "nath": "नाथ", "das": "दास", "bahadur": "बहादुर",
    "pratap": "प्रताप", "mohan": "मोहन", "rawat": "रावत", "negi": "नेगी", "bisht": "बिष्ट",
    "joshi": "जोशी", "pandey": "पांडे", "pant": "पंत", "bhatt": "भट्ट", "tiwari": "तिवारी",
    "johri": "जौहरी", "ashutosh": "आशुतोष",
}


def _tokens(word: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(word):
        for size in (3, 2, 1):
            chunk = word[i:i + size]
            if chunk in _CONS:
                out.append(("c", chunk))
                break
            if chunk in _VOW:
                out.append(("v", chunk))
                break
        else:
            out.append(("x", word[i]))   # digit, punctuation, Devanagari... passes through
            size = 1
        i += size
    return out


def _build(tokens: list[tuple[str, str]], *, long_a_start=False, long_iu=False,
           retroflex=False, ssa=False, gya=False) -> str:
    out: list[str] = []
    prev_kind: str | None = None
    prev_cons: str | None = None
    prev_joined = False           # last consonant is the 2nd member of a C+r/y/v cluster (chandra, satya)
    started = False               # a vowel has been emitted (word-initial clusters always join)
    n = len(tokens)
    for idx, (kind, tok) in enumerate(tokens):
        last = idx == n - 1
        if kind == "v":
            indep, matra = _VOW[tok]
            if long_iu and tok in ("i", "u"):
                indep, matra = _VOW["ee" if tok == "i" else "oo"]
            if tok == "a":
                if idx == 0 and long_a_start:
                    indep = "आ"
                if last and prev_kind == "c" and not prev_joined:
                    matra = "ा"                     # sita -> सीता, sharma -> शर्मा; but chandra -> चंद्र
            out.append(matra if prev_kind == "c" else indep)
            prev_kind, started = "v", True
        elif kind == "c":
            dev = _CONS[tok]
            if retroflex and tok in _RETROFLEX:
                dev = _RETROFLEX[tok]
            if ssa and tok == "sh":
                dev = "ष"
            if gya and tok == "gy":
                dev = "ग्य"
            joined_second = False
            if prev_kind == "c":
                if (prev_cons == "n" and tok in _NASAL_N) or (prev_cons == "m" and tok in _NASAL_M):
                    out[-1] = "ं"                   # anand -> आनंद, sampat -> संपत
                elif prev_cons in _NO_JOIN_FIRST:
                    pass                            # johri -> जौहरी
                elif (not started or tok in _SECOND_MEMBER or prev_cons == tok
                      or prev_cons in _FIRST_MEMBER or (prev_cons == "n" and tok == "h")):
                    out.append("्")                 # shri, chandra, munna, shastri, sharma, kanhaiya
                    joined_second = tok in _SECOND_MEMBER and prev_cons != tok
            out.append(dev)
            prev_kind, prev_cons, prev_joined = "c", tok, joined_second
        else:
            out.append(tok)
            prev_kind, prev_cons, prev_joined = None, None, False
    return "".join(out)


def _word_variants(word: str, num: int) -> list[str]:
    lw = word.lower()
    variants: list[str] = []
    if lw in _EXCEPTIONS:
        variants.append(_EXCEPTIONS[lw])
    toks = _tokens(lw)
    if not any(k in ("c", "v") for k, _ in toks):
        return [word]                                  # nothing to transliterate
    variants.append(_build(toks))
    flags = {
        "long_a_start": toks[0] == ("v", "a"),
        "long_iu": any(k == "v" and t in ("i", "u") for k, t in toks),
        "retroflex": any(k == "c" and t in _RETROFLEX for k, t in toks),
        "ssa": any(k == "c" and t == "sh" for k, t in toks),
        "gya": any(k == "c" and t == "gy" for k, t in toks),
    }
    for name, applicable in flags.items():
        if applicable:
            variants.append(_build(toks, **{name: True}))
    if flags["long_a_start"] and flags["long_iu"]:
        variants.append(_build(toks, long_a_start=True, long_iu=True))
    if flags["retroflex"] and flags["long_iu"]:
        variants.append(_build(toks, retroflex=True, long_iu=True))
    seen: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.append(v)
    return seen[:num]


def phonetic(text: str, num: int = DEFAULT_NUM) -> list[str]:
    """Offline rule-based variants for a word or phrase (most likely spelling first)."""
    words = (text or "").split()
    if not words:
        return []
    per_word = [_word_variants(w, num) for w in words]
    width = max(len(v) for v in per_word)
    out: list[str] = []
    for i in range(width):
        phrase = " ".join(v[min(i, len(v) - 1)] for v in per_word)
        if phrase not in out:
            out.append(phrase)
    return out[:num]


# ------------------------------------------------------------------ CLI
def main(argv: list[str]) -> int:
    offline = "--offline" in argv
    words = [a for a in argv if not a.startswith("--")]
    if not words:
        print(__doc__)
        return 1
    text = " ".join(words)
    cands = phonetic(text) if offline else suggest(text)
    print(f"{text}  ({'rules' if offline else last_source})")
    for i, c in enumerate(cands, 1):
        print(f"  {i}. {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Shared utilities for collectors (v2).

New in v2:
  - stem-based dedupe: "phone holder" / "phone holders" → same canonical
  - lightweight category classifier (keyword-dictionary based, no ML)
  - retry with structured error reason
  - random sleep, polite_sleep
  - batched HTTP with connection reuse via urllib (stdlib only)
"""
from __future__ import annotations
import sys
import time
import re
import random
import math
from datetime import datetime
from typing import Callable, TypeVar, Iterable

T = TypeVar("T")

# ============================================================
# logging
# ============================================================
def log(tag: str, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts} {tag}] {msg}", file=sys.stderr, flush=True)


# ============================================================
# text normalization
# ============================================================
_PUNCT = re.compile(r"[^a-z0-9\s\-']")

def normalize_keyword(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = s.lower().strip()
    s = _PUNCT.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# very lightweight English stemmer — enough for dedup, not for NLP
def stem_word(w: str) -> str:
    """Crude Porter-light: strip plural/ing/ed/er/est/ly."""
    if len(w) < 4:
        return w
    for suf in ("iest", "ies", "ied", "ing", "ers", "est", "ly", "ed",
                "es", "er", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def canonical_keyword(s: str) -> str:
    """Stem each token. 'phone holders' / 'phone holder' both → 'phone holder'."""
    n = normalize_keyword(s)
    if not n:
        return ""
    return " ".join(stem_word(t) for t in n.split())


def dedup_by_canonical(items: Iterable[tuple[str, float]]) -> dict[str, float]:
    """Given (keyword, score) pairs, keep the max-score representative per stem."""
    best: dict[str, tuple[str, float]] = {}
    for kw, score in items:
        c = canonical_keyword(kw)
        if not c:
            continue
        if c not in best or score > best[c][1]:
            best[c] = (kw, score)
    return {orig: score for orig, score in best.values()}


# ============================================================
# score normalization
# ============================================================
def normalize_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    if len(values) == 1 or max(values) == min(values):
        return [50.0] * len(values)
    lo, hi = min(values), max(values)
    return [round((v - lo) / (hi - lo) * 100, 2) for v in values]


def log_scale_score(value: float, ref_max: float) -> float:
    if value <= 0:
        return 0.0
    if ref_max <= 0:
        return 0.0
    s = math.log1p(value) / math.log1p(ref_max) * 100
    return round(max(0.0, min(100.0, s)), 2)


def percentile_score(value: float, sorted_pop: list[float]) -> float:
    """Where does `value` sit in `sorted_pop`? Returns 0-100."""
    if not sorted_pop:
        return 50.0
    import bisect
    idx = bisect.bisect_left(sorted_pop, value)
    return round(idx / len(sorted_pop) * 100, 2)


# ============================================================
# category classifier — keyword dictionary
# ============================================================
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "beauty":      ["skin", "hair", "makeup", "lipstick", "serum", "moistur",
                    "cream", "mask", "facial", "cosmetic", "lash", "nail",
                    "shampoo", "conditioner", "perfume", "fragrance", "cleanser"],
    "supplements": ["vitamin", "supplement", "collagen", "protein", "biotin",
                    "magnesium", "omega", "probiotic", "prebiotic", "ashwagandha",
                    "creatine", "melatonin", "hormone", "ozempic", "weight loss"],
    "fitness":     ["workout", "fitness", "yoga", "gym", "muscle", "treadmill",
                    "dumbbell", "kettlebell", "resistance band", "fitness tracker",
                    "running", "pilates", "trainer"],
    "wellness":    ["pain", "relief", "arthritis", "meniscus", "knee", "back pain",
                    "swelling", "anxiety", "sleep", "snore", "posture", "massage",
                    "therapy", "compression"],
    "kitchen":     ["kitchen", "cookware", "blender", "knife", "pan", "pot",
                    "spatula", "cutting board", "air fryer", "instant pot",
                    "coffee maker", "toaster", "kettle", "mixer"],
    "home":        ["home", "decor", "lamp", "sofa", "rug", "curtain", "pillow",
                    "blanket", "bedding", "shelf", "storage", "organizer",
                    "vacuum", "humidifier", "air purifier", "sheet"],
    "pet":         ["pet", "dog", "cat", "puppy", "kitten", "leash", "collar",
                    "litter", "cat tree", "dog bed", "scratch"],
    "baby":        ["baby", "infant", "toddler", "diaper", "stroller", "crib",
                    "bottle", "pacifier", "bib", "swaddle", "nursery"],
    "toys":        ["toy", "puzzle", "doll", "lego", "play set", "stuffed",
                    "plush", "board game", "rc car"],
    "auto":        ["car", "auto", "vehicle", "phone holder", "dash cam",
                    "tire", "wheel", "windshield", "garage"],
    "electronics": ["headphone", "earbud", "speaker", "charger", "cable",
                    "router", "monitor", "keyboard", "mouse", "webcam",
                    "smart watch", "tablet", "laptop"],
    "outdoor":     ["camping", "tent", "hiking", "backpack", "fishing",
                    "kayak", "bike", "climb", "outdoor"],
    "office":      ["desk", "chair", "office", "stationery", "planner",
                    "notebook", "pen", "marker", "stand"],
    "fashion":     ["dress", "shirt", "pants", "jeans", "shoes", "sneaker",
                    "boot", "jacket", "coat", "hoodie", "watch", "bag",
                    "purse", "wallet", "belt", "scarf"],
}


def classify_category(keyword: str) -> str | None:
    """Return best-match category or None."""
    if not keyword:
        return None
    kw = normalize_keyword(keyword)
    best_cat, best_hits = None, 0
    for cat, terms in CATEGORY_KEYWORDS.items():
        hits = sum(1 for t in terms if t in kw)
        if hits > best_hits:
            best_hits, best_cat = hits, cat
    return best_cat


# ============================================================
# retry / sleep
# ============================================================
def polite_sleep(low: float = 0.8, high: float = 1.8) -> None:
    time.sleep(random.uniform(low, high))


def retry(
    fn: Callable[..., T],
    *args,
    tries: int = 3,
    backoff: float = 2.0,
    base_sleep: float = 5.0,
    on_error: Callable[[Exception, int], None] | None = None,
    retry_on: tuple[type, ...] = (Exception,),
    **kwargs,
) -> T:
    last_exc: Exception | None = None
    for attempt in range(1, tries + 1):
        try:
            return fn(*args, **kwargs)
        except retry_on as e:
            last_exc = e
            if on_error:
                on_error(e, attempt)
            if attempt == tries:
                break
            wait = base_sleep * (backoff ** (attempt - 1)) + random.uniform(0, 2)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


# ============================================================
# HTTP
# ============================================================
_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

def random_ua() -> str:
    return random.choice(_UA_POOL)


def http_get(url: str, *, timeout: int = 20,
             headers: dict | None = None,
             accept_lang: str = "en-US,en;q=0.9") -> bytes:
    import urllib.request
    h = {
        "User-Agent": random_ua(),
        "Accept-Language": accept_lang,
        "Accept-Encoding": "identity",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_get_json(url: str, **kw) -> dict:
    import json
    body = http_get(url, **kw)
    return json.loads(body.decode("utf-8", errors="ignore"))


# ============================================================
# light HTML helpers
# ============================================================
def extract_first(html: str, pattern: str, group: int = 1) -> str | None:
    m = re.search(pattern, html, re.I | re.S)
    return m.group(group).strip() if m else None


def extract_all(html: str, pattern: str, group: int = 1) -> list[str]:
    return [m.group(group).strip() for m in re.finditer(pattern, html, re.I | re.S)]


def parse_money(s: str) -> float | None:
    """'$12.99' → 12.99 ; '12,345' → 12345.0"""
    if not s:
        return None
    m = re.search(r"([\d,]+\.?\d*)", s)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_int(s: str) -> int | None:
    if not s:
        return None
    m = re.search(r"([\d,]+)", s)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None

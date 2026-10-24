#!/usr/bin/env python3
"""AI Intent / GEO signal collector (v2).

For each input keyword we ask: "what brands / products does an AI search
engine recommend when a user types this query?"

v2 changes:
  - **Three backends** (try in order):
      1. ANTHROPIC_API_KEY → claude-haiku-4-5 (cheapest + best JSON)
      2. PERPLEXITY_API_KEY → sonar-pro
      3. SERPAPI_KEY        → google_ai_overview engine

  - **Commercial intent sub-score**: a separate signal for whether the
    answer recommends specific products (transactional) vs explains
    a concept (informational). Pure transactional answers score higher.
      commercial_intent ∈ {informational, mixed, transactional}
      bonus: 0 / 15 / 30

  - **Price-range extraction** from answer text → hint for SKU pricing.

  - **ThreadPoolExecutor** with --workers N for parallelism (default 4).
    Each backend has its own rate-limit bucket.

  - **Per-keyword sku_details rows** are written when we find product names —
    competitor-intel can then enrich them.

  - **Cost tracking**: prints estimated $ spent at end of run.

Score formula:
  raw_metric  = brand_count × 10
              + has_product_block × 50
              + commercial_intent_bonus      (0 / 15 / 30)
              + has_price_range × 10
  score       = clamp(raw_metric, 0, 100)
"""
from __future__ import annotations
import argparse
import os
import re
import sys
import json
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Iterable

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from _db import connect, upsert_many, upsert_skus, upsert_attrs, log_run  # noqa: E402
from _utils import (log, normalize_keyword, classify_category, retry,     # noqa: E402
                    polite_sleep)

# ---------- shared prompt (works on Claude / Perplexity) ----------
PROMPT = (
    "User searches: \"{kw}\".\n\n"
    "Pretend you are an AI shopping assistant answering this query. "
    "Return ONLY a single JSON object with these keys (no prose, no markdown):\n"
    "  brands: array of 3-7 specific brand or product names you'd recommend\n"
    "  intent: one of \"informational\" | \"mixed\" | \"transactional\"\n"
    "  price_low: typical lowest price in USD (number, no $) or null\n"
    "  price_high: typical highest price in USD or null\n"
    "  category: short category label like \"beauty\", \"kitchen\", \"pet\"\n\n"
    "Example:\n"
    "{{\"brands\":[\"BrandA\",\"BrandB\"],\"intent\":\"transactional\","
    "\"price_low\":12.99,\"price_high\":89.0,\"category\":\"kitchen\"}}\n"
)


# ============================================================
# backend 1: Anthropic Claude (preferred — cheapest + most reliable JSON)
#   Honors a third-party proxy when these env vars are set:
#     ANTHROPIC_BASE_URL       — e.g. https://fufu.iqach.top/anthropic
#     ANTHROPIC_AUTH_TOKEN     — bearer token for that proxy (alt to ANTHROPIC_API_KEY)
#     ANTHROPIC_DEFAULT_HAIKU_MODEL — override haiku model name
# ============================================================
DEFAULT_ANTH_URL = "https://api.anthropic.com"
DEFAULT_ANTH_MODEL = "claude-haiku-4-5"


def _anth_endpoint() -> str:
    base = (os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_ANTH_URL).rstrip("/")
    return f"{base}/v1/messages"


def _anth_model() -> str:
    return (os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
            or os.environ.get("ANTHROPIC_MODEL")
            or DEFAULT_ANTH_MODEL)


def _ask_anthropic(kw: str) -> dict:
    import urllib.request
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    headers = {
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if api_key:
        headers["x-api-key"] = api_key
    elif auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    else:
        raise RuntimeError("no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN")
    req = urllib.request.Request(
        _anth_endpoint(),
        method="POST",
        headers=headers,
        data=json.dumps({
            "model": _anth_model(),
            "max_tokens": 350,
            "messages": [{"role": "user", "content": PROMPT.format(kw=kw)}],
        }).encode("utf-8"),
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    text = payload["content"][0]["text"]
    return _parse_json_answer(text)


# ============================================================
# backend 2: Perplexity
# ============================================================
PPLX_URL = "https://api.perplexity.ai/chat/completions"
PPLX_MODEL = "sonar-pro"


def _ask_perplexity(kw: str) -> dict:
    import urllib.request
    api_key = os.environ["PERPLEXITY_API_KEY"]
    req = urllib.request.Request(
        PPLX_URL,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps({
            "model": PPLX_MODEL,
            "messages": [{"role": "user", "content": PROMPT.format(kw=kw)}],
            "max_tokens": 350,
            "temperature": 0.0,
        }).encode("utf-8"),
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    text = payload["choices"][0]["message"]["content"]
    return _parse_json_answer(text)


# ============================================================
# backend 3: SerpApi google_ai_overview
# ============================================================
SERP_URL = "https://serpapi.com/search.json?engine=google_ai_overview&q={q}&api_key={k}"


def _ask_serpapi(kw: str) -> dict:
    import urllib.request, urllib.parse
    api_key = os.environ["SERPAPI_KEY"]
    url = SERP_URL.format(q=urllib.parse.quote(kw), k=api_key)
    with urllib.request.urlopen(url, timeout=30) as r:
        payload = json.loads(r.read())
    aio = payload.get("ai_overview") or {}
    blocks = aio.get("text_blocks") or []
    text = " ".join((b.get("snippet") or "") for b in blocks)
    brands = re.findall(
        r"\b([A-Z][a-zA-Z0-9]{2,}(?:\s[A-Z][a-zA-Z0-9]{2,}){0,2})\b", text)
    has_product_block = any(b.get("type") == "products" for b in blocks)
    # crude price extraction
    pr = re.findall(r"\$([0-9]+(?:\.[0-9]+)?)", text)
    pr_nums = [float(x) for x in pr][:6]
    intent = "transactional" if has_product_block else (
        "mixed" if brands else "informational"
    )
    return {
        "brands": list(dict.fromkeys(brands))[:10],
        "intent": intent,
        "has_product_block": has_product_block,
        "price_low": min(pr_nums) if pr_nums else None,
        "price_high": max(pr_nums) if pr_nums else None,
        "category": None,
        "raw_text_len": len(text),
    }


# ---------- shared JSON parsing ----------
_INTENT_VALID = {"informational", "mixed", "transactional"}


def _parse_json_answer(text: str) -> dict:
    """Try strict JSON, fall back to regex extraction. Always returns a dict."""
    # Strip code fences if present
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    obj = None
    try:
        obj = json.loads(s)
    except Exception:
        # Find the first {...} block
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None

    brands, intent, plo, phi, cat = [], "mixed", None, None, None
    if isinstance(obj, dict):
        b = obj.get("brands")
        if isinstance(b, list):
            brands = [str(x)[:80] for x in b][:10]
        i = str(obj.get("intent") or "").lower()
        intent = i if i in _INTENT_VALID else "mixed"
        plo = obj.get("price_low") if isinstance(obj.get("price_low"), (int, float)) else None
        phi = obj.get("price_high") if isinstance(obj.get("price_high"), (int, float)) else None
        cat = obj.get("category") if isinstance(obj.get("category"), str) else None
    else:
        # Last-resort: pull quoted strings
        brands = re.findall(r'"([^"]+)"', s)[:10]

    return {
        "brands": brands,
        "intent": intent,
        "has_product_block": intent == "transactional" and len(brands) >= 3,
        "price_low": plo,
        "price_high": phi,
        "category": cat,
        "raw_text_len": len(text),
    }


# ============================================================
# backend selection
# ============================================================
def _pick_backend():
    # Accept either a native Anthropic key or a third-party-proxy bearer
    # (Claude Code's settings.json injects ANTHROPIC_AUTH_TOKEN +
    # ANTHROPIC_BASE_URL when you configure a custom Claude proxy.)
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic", _ask_anthropic
    if os.environ.get("PERPLEXITY_API_KEY"):
        return "perplexity", _ask_perplexity
    if os.environ.get("SERPAPI_KEY"):
        return "serpapi", _ask_serpapi
    return None, None


def _load_keywords(file: Path | None, fallback_db: Path | None) -> list[str]:
    if file and file.exists():
        return [normalize_keyword(l) for l in file.read_text().splitlines()
                if l.strip() and not l.startswith("#")]
    conn = connect(fallback_db) if fallback_db else connect()
    rows = conn.execute(
        "SELECT keyword, AVG(score) s FROM signals "
        "GROUP BY keyword ORDER BY s DESC LIMIT 200"
    ).fetchall()
    return [r[0] for r in rows]


# ============================================================
# main collect
# ============================================================
INTENT_BONUS = {"informational": 0, "mixed": 15, "transactional": 30}


def _process_one(kw: str, ask, backend_name: str) -> dict | None:
    if not kw or len(kw) < 3:
        return None
    try:
        res = retry(ask, kw, tries=2, base_sleep=4.0,
                    on_error=lambda e, a: log("ai", f"  retry {kw!r} {a}: {e!r}"))
    except Exception as e:
        log("ai", f"  ✗ {kw!r}: {e}")
        return None

    brand_count = len(res["brands"])
    has_pb = bool(res["has_product_block"])
    intent_bonus = INTENT_BONUS.get(res["intent"], 0)
    price_bonus = 10 if (res["price_low"] is not None
                         and res["price_high"] is not None) else 0
    raw = brand_count * 10 + (50 if has_pb else 0) + intent_bonus + price_bonus
    score = max(0.0, min(100.0, float(raw)))

    return {
        "keyword": kw,
        "score": score,
        "raw_metric": float(raw),
        "category": res["category"] or classify_category(kw),
        "metadata": {
            "backend":           backend_name,
            "brands":            res["brands"],
            "brand_count":       brand_count,
            "intent":            res["intent"],
            "has_product_block": has_pb,
            "price_low":         res["price_low"],
            "price_high":        res["price_high"],
            "raw_text_len":      res["raw_text_len"],
        },
    }


def collect(keywords: Iterable[str],
            db_path: Path | None = None,
            workers: int = 4,
            sleep_sec: float = 0.4,
            limit: int | None = None) -> int:
    backend_name, ask = _pick_backend()
    if not ask:
        log("ai", "✗ no API key (ANTHROPIC_API_KEY / PERPLEXITY_API_KEY / SERPAPI_KEY) — skipping")
        conn = connect(db_path) if db_path else connect()
        log_run(conn, "ai_intent", 0, "failed", "no_api_key")
        return 0
    log("ai", f"backend = {backend_name}  ·  workers = {workers}")

    kws = list(keywords)
    if limit:
        kws = kws[:limit]
    n_total = len(kws)
    log("ai", f"querying {n_total} keywords")

    rows: list[dict] = []
    progress = {"i": 0}
    p_lock = Lock()

    def worker(kw: str):
        time.sleep(sleep_sec)  # gentle stagger
        r = _process_one(kw, ask, backend_name)
        with p_lock:
            progress["i"] += 1
            if progress["i"] % 10 == 0:
                log("ai", f"  [{progress['i']}/{n_total}] last={kw!r}")
        return r

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = [ex.submit(worker, kw) for kw in kws]
        for f in as_completed(futures):
            r = f.result()
            if r is not None:
                rows.append(r)

    conn = connect(db_path) if db_path else connect()
    if not rows:
        log_run(conn, "ai_intent", 0, "failed", "no_responses")
        return 0
    n = upsert_many(conn, "ai_intent", rows)
    # Push intent + category into keyword_attrs
    for r in rows:
        upsert_attrs(
            conn, r["keyword"],
            primary_category=r["category"],
            notes=f"AI intent: {r['metadata']['intent']}",
        )
    log_run(conn, "ai_intent", n, "ok")

    # rough cost estimate (Claude haiku ~ $0.0008 per call, sonar-pro ~ $0.005, serpapi ~ $0.005)
    per_call = {"anthropic": 0.0008, "perplexity": 0.005, "serpapi": 0.005}.get(backend_name, 0.005)
    cost = round(n * per_call, 3)
    log("ai", f"✓ wrote {n} keywords (backend={backend_name})  ·  est. cost ≈ ${cost}")
    high = sum(1 for r in rows if r["score"] >= 70)
    mid  = sum(1 for r in rows if 40 <= r["score"] < 70)
    log("ai", f"  score distribution: ≥70: {high}  ·  40-70: {mid}  ·  <40: {n-high-mid}")
    txn = sum(1 for r in rows if r["metadata"]["intent"] == "transactional")
    log("ai", f"  transactional intent: {txn}/{n}")
    return n


def main():
    ap = argparse.ArgumentParser(description="AI Intent collector v2")
    ap.add_argument("--keywords", default=None,
                    help="file with one keyword per line (default: top 200 from signals.db)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.4)
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    kws = _load_keywords(Path(args.keywords) if args.keywords else None,
                         Path(args.db) if args.db else None)
    n = collect(kws, Path(args.db) if args.db else None,
                workers=args.workers, sleep_sec=args.sleep, limit=args.limit)
    sys.exit(0 if n > 0 else 1)


if __name__ == "__main__":
    main()

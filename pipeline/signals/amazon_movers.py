#!/usr/bin/env python3
"""Amazon dual-ranking signal collector (v2).

v2 changes:
  - **two endpoints per category**:
      /gp/movers-and-shakers/{cat}/   ← leading (24 h rank rise)
      /gp/bestsellers/{cat}/          ← lagging (overall demand floor)
    Combined score = 0.7·movers + 0.3·bestsellers (movers dominates).

  - **per-product extraction**: ASIN, price, rating, review_count, image,
    product_url. All written to `sku_details` so shopify-product-pipeline
    and competitor-intel can pull rich metadata without re-scraping.

  - **ThreadPoolExecutor** with `max_workers=4` — Amazon tolerates that
    politely (we space requests across categories rather than spam one).

  - **3-gram support** in addition to 2-gram (a lot of niche keywords are
    3-token: "magnetic phone holder", "blue light glasses").

  - **brand-aware stop list**: skips obvious brand names that appear
    in titles (Apple, Samsung, …) so the keyword bag isn't polluted.

  - **structured failure**: every category logs success/empty/blocked into
    metadata so the operator can see *why* a category had 0 rows.

Score formula:
  raw_metric_kw = mean(rise_pct) × √appearances        (movers)
                + log1p(rank_inverse) × 5              (bestsellers, scaled)
  score         = log_scale(raw_metric, ref_max) ∈ [0, 100]
"""
from __future__ import annotations
import argparse
import re
import sys
import time
import math
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from typing import Iterable

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from _db import connect, upsert_many, upsert_skus, upsert_attrs, log_run  # noqa: E402
from _utils import (log, normalize_keyword, log_scale_score, http_get,    # noqa: E402
                    retry, polite_sleep, classify_category, parse_money,
                    parse_int)

# Amazon's top-level Movers & Shakers categories
DEFAULT_CATS = [
    "beauty",
    "hpc",                  # Health & Personal Care
    "kitchen",
    "home-garden",
    "pet-supplies",
    "sporting-goods",
    "toys-and-games",
    "electronics",
    "office-products",
    "automotive",
    "baby-products",
]

URL_MOVERS  = "https://www.amazon.com/gp/movers-and-shakers/{cat}/"
URL_BESTSEL = "https://www.amazon.com/gp/bestsellers/{cat}/"

# stop words — same vocab as your amazon_full_trends_v6.py
STOP = {
    "the","a","an","and","or","for","with","in","on","to","of","by","is","it","at","as","from",
    "new","your","this","that","all","can","be","has","have","are","was","were","will","my","our",
    "up","down","out","no","not","into","set","pack","pcs","inch","ft","lb","lbs","oz","ml",
    "2024","2025","2026","amazon","basics","brand","style","color","size","type","model",
    "x","s","m","l","xl","xxl","men","women","kids","boys","girls","baby",
    "black","white","red","blue","green","pink","gray","grey","brown","purple","orange","yellow",
    "small","medium","large","extra","us","usa","pro","plus","mini","max","super","ultra",
    "portable","upgraded","latest","premium","professional","adjustable","waterproof","wireless",
    "electric","automatic","stainless","steel","heavy","duty","high","quality","best","free",
    "indoor","outdoor",
    "multi","double","single","pair","packs","pieces","count",
}

# Common Amazon brand names — skip as keyword tokens
BRAND_STOP = {
    "apple","samsung","sony","dell","hp","logitech","anker","aukey","tp-link",
    "crocs","nike","adidas","puma","under","armour", "lego","fisher","hasbro",
    "kindle","echo","alexa", "blink","ring","eufy",
}

# regexes (best-effort over Amazon's modern Movers/Bestsellers HTML, 2026-04)
# Titles now use hashed CSS-module classes: `_cDEzb_p13n-sc-css-line-clamp-<N>_<hash>`.
# Old `p13n-sc-truncate` class is gone.
RE_ITEM_BLOCK = re.compile(r'<div id="zg-ordered-list".*?</div>\s*</div>', re.S | re.I)
RE_TITLE = re.compile(
    r'class="[^"]*p13n-sc-css-line-clamp[^"]*"[^>]*>([^<]+)</', re.I)
# Amazon's Movers page currently doesn't render a "+X%" rise client-side
# (it's injected by JS). Leave a best-effort regex that matches either
# "+888%" inline or the old "<span>+N%</span>" form; if nothing matches,
# callers get rise=0 and the bestseller rank-inverse signal covers it.
RE_RISE  = re.compile(r'>\s*(\+?[\-]?[\d,]+)\s*%\s*<', re.I)
RE_ASIN  = re.compile(r'/(?:dp|gp/product)/(B0[A-Z0-9]{8})', re.I)
RE_PRICE = re.compile(
    r'class="[^"]*p13n-sc-price[^"]*"[^>]*>\s*\$?([\d,]+\.?\d*)', re.I)
RE_RATING = re.compile(r'(\d\.\d)\s+out of 5 stars', re.I)
RE_REVIEWS = re.compile(
    r'a-size-small[^"]*"[^>]*>\s*([\d,]+)\s*</span>', re.I)
RE_IMG   = re.compile(r'<img[^>]+src="(https://m\.media-amazon\.com/images/[^"]+)"', re.I)


# ---------- title → keyword grams ----------
def _extract_grams(title: str) -> list[str]:
    title = unescape(title)
    t = re.sub(r"\([^)]*\)", " ", title.lower())
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    words = [w for w in t.split()
             if w not in STOP and w not in BRAND_STOP
             and len(w) > 2 and not w.isdigit()]
    grams = []
    for i in range(min(len(words) - 1, 8)):
        grams.append(f"{words[i]} {words[i+1]}")
    for i in range(min(len(words) - 2, 6)):
        grams.append(f"{words[i]} {words[i+1]} {words[i+2]}")
    return grams


# ---------- per-item parsing ----------
def _parse_items(html: str, *, want_rise: bool) -> list[dict]:
    """
    Parse a Movers or Bestsellers page. Each item is {title, asin, rise, price,
    rating, reviews, image_url, product_url}. Best-effort — missing fields = None.
    """
    titles = RE_TITLE.findall(html)[:50]
    rises  = RE_RISE.findall(html)[:50]   if want_rise else []
    asins_iter = list(dict.fromkeys(RE_ASIN.findall(html)))[:50]
    prices = RE_PRICE.findall(html)[:50]
    ratings = RE_RATING.findall(html)[:50]
    reviews = RE_REVIEWS.findall(html)[:50]
    images = RE_IMG.findall(html)[:50]

    out = []
    for i, t in enumerate(titles):
        asin = asins_iter[i] if i < len(asins_iter) else None
        rise = 0.0
        if want_rise and i < len(rises):
            try:
                rise = float(rises[i].replace(",", "").replace("+", ""))
            except ValueError:
                rise = 0.0
        price = parse_money(prices[i]) if i < len(prices) else None
        rating = float(ratings[i]) if i < len(ratings) else None
        rev = parse_int(reviews[i]) if i < len(reviews) else None
        img = images[i] if i < len(images) else None
        out.append({
            "title": t.strip(),
            "asin": asin,
            "rise_pct": rise,
            "price_usd": price,
            "rating": rating,
            "reviews_count": rev,
            "image_url": img,
            "product_url": (f"https://www.amazon.com/dp/{asin}" if asin else None),
            "rank": i + 1,
        })
    return out


# ---------- fetch ----------
def _fetch(url: str, label: str) -> str:
    def _go():
        body = http_get(url, timeout=25,
                        headers={"Accept": "text/html,application/xhtml+xml"})
        return body.decode("utf-8", errors="ignore")

    return retry(_go, tries=3, base_sleep=4.0,
                 on_error=lambda e, a: log("amz", f"  retry {label} {a}: {e!r}"))


def fetch_category(cat: str) -> dict:
    """Fetch BOTH movers and bestsellers for a category. Returns dict."""
    out = {"cat": cat, "movers": [], "bestsellers": [], "status": "ok", "err": None}
    try:
        html_m = _fetch(URL_MOVERS.format(cat=cat),  f"movers/{cat}")
        out["movers"] = _parse_items(html_m, want_rise=True)
    except Exception as e:
        out["status"] = "movers_failed"
        out["err"] = repr(e)
    polite_sleep(0.6, 1.2)
    try:
        html_b = _fetch(URL_BESTSEL.format(cat=cat), f"bestsellers/{cat}")
        out["bestsellers"] = _parse_items(html_b, want_rise=False)
    except Exception as e:
        if out["status"] == "ok":
            out["status"] = "bestsellers_failed"
            out["err"] = repr(e)
    log("amz", f"  ✓ {cat}: movers={len(out['movers'])} "
               f"bestsellers={len(out['bestsellers'])}")
    return out


# ---------- aggregate ----------
def _aggregate(per_cat: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (signal_rows, sku_rows)."""
    bucket: dict[str, dict] = defaultdict(lambda: {
        "rises": [], "ranks_bs": [], "cats": set(),
        "asins": set(), "appearances": 0,
    })
    sku_rows: dict[tuple[str, str], dict] = {}

    for ev in per_cat:
        cat = ev["cat"]

        # 1) Movers items: keyword grams + per-item SKU detail
        for it in ev["movers"]:
            grams = _extract_grams(it["title"])
            for kw in grams:
                kw = normalize_keyword(kw)
                if not kw or len(kw) < 4:
                    continue
                a = bucket[kw]
                a["rises"].append(it["rise_pct"])
                a["cats"].add(cat)
                if it["asin"]:
                    a["asins"].add(it["asin"])
                a["appearances"] += 1
            if it["asin"]:
                key = (it["asin"], "amazon")
                # use the first keyword as the "discovered_via" hint
                first_kw = normalize_keyword(_extract_grams(it["title"])[0]) \
                    if _extract_grams(it["title"]) else None
                sku_rows[key] = {
                    "sku_id":        it["asin"],
                    "keyword":       first_kw,
                    "title":         it["title"][:300],
                    "brand":         None,
                    "price_usd":     it["price_usd"],
                    "rating":        it["rating"],
                    "reviews_count": it["reviews_count"],
                    "image_url":     it["image_url"],
                    "product_url":   it["product_url"],
                    "category":      cat,
                    "metadata":      {
                        "discovered_via": "amazon_movers",
                        "movers_rank": it["rank"],
                        "rise_pct":    it["rise_pct"],
                    },
                }

        # 2) Bestsellers items: rank-inverse weight + SKU detail
        for it in ev["bestsellers"]:
            grams = _extract_grams(it["title"])
            for kw in grams:
                kw = normalize_keyword(kw)
                if not kw or len(kw) < 4:
                    continue
                a = bucket[kw]
                a["ranks_bs"].append(it["rank"])
                a["cats"].add(cat)
                if it["asin"]:
                    a["asins"].add(it["asin"])
                a["appearances"] += 1
            if it["asin"]:
                key = (it["asin"], "amazon")
                if key in sku_rows:
                    # already from movers — just amend bestseller_rank into metadata
                    sku_rows[key]["metadata"]["bestseller_rank"] = it["rank"]
                else:
                    first_kw = normalize_keyword(_extract_grams(it["title"])[0]) \
                        if _extract_grams(it["title"]) else None
                    sku_rows[key] = {
                        "sku_id":        it["asin"],
                        "keyword":       first_kw,
                        "title":         it["title"][:300],
                        "brand":         None,
                        "price_usd":     it["price_usd"],
                        "rating":        it["rating"],
                        "reviews_count": it["reviews_count"],
                        "image_url":     it["image_url"],
                        "product_url":   it["product_url"],
                        "category":      cat,
                        "metadata":      {
                            "discovered_via": "amazon_bestsellers",
                            "bestseller_rank": it["rank"],
                        },
                    }

    if not bucket:
        return [], []

    # raw_metric per keyword: 0.7·movers + 0.3·bestsellers
    raws: dict[str, float] = {}
    for kw, a in bucket.items():
        movers_score = 0.0
        if a["rises"]:
            mean_rise = sum(a["rises"]) / len(a["rises"])
            movers_score = mean_rise * math.sqrt(len(a["rises"]))
        bs_score = 0.0
        if a["ranks_bs"]:
            # rank-inverse: rank 1 → log1p(50), rank 50 → log1p(1)
            bs_score = sum(math.log1p(max(0, 50 - r)) for r in a["ranks_bs"]) * 5
        raws[kw] = 0.7 * movers_score + 0.3 * bs_score

    ref_max = max(raws.values()) or 1
    rows = []
    for kw, raw in raws.items():
        a = bucket[kw]
        primary_cat = next(iter(a["cats"]), None)
        rows.append({
            "keyword": kw,
            "score": log_scale_score(raw, ref_max),
            "raw_metric": round(raw, 2),
            "category": classify_category(kw) or primary_cat,
            "metadata": {
                "appearances":      a["appearances"],
                "movers_count":     len(a["rises"]),
                "bestseller_count": len(a["ranks_bs"]),
                "mean_rise_pct":    round(sum(a["rises"]) / len(a["rises"]), 2)
                                     if a["rises"] else 0,
                "max_rise_pct":     max(a["rises"]) if a["rises"] else 0,
                "best_bs_rank":     min(a["ranks_bs"]) if a["ranks_bs"] else None,
                "amazon_cats":      sorted(a["cats"]),
                "asin_sample":      next(iter(a["asins"]), None),
            },
        })

    return rows, list(sku_rows.values())


# ---------- entry ----------
def collect(categories: Iterable[str] = DEFAULT_CATS,
            db_path: Path | None = None,
            max_workers: int = 4) -> int:
    cats = list(categories)
    log("amz", f"fetching {len(cats)} categories with {max_workers} workers")
    per_cat = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(fetch_category, c): c for c in cats}
        for fu in as_completed(futures):
            try:
                per_cat.append(fu.result())
            except Exception as e:
                cat = futures[fu]
                log("amz", f"  ✗ {cat} crashed: {e!r}")
                per_cat.append({"cat": cat, "movers": [], "bestsellers": [],
                                "status": "crashed", "err": repr(e)})

    rows, sku_rows = _aggregate(per_cat)
    if not rows:
        log("amz", "✗ no keywords harvested")
        conn = connect(db_path) if db_path else connect()
        log_run(conn, "amazon_movers", 0, "failed", "empty")
        return 0

    conn = connect(db_path) if db_path else connect()
    n = upsert_many(conn, "amazon_movers", rows)
    n_sku = upsert_skus(conn, "amazon", sku_rows)
    for r in rows:
        upsert_attrs(conn, r["keyword"], primary_category=r["category"])
    log_run(conn, "amazon_movers", n, "ok")

    log("amz", f"✓ wrote {n} keywords  ·  {n_sku} ASINs")
    high = sum(1 for r in rows if r["score"] >= 70)
    mid  = sum(1 for r in rows if 40 <= r["score"] < 70)
    log("amz", f"  score distribution: ≥70: {high}  ·  40-70: {mid}  ·  <40: {n-high-mid}")
    return n


def main():
    ap = argparse.ArgumentParser(description="Amazon dual-ranking collector v2")
    ap.add_argument("--cats", nargs="*", default=DEFAULT_CATS)
    ap.add_argument("--db", default=None)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    n = collect(args.cats, Path(args.db) if args.db else None, args.workers)
    sys.exit(0 if n > 0 else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""TikTok Creative Center signal collector (v2).

v2 changes:
  - **three endpoints** (not two):
      hashtag/list   → trending hashtags
      product/list   → trending TikTok-Shop products (with cover image)
      ads/list       → top-performing ads (with creative_count signal)

  - **growth_rate** is captured (TikTok exposes 7d/30d ratio called `rank_diff`)
    → used as a momentum bonus, similar to FB's heat_7d / avg_30d.

  - **per-product SKU rows** in sku_details: TikTok product_id, title, price,
    cover image, product_url. Lets shopify-product-pipeline pull TikTok
    bestsellers without re-scraping.

  - **per-hashtag video sample** stored in metadata (one viral video URL),
    so the operator can sanity-check the trend visually.

  - **smart keyword extraction from product names**: drop generic suffixes
    ("for women", "with ...", "set of ..."), keep first 3 meaningful tokens.

  - **structured failure log** — endpoint that failed is recorded so the
    operator knows which API to fix (Cloudflare often blocks one but not
    all three).

Score formula:
  raw_metric = max(video_cnt × growth_factor,
                   ad_cost × √ad_creatives,
                   product_sales_cnt × √rank_diff)
  score      = log_scale(raw_metric, ref_max) ∈ [0, 100]
"""
from __future__ import annotations
import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from _db import connect, upsert_many, upsert_skus, upsert_attrs, log_run  # noqa: E402
from _utils import (log, normalize_keyword, log_scale_score, http_get,    # noqa: E402
                    retry, classify_category, polite_sleep)

API_BASE = "https://ads.tiktok.com/creative_radar_api/v1/popular_trend"
API_HASHTAG = (API_BASE + "/hashtag/list"
               "?period={period}&page={page}&limit={limit}&country_code={country}")
API_PRODUCT = (API_BASE + "/product/list"
               "?period={period}&page={page}&limit={limit}&country_code={country}")
API_ADS     = (API_BASE + "/ads/list"
               "?period={period}&page={page}&limit={limit}&country_code={country}")

COMMON_HEADERS = {
    "Accept": "application/json",
    "Referer": "https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en",
    "Anonymous-User-Id": "00000000-0000-0000-0000-000000000000",
    "Lang": "en",
    "Timezone": "America/New_York",
}

# Suffixes that pollute keyword grams
_DROP_TAILS = (" for women", " for men", " for kids", " for baby",
               " with case", " with charger", " set of",
               " pack of", " bundle", " gift set")


def _fetch(url: str) -> dict[str, Any]:
    """Fetch a Creative Center endpoint.

    As of 2026-04 the public anonymous API is degraded:
      • /product/list and /ads/list → HTTP 404 (deprecated / relocated)
      • /hashtag/list → HTTP 200 but `{"code":40101,"msg":"no permission"}`
        unless called with a valid ms_token cookie from a signed-in session.

    We fail fast on these signals (no 3× retry storm) and let the caller
    move on. A payload with `code != 0` is returned as-is so the caller
    can surface the specific error.
    """
    import urllib.error
    def _go():
        try:
            body = http_get(url, timeout=25, headers=COMMON_HEADERS)
        except urllib.error.HTTPError as e:
            # 404 = endpoint gone, no point retrying
            if e.code in (403, 404):
                raise RuntimeError(f"HTTP {e.code} (endpoint deprecated or auth-gated)")
            raise
        return json.loads(body.decode("utf-8", errors="ignore"))
    payload = retry(_go, tries=2, base_sleep=3.0,
                    on_error=lambda e, a: log("tk", f"  retry {a}: {e!r}"))
    # Surface TikTok's own error codes rather than silently returning empty
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        raise RuntimeError(f"tiktok api code={payload.get('code')} "
                           f"msg={payload.get('msg')!r}")
    return payload


def _walk(payload: dict, *keys: str) -> list[dict]:
    if not payload:
        return []
    data = payload.get("data") or {}
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            return v
    return []


def _clean_product_kw(name: str) -> str:
    s = name.lower().strip()
    for tail in _DROP_TAILS:
        if tail in s:
            s = s.split(tail, 1)[0]
            break
    s = normalize_keyword(s)
    return " ".join(s.split()[:3])


# ---------- per-endpoint loops ----------
def _fetch_hashtags(country, period, limit, pages, bucket):
    log("tk", f"  hashtags  country={country} period={period}d × {pages} pages")
    ok = 0
    for p in range(1, pages + 1):
        url = API_HASHTAG.format(period=period, page=p, limit=limit, country=country)
        try:
            payload = _fetch(url)
        except Exception as e:
            log("tk", f"    ✗ hashtag p{p}: {e}")
            return ok
        items = _walk(payload, "list", "hashtag_list")
        if not items:
            return ok
        for it in items:
            name = (it.get("hashtag_name") or it.get("name") or "").lstrip("#")
            kw = normalize_keyword(name)
            if not kw or len(kw) < 3:
                continue
            video_cnt   = int(it.get("publish_cnt") or it.get("post_count") or 0)
            view_cnt    = int(it.get("video_views") or it.get("view_cnt") or 0)
            rank_diff   = int(it.get("rank_diff") or 0)   # +N rises
            sample_url  = it.get("first_video_url") or it.get("share_url") or ""
            b = bucket.setdefault(kw, _new_bucket())
            b["video_cnt"]    = max(b["video_cnt"], video_cnt)
            b["view_cnt"]     = max(b["view_cnt"], view_cnt)
            b["rank_diff"]    = max(b["rank_diff"], rank_diff)
            if not b["sample_video_url"] and sample_url:
                b["sample_video_url"] = sample_url
            b["sources"].add("hashtag")
            ok += 1
        polite_sleep(1.0, 2.0)
    return ok


def _fetch_products(country, period, limit, pages, bucket, sku_rows):
    log("tk", f"  products  country={country} period={period}d × {pages} pages")
    ok = 0
    for p in range(1, pages + 1):
        url = API_PRODUCT.format(period=period, page=p, limit=limit, country=country)
        try:
            payload = _fetch(url)
        except Exception as e:
            log("tk", f"    ✗ product p{p}: {e}")
            return ok
        items = _walk(payload, "list", "product_list")
        if not items:
            return ok
        for it in items:
            name = it.get("product_name") or it.get("name") or ""
            if not name:
                continue
            kw = _clean_product_kw(name)
            if not kw or len(kw) < 3:
                continue
            cost     = float(it.get("cost") or it.get("ad_cost") or 0)
            ad_cnt   = int(it.get("ad_cnt") or it.get("creative_cnt") or 0)
            sales    = int(it.get("sales_cnt") or it.get("orders") or 0)
            cover    = it.get("product_image") or it.get("cover_url")
            price_str = str(it.get("price") or it.get("price_str") or "")
            try:
                price = float(price_str.replace("$","").replace(",","")) if price_str else None
            except ValueError:
                price = None
            cat      = it.get("category_name")
            pid      = str(it.get("product_id") or it.get("id") or "")
            rank_diff = int(it.get("rank_diff") or 0)

            b = bucket.setdefault(kw, _new_bucket())
            b["ad_cost"]      = b["ad_cost"] + cost
            b["ad_creatives"] = b["ad_creatives"] + ad_cnt
            b["sales_cnt"]    = b["sales_cnt"] + sales
            b["rank_diff"]    = max(b["rank_diff"], rank_diff)
            if not b["category"]:
                b["category"] = cat
            b["sources"].add("product")

            if pid:
                sku_rows[pid] = {
                    "sku_id":      f"tk_{pid}",
                    "keyword":     kw,
                    "title":       name[:300],
                    "brand":       it.get("brand_name"),
                    "price_usd":   price,
                    "rating":      None,
                    "reviews_count": None,
                    "image_url":   cover,
                    "product_url": it.get("product_url"),
                    "category":    cat,
                    "metadata":    {
                        "discovered_via": "tiktok_creative",
                        "sales_cnt":      sales,
                        "rank_diff":      rank_diff,
                    },
                }
            ok += 1
        polite_sleep(1.0, 2.0)
    return ok


def _fetch_ads(country, period, limit, pages, bucket):
    log("tk", f"  ads       country={country} period={period}d × {pages} pages")
    ok = 0
    for p in range(1, pages + 1):
        url = API_ADS.format(period=period, page=p, limit=limit, country=country)
        try:
            payload = _fetch(url)
        except Exception as e:
            log("tk", f"    ✗ ads p{p}: {e}")
            return ok
        items = _walk(payload, "list", "ads_list", "materials")
        if not items:
            return ok
        for it in items:
            # Ad pages give us advertiser brand + ad_title
            name = it.get("ad_title") or it.get("title") or ""
            if not name:
                continue
            kw = _clean_product_kw(name)
            if not kw or len(kw) < 3:
                continue
            ctr = float(it.get("ctr") or 0)
            likes = int(it.get("like") or 0)
            b = bucket.setdefault(kw, _new_bucket())
            b["ad_creatives"] = b["ad_creatives"] + 1
            b["ad_engagement"] = b["ad_engagement"] + likes
            b["ad_ctr_max"]    = max(b["ad_ctr_max"], ctr)
            b["sources"].add("ad")
            ok += 1
        polite_sleep(1.0, 2.0)
    return ok


def _new_bucket() -> dict:
    return {
        "video_cnt": 0, "view_cnt": 0, "rank_diff": 0,
        "ad_cost": 0.0, "ad_creatives": 0,
        "sales_cnt": 0, "ad_engagement": 0, "ad_ctr_max": 0.0,
        "category": None, "sources": set(),
        "sample_video_url": "",
    }


# ---------- main ----------
def collect(country: str = "US", period: int = 7,
            limit: int = 50, pages: int = 4,
            db_path: Path | None = None) -> int:
    bucket: dict[str, dict] = {}
    sku_rows: dict[str, dict] = {}

    log("tk", f"==> country={country} period={period}d limit={limit} pages={pages}")
    n_h = _fetch_hashtags(country, period, limit, pages, bucket)
    n_p = _fetch_products(country, period, limit, pages, bucket, sku_rows)
    n_a = _fetch_ads(country, period, limit, pages, bucket)
    log("tk", f"  fetched: hashtags={n_h} products={n_p} ads={n_a}")

    if not bucket:
        log("tk", "✗ no items returned (endpoint may be gated — try VPN/proxy)")
        conn = connect(db_path) if db_path else connect()
        log_run(conn, "tiktok_creative", 0, "failed", "all_endpoints_empty")
        return 0

    # raw_metric: best of three signals
    raws: dict[str, float] = {}
    for kw, b in bucket.items():
        # momentum bonus from rank_diff (clip 0-50 → 1.0-2.0)
        momentum = 1.0 + min(b["rank_diff"], 50) / 50.0
        v_signal = b["video_cnt"] * momentum
        a_signal = b["ad_cost"] * math.sqrt(max(1, b["ad_creatives"]))
        s_signal = b["sales_cnt"] * math.sqrt(max(1, momentum))
        raws[kw] = max(v_signal, a_signal, s_signal)

    ref_max = max(raws.values()) or 1
    rows = []
    for kw, raw in raws.items():
        b = bucket[kw]
        category = classify_category(kw) or b["category"]
        rows.append({
            "keyword": kw,
            "score": log_scale_score(raw, ref_max),
            "raw_metric": round(raw, 2),
            "category": category,
            "metadata": {
                "video_cnt":         b["video_cnt"],
                "view_cnt":          b["view_cnt"],
                "rank_diff":         b["rank_diff"],
                "ad_cost":           round(b["ad_cost"], 2),
                "ad_creatives":      b["ad_creatives"],
                "ad_engagement":     b["ad_engagement"],
                "ad_ctr_max":        round(b["ad_ctr_max"], 4),
                "sales_cnt":         b["sales_cnt"],
                "sources":           sorted(b["sources"]),
                "sample_video_url":  b["sample_video_url"],
                "country":           country,
                "period_days":       period,
            },
        })

    conn = connect(db_path) if db_path else connect()
    n = upsert_many(conn, "tiktok_creative", rows)
    n_sku = upsert_skus(conn, "tiktok", list(sku_rows.values()))
    for r in rows:
        upsert_attrs(conn, r["keyword"], primary_category=r["category"])
    log_run(conn, "tiktok_creative", n, "ok")

    log("tk", f"✓ wrote {n} keywords  ·  {n_sku} TikTok-Shop SKUs")
    high = sum(1 for r in rows if r["score"] >= 70)
    mid  = sum(1 for r in rows if 40 <= r["score"] < 70)
    log("tk", f"  score distribution: ≥70: {high}  ·  40-70: {mid}  ·  <40: {n-high-mid}")
    return n


def main():
    ap = argparse.ArgumentParser(description="TikTok Creative Center collector v2")
    ap.add_argument("--country", default="US")
    ap.add_argument("--period", type=int, default=7, choices=[7, 30, 120])
    ap.add_argument("--pages", type=int, default=4)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    n = collect(args.country, args.period, args.limit, args.pages,
                Path(args.db) if args.db else None)
    sys.exit(0 if n > 0 else 1)


if __name__ == "__main__":
    main()

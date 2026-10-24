#!/usr/bin/env python3
"""FB Ads signal collector (v2).

v2 changes:
  - capture every numeric field on the master sheet:
      广告变体数, 粉丝数, 近7天热度, 近30天均值, 跨page投放, 策略角度
  - composite score = log(ad_variants) × cross_page_bonus × momentum_factor
      where momentum_factor = clip(7d / 30d_avg, 0.5, 2.0)
  - secondary site_count metric: how many distinct landing pages run the
    same keyword → multi-advertiser consensus signal
  - per-keyword brand list (top 5 brands writing the same keyword)
  - emits keyword_attrs hints (primary_category via classify_category)
  - landing-page URLs go into sku_details under source='fb_landing' so
    competitor-intel and shopify-product-pipeline can pull them later

Score components written to metadata:
  ad_variants, fan_count, last_7d_heat, last_30d_avg, momentum,
  cross_page_count, site_count, top_brands, strategy_angles, sample_url
"""
from __future__ import annotations
import os
import argparse
import sys
import re
from pathlib import Path
from collections import Counter, defaultdict

try:
    import openpyxl
except ImportError:
    sys.exit("pip install openpyxl")

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from _db import connect, upsert_many, upsert_skus, upsert_attrs, log_run  # noqa: E402
from _utils import (log, normalize_keyword, log_scale_score,               # noqa: E402
                    classify_category, parse_int)

DEFAULT_XLSX = Path(os.getenv("FB_KEYWORDS_XLSX",
    str(Path(__file__).resolve().parents[2] / "facebook-ad-keywords-master.xlsx")))

# Aliases — any header name in this set wins.
COL_ALIASES = {
    "keyword":     {"关键词", "搜索关键词", "keyword", "kw", "词", "搜索词"},
    "ad_count":    {"广告数", "广告变体数", "变体数", "ad_count", "ads", "条数"},
    "url":         {"独立站", "独立站网址", "url", "domain", "site", "网址", "landing"},
    "brand":       {"品牌", "品牌名", "brand"},
    "fans":        {"粉丝数", "fans", "followers"},
    "heat_7d":     {"近7天热度", "近七天热度", "7d_heat", "heat_7d"},
    "avg_30d":     {"近30天均值", "近三十天均值", "30d_avg", "avg_30d"},
    "trend":       {"趋势", "trend", "direction"},
    "strategy":    {"策略角度", "strategy", "angle"},
    "cross_page":  {"跨page投放", "跨page", "cross_page", "multi_page"},
    "date":        {"最新出现", "查询日期", "last_seen", "更新时间", "fetched_at"},
}


def _find_col(header: list, aliases: set[str]) -> int | None:
    al_lower = {a.lower() for a in aliases}
    for i, h in enumerate(header):
        if h is None:
            continue
        if str(h).strip().lower() in al_lower:
            return i
    return None


def _read_int(cell) -> int:
    if cell is None or cell == "":
        return 0
    if isinstance(cell, (int, float)):
        return int(cell)
    return parse_int(str(cell)) or 0


def _read_float(cell) -> float:
    if cell is None or cell == "":
        return 0.0
    try:
        return float(cell)
    except (TypeError, ValueError):
        return float(parse_int(str(cell)) or 0)


def _read_bool(cell) -> bool:
    if cell is None or cell == "":
        return False
    s = str(cell).strip().lower()
    return s in {"1", "true", "yes", "y", "是", "✓", "√"}


def _iter_rows(xlsx_path: Path):
    wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        try:
            header = list(next(rows))
        except StopIteration:
            continue
        idx = {k: _find_col(header, v) for k, v in COL_ALIASES.items()}
        if idx["keyword"] is None:
            continue

        for r in rows:
            if not r or r[idx["keyword"]] is None:
                continue
            kw = normalize_keyword(str(r[idx["keyword"]]))
            if not kw or len(kw) < 3:
                continue
            yield {
                "sheet":      ws.title,
                "keyword":    kw,
                "ad_count":   _read_int(r[idx["ad_count"]])  if idx["ad_count"]  is not None else 0,
                "url":        (str(r[idx["url"]]).strip()    if idx["url"] is not None and r[idx["url"]] else ""),
                "brand":      (str(r[idx["brand"]]).strip()  if idx["brand"] is not None and r[idx["brand"]] else ""),
                "fans":       _read_int(r[idx["fans"]])      if idx["fans"]     is not None else 0,
                "heat_7d":    _read_float(r[idx["heat_7d"]]) if idx["heat_7d"]  is not None else 0.0,
                "avg_30d":    _read_float(r[idx["avg_30d"]]) if idx["avg_30d"]  is not None else 0.0,
                "trend":      (str(r[idx["trend"]]).strip()  if idx["trend"] is not None and r[idx["trend"]] else ""),
                "strategy":   (str(r[idx["strategy"]]).strip() if idx["strategy"] is not None and r[idx["strategy"]] else ""),
                "cross_page": _read_bool(r[idx["cross_page"]]) if idx["cross_page"] is not None else False,
            }


# ---------- normalize URL → site_root ----------
_RE_DOMAIN = re.compile(r"https?://(?:www\.)?([^/?#]+)", re.I)

def _site_of(url: str) -> str:
    if not url:
        return ""
    m = _RE_DOMAIN.search(url)
    return m.group(1).lower() if m else url.lower().split("/")[0]


def collect(xlsx_path: Path, db_path: Path | None = None) -> int:
    if not xlsx_path.exists():
        log("fb", f"✗ master xlsx not found: {xlsx_path}")
        return 0

    log("fb", f"reading {xlsx_path.name}")
    raw = list(_iter_rows(xlsx_path))
    if not raw:
        log("fb", "✗ no recognizable rows")
        return 0
    log("fb", f"  parsed {len(raw)} raw rows across all sheets")

    # Aggregate per-keyword
    agg: dict[str, dict] = defaultdict(lambda: {
        "ad_count_sum": 0, "ad_count_max": 0,
        "fans_sum": 0, "fans_max": 0,
        "heat_7d_sum": 0.0, "avg_30d_sum": 0.0, "n": 0,
        "sites": set(), "brands": [], "strategies": Counter(),
        "trends": Counter(), "cross_page_count": 0,
        "sheets": set(), "sample_url": "",
    })
    for r in raw:
        a = agg[r["keyword"]]
        a["ad_count_sum"] += r["ad_count"]
        a["ad_count_max"]  = max(a["ad_count_max"], r["ad_count"])
        a["fans_sum"]     += r["fans"]
        a["fans_max"]      = max(a["fans_max"], r["fans"])
        a["heat_7d_sum"]  += r["heat_7d"]
        a["avg_30d_sum"]  += r["avg_30d"]
        a["n"]            += 1
        site = _site_of(r["url"])
        if site:
            a["sites"].add(site)
            if not a["sample_url"]:
                a["sample_url"] = r["url"]
        if r["brand"]:
            a["brands"].append(r["brand"])
        if r["strategy"]:
            a["strategies"][r["strategy"]] += 1
        if r["trend"]:
            a["trends"][r["trend"]] += 1
        if r["cross_page"]:
            a["cross_page_count"] += 1
        a["sheets"].add(r["sheet"])

    # Compute composite score
    ref_max = max(a["ad_count_max"] for a in agg.values()) or 1
    rows, sku_rows = [], []
    for kw, a in agg.items():
        n = a["n"]
        avg_30 = a["avg_30d_sum"] / n if n else 0.0
        heat_7 = a["heat_7d_sum"] / n if n else 0.0
        # momentum: 7d vs 30d ratio, clamped 0.5–2.0
        if avg_30 > 0:
            momentum = max(0.5, min(2.0, heat_7 / avg_30))
        else:
            momentum = 1.0
        site_count    = len(a["sites"])
        # Bonuses kick in only when there's evidence (cross_page or multi-site).
        # No-bonus baseline keeps multiplier = momentum (≈1.0), so most scores
        # equal the raw log_scale_score(ad_count_max, ref_max).
        cross_factor  = 1.0 + 0.15 * min(a["cross_page_count"], 5)   # up to +75 %
        site_factor   = 1.0 + 0.10 * max(site_count - 1, 0)          # up to +50 %, capped below
        site_factor   = min(site_factor, 1.5)
        base          = log_scale_score(a["ad_count_max"], ref_max)
        score         = round(min(100.0, base * momentum * cross_factor * site_factor), 2)

        category = classify_category(kw)
        top_brands = [b for b, _ in Counter(a["brands"]).most_common(5)]
        top_angles = [s for s, _ in a["strategies"].most_common(3)]
        top_trends = [t for t, _ in a["trends"].most_common(2)]

        rows.append({
            "keyword": kw,
            "score": score,
            "raw_metric": a["ad_count_max"],
            "category": category,
            "metadata": {
                "ad_variants_max": a["ad_count_max"],
                "ad_variants_sum": a["ad_count_sum"],
                "fan_count_max": a["fans_max"],
                "last_7d_heat": round(heat_7, 2),
                "last_30d_avg": round(avg_30, 2),
                "momentum": round(momentum, 3),
                "cross_page_count": a["cross_page_count"],
                "site_count": site_count,
                "top_brands": top_brands,
                "strategy_angles": top_angles,
                "trend_labels": top_trends,
                "sample_url": a["sample_url"],
                "sheets": sorted(a["sheets"]),
                "ref_max_corpus": ref_max,
            },
        })

        # Landing-page SKUs (one row per unique site)
        for site in list(a["sites"])[:10]:
            sku_rows.append({
                "sku_id":      f"{site}::{kw}"[:200],
                "keyword":     kw,
                "title":       None,
                "brand":       top_brands[0] if top_brands else None,
                "product_url": f"https://{site}",
                "image_url":   None,
                "category":    category,
                "metadata":    {"discovered_via": "fb_ads"},
            })

    conn = connect(db_path) if db_path else connect()
    n = upsert_many(conn, "fb_ads", rows)
    upsert_skus(conn, "fb_landing", sku_rows)
    # Push category hints into keyword_attrs
    for r in rows:
        upsert_attrs(conn, r["keyword"], primary_category=r["category"])
    log_run(conn, "fb_ads", n, "ok")

    log("fb", f"✓ wrote {n} keywords  ·  {len(sku_rows)} landing-page SKUs  ·  ref_max={ref_max}")
    # quick distribution
    high = sum(1 for r in rows if r["score"] >= 70)
    mid  = sum(1 for r in rows if 40 <= r["score"] < 70)
    log("fb", f"  score distribution: ≥70: {high}  ·  40-70: {mid}  ·  <40: {n-high-mid}")
    return n


def main():
    ap = argparse.ArgumentParser(description="FB Ads signal collector v2")
    ap.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    n = collect(Path(args.xlsx), Path(args.db) if args.db else None)
    sys.exit(0 if n > 0 else 1)


if __name__ == "__main__":
    main()

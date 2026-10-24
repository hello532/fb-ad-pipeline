#!/usr/bin/env python3
"""Reddit purchase-intent signal collector.

Mines public Reddit JSON (no auth needed for read-only) for "looking for X"
/ "anyone tried X" / "best X" / "alternative to X" posts across high-intent
subreddits. The frequency a noun-phrase shows up in this kind of post over
the last week is a real-world demand signal that:

  - is independent of advertiser spend (FB / TikTok)
  - is independent of Amazon's algorithm
  - is *uniquely high-fidelity* for early demand: people aren't asking
    "where can I buy X" unless they're about to.

Subreddits tracked (curated for D2C/Shopify niches):

  buyitforlife, skincareaddiction, asianbeauty, makeupaddiction,
  pet, dogs, cats, bigboobproblems, malehairadvice, weightroom,
  skincare, frugalmalefashion, femalefashionadvice, baking,
  homeimprovement, gardening, tea, coffee, mechanicaladvice,
  homedecor, parenting, sleephackers, supplements, nutrition

Score formula:
  raw_metric = mention_count × log1p(post_score)
  score      = log_scale(raw_metric, ref_max) ∈ [0, 100]

Heuristic: posts with INTENT_MARKERS get 2× weight (already filtered to
purchase-intent posts, but boost the most explicit ones).
"""
from __future__ import annotations
import argparse
import json
import math
import re
import sys
import time
from pathlib import Path
from collections import defaultdict
from typing import Iterable

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from _db import connect, upsert_many, upsert_attrs, log_run             # noqa: E402
from _utils import (log, normalize_keyword, log_scale_score, http_get,   # noqa: E402
                    retry, classify_category, polite_sleep)

DEFAULT_SUBS = [
    "buyitforlife", "skincareaddiction", "asianbeauty", "makeupaddiction",
    "pet", "dogs", "cats", "malehairadvice", "weightroom",
    "skincare", "frugalmalefashion", "femalefashionadvice", "baking",
    "homeimprovement", "gardening", "tea", "coffee", "mechanicaladvice",
    "homedecor", "parenting", "sleephackers", "supplements", "nutrition",
    "fitness", "yoga", "running", "headphones", "buildapc", "sneakers",
]

# Phrases that mark purchase intent in the title
INTENT_MARKERS = [
    "looking for", "anyone tried", "best ", "recommend", "alternative to",
    "where to buy", "any good ", "thoughts on ", "worth it", "vs ",
    "review of", "is it worth", "before i buy", "want to buy", "should i buy",
    "considering ", "help me find", "any luck with",
]
EXPLICIT_BUY = {"want to buy", "where to buy", "should i buy",
                "before i buy", "help me find"}

# regex helpers
_RE_SPLIT = re.compile(r"[^\w\s'\-]+")
STOP = {
    "the","a","an","and","or","for","with","in","on","to","of","by","is","it","at","as","from",
    "this","that","my","our","i","you","we","me","us","they","be","are","was","were","do","did",
    "have","has","had","will","would","could","should","can","just","like","get","got","one",
    "two","three","new","old","good","great","really","very","more","most","some","any","all",
    "anyone","someone","every","ever","never","always","also","best","worst","help","please",
    "thanks","know","need","want","think","tried","trying","try","find","found","buy","bought",
    "what","when","where","why","how","who","which","there","here","then","than","so",
    "use","using","used","make","makes","made","take","taking","took","look","looking","looks",
    "post","posts","comment","update","edit","question","sub","reddit","amazon",
    "alternative","review","reviews","worth","recommendation","recommendations",
}


def _grams(text: str) -> list[str]:
    text = text.lower()
    text = _RE_SPLIT.sub(" ", text)
    words = [w for w in text.split()
             if len(w) > 2 and w not in STOP and not w.isdigit()]
    grams = []
    for i in range(min(len(words) - 1, 12)):
        grams.append(f"{words[i]} {words[i+1]}")
    for i in range(min(len(words) - 2, 8)):
        grams.append(f"{words[i]} {words[i+1]} {words[i+2]}")
    return grams


def _is_intent_post(title: str) -> tuple[bool, bool]:
    """Returns (is_intent, is_explicit_buy)."""
    t = title.lower()
    is_intent = any(m in t for m in INTENT_MARKERS)
    is_explicit = any(m in t for m in EXPLICIT_BUY)
    return is_intent, is_explicit


URL = ("https://www.reddit.com/r/{sub}/top.json?t=week&limit={limit}")


def _fetch_sub(sub: str, limit: int = 100) -> list[dict]:
    headers = {
        "Accept": "application/json",
        # Reddit requires a UA that looks like a real client
        "User-Agent": "signals-collector/2.0 (research; not a bot)",
    }
    def _go():
        body = http_get(URL.format(sub=sub, limit=limit), headers=headers, timeout=20)
        return json.loads(body.decode("utf-8", errors="ignore"))

    try:
        payload = retry(_go, tries=3, base_sleep=4.0,
                        on_error=lambda e, a: log("rd", f"  retry r/{sub} {a}: {e!r}"))
    except Exception as e:
        log("rd", f"  ✗ r/{sub}: {e}")
        return []

    children = (payload.get("data") or {}).get("children") or []
    out = []
    for ch in children:
        d = ch.get("data") or {}
        out.append({
            "title": d.get("title", ""),
            "selftext": (d.get("selftext") or "")[:1000],   # cap
            "score": int(d.get("score") or 0),
            "num_comments": int(d.get("num_comments") or 0),
            "permalink": d.get("permalink") or "",
            "created_utc": d.get("created_utc") or 0,
        })
    return out


def collect(subs: Iterable[str] = DEFAULT_SUBS,
            limit_per_sub: int = 100,
            db_path: Path | None = None) -> int:
    bucket: dict[str, dict] = defaultdict(lambda: {
        "mentions": 0, "weighted": 0.0,
        "subs": set(), "explicit": 0,
        "sample_post": "", "sample_score": 0,
    })

    subs_list = list(subs)
    log("rd", f"scanning {len(subs_list)} subreddits (top of week, limit={limit_per_sub})")
    n_posts = 0
    for sub in subs_list:
        posts = _fetch_sub(sub, limit_per_sub)
        log("rd", f"  ✓ r/{sub}: {len(posts)} posts")
        for p in posts:
            n_posts += 1
            title = p["title"]
            is_intent, is_explicit = _is_intent_post(title)
            if not is_intent:
                continue
            weight = math.log1p(max(0, p["score"]))
            if is_explicit:
                weight *= 2.0
            for kw in _grams(title + " " + p["selftext"][:200]):
                kw = normalize_keyword(kw)
                if not kw or len(kw) < 4:
                    continue
                b = bucket[kw]
                b["mentions"] += 1
                b["weighted"] += weight
                b["subs"].add(sub)
                if is_explicit:
                    b["explicit"] += 1
                if p["score"] > b["sample_score"]:
                    b["sample_score"] = p["score"]
                    b["sample_post"] = f"https://reddit.com{p['permalink']}"
        polite_sleep(0.8, 1.6)

    log("rd", f"  scanned {n_posts} posts total")

    # Filter: must appear in ≥ 2 posts to be meaningful (1-off mentions = noise)
    bucket = {k: v for k, v in bucket.items() if v["mentions"] >= 2}

    if not bucket:
        log("rd", "✗ no qualifying keywords (≥2 mentions in intent posts)")
        conn = connect(db_path) if db_path else connect()
        log_run(conn, "reddit", 0, "failed", "empty")
        return 0

    raws = {kw: b["weighted"] for kw, b in bucket.items()}
    ref_max = max(raws.values()) or 1
    rows = []
    for kw, raw in raws.items():
        b = bucket[kw]
        rows.append({
            "keyword": kw,
            "score": log_scale_score(raw, ref_max),
            "raw_metric": round(raw, 2),
            "category": classify_category(kw),
            "metadata": {
                "mentions":         b["mentions"],
                "explicit_buy":     b["explicit"],
                "sub_count":        len(b["subs"]),
                "subs":             sorted(b["subs"]),
                "sample_post":      b["sample_post"],
                "sample_post_score": b["sample_score"],
                "weighted_raw":     round(b["weighted"], 2),
            },
        })

    conn = connect(db_path) if db_path else connect()
    n = upsert_many(conn, "reddit", rows)
    for r in rows:
        upsert_attrs(conn, r["keyword"], primary_category=r["category"])
    log_run(conn, "reddit", n, "ok")

    log("rd", f"✓ wrote {n} keywords  ·  scanned {n_posts} posts")
    high = sum(1 for r in rows if r["score"] >= 70)
    mid  = sum(1 for r in rows if 40 <= r["score"] < 70)
    log("rd", f"  score distribution: ≥70: {high}  ·  40-70: {mid}  ·  <40: {n-high-mid}")
    explicit_kws = sum(1 for r in rows if r["metadata"]["explicit_buy"] > 0)
    log("rd", f"  explicit-buy intent keywords: {explicit_kws}")
    return n


def main():
    ap = argparse.ArgumentParser(description="Reddit purchase-intent collector v1")
    ap.add_argument("--subs", nargs="*", default=DEFAULT_SUBS)
    ap.add_argument("--limit", type=int, default=100,
                    help="posts per subreddit (max 100 per Reddit API)")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    n = collect(args.subs, args.limit, Path(args.db) if args.db else None)
    sys.exit(0 if n > 0 else 1)


if __name__ == "__main__":
    main()

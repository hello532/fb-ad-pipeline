#!/usr/bin/env python3
"""阶段 1 · 关键词聚合去重 (V3 闭环)

把 `pipeline/signals/signals.db` 里 5 个源（fb_ads / amazon_movers /
tiktok_creative / reddit / ai_intent）的原始 (keyword, source) 行折叠成
**一行一关键词** 的可决策视图，写到：

  1. `signals.db` 的 `keyword_attrs` 表（结构化、可被 SQL 直接 JOIN）
  2. `signals.db` 的 `aggregate_snapshots` 表（每天一份历史快照，给 momentum 用）
  3. `facebook-ad-keywords-master.xlsx` 的 `V3_SIGNALS` sheet（给 fb-ad-picker V2 消费）

设计原则：
  - **Stem 去重**：所有源用 `_utils.canonical_keyword` 折叠到同一 stem
    （"phone holder" / "phone holders" → 同一行）。每行选 max-score 的
    原始词作为 display_keyword。
  - **5 源加权打分**：默认权重 fb 0.25 / amazon 0.25 / tiktok 0.20 /
    reddit 0.15 / ai 0.15。源缺失时把权重等比例分摊到出现的源 — 单源也
    能给出有意义的 weighted_score（不会因为某采集器今天挂了导致全部归零）。
  - **多源投票类目**：每个非空 category 一票，平局按 fb > amazon >
    tiktok > reddit > ai 优先级取。
  - **commercial_intent**：优先用 ai_intent.metadata.intent，
    fallback：reddit explicit_buy>0 → 'transactional'；
    fb_ads ad_variants_max≥20 且 site_count≥1 → 'transactional'；其余 'mixed'。
  - **momentum**：从 `aggregate_snapshots` 读 7 天前同 canonical 的
    weighted_score，算 (now - 7d) / 7d。无历史 → 0.0。
  - **freshness_days** = today - max(last_seen)。

跑法：
    cd "$(git rev-parse --show-toplevel)/pipeline"
    python3 aggregate_keywords.py                 # 写 db + 写 master.xlsx
    python3 aggregate_keywords.py --no-xlsx       # 只更新 db
    python3 aggregate_keywords.py --top 100 --print  # 打印前 100 行
"""
from __future__ import annotations
import os
import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict, Counter
from datetime import date, timedelta, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "signals"))
from _db import connect                                                  # noqa: E402
from _utils import canonical_keyword, log                                 # noqa: E402

# ============================================================
# Config
# ============================================================
SOURCE_WEIGHTS: dict[str, float] = {
    "fb_ads":          0.25,
    "amazon_movers":   0.25,
    "tiktok_creative": 0.20,
    "reddit":          0.15,
    "ai_intent":       0.15,
}
SOURCE_PRIORITY = ("fb_ads", "amazon_movers", "tiktok_creative", "reddit", "ai_intent")
MOMENTUM_WINDOW_DAYS = 7

DEFAULT_MASTER = Path(os.getenv("FB_KEYWORDS_XLSX",
    str(Path(__file__).resolve().parents[1] / "facebook-ad-keywords-master.xlsx")))
SHEET_NAME = "V3_SIGNALS"


# ============================================================
# 1. Snapshot table — used for momentum / regression detection
# ============================================================
SNAP_DDL = """
CREATE TABLE IF NOT EXISTS aggregate_snapshots (
  snap_date      DATE NOT NULL,
  keyword        TEXT NOT NULL,        -- canonical (stemmed) form
  weighted_score REAL,
  source_count   INTEGER,
  PRIMARY KEY (snap_date, keyword)
);
CREATE INDEX IF NOT EXISTS idx_snap_kw   ON aggregate_snapshots(keyword);
CREATE INDEX IF NOT EXISTS idx_snap_date ON aggregate_snapshots(snap_date);
"""


def _ensure_snapshot_table(conn: sqlite3.Connection) -> None:
    conn.executescript(SNAP_DDL)


# ============================================================
# 2. Loaders / helpers
# ============================================================
def _load_signals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read every (keyword, source) row with parsed metadata + dates."""
    rows = []
    for r in conn.execute(
        """SELECT keyword, source, score, raw_metric, category,
                  first_seen, last_seen, metadata
           FROM signals"""):
        kw, source, score, raw, cat, first_seen, last_seen, meta_json = r
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except Exception:
            meta = {}
        rows.append({
            "keyword": kw,
            "source": source,
            "score": float(score or 0.0),
            "raw_metric": float(raw) if raw is not None else None,
            "category": cat,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "metadata": meta,
        })
    return rows


def _load_prior_snapshot(conn: sqlite3.Connection,
                         days_ago: int) -> dict[str, float]:
    """canonical_kw → weighted_score from N days ago (closest snapshot ≤ that day)."""
    cutoff = (date.today() - timedelta(days=days_ago)).isoformat()
    out: dict[str, float] = {}
    for kw, score in conn.execute(
        """SELECT keyword, weighted_score
           FROM aggregate_snapshots
           WHERE snap_date <= ?
           ORDER BY snap_date DESC""",
        (cutoff,),
    ):
        # First (newest) row per keyword wins
        out.setdefault(kw, float(score or 0.0))
    return out


# ============================================================
# 3. Aggregation logic — the meat
# ============================================================
def _vote_category(per_source: dict[str, dict[str, Any]]) -> str | None:
    """Majority vote across sources; tie → SOURCE_PRIORITY order; fallback → keyword inference."""
    votes: Counter = Counter()
    for src, agg in per_source.items():
        cat = agg.get("category")
        if cat:
            votes[cat] += 1
    if votes:
        top = votes.most_common()
        top_count = top[0][1]
        tied = [c for c, n in top if n == top_count]
        if len(tied) == 1:
            return tied[0]
        for src in SOURCE_PRIORITY:
            if src in per_source and per_source[src].get("category") in tied:
                return per_source[src]["category"]
        return tied[0]

    # Fallback: infer from canonical keyword
    canon = next(iter(per_source.values())).get("display_keyword", "").lower() if per_source else ""
    if not canon:
        return None
    RULES = [
        (['massag','pain','relief','therapy','recovery','muscle','joint','back','neck',
          'shoulder','knee','foot','feet','tens','ems','acupressure','compression',
          'heating','vibration','chiropract','nerve','neuropathy','inflammation',
          'spine','lumbar','trigger'], 'Health & Wellness'),
        (['skin','care','beauty','makeup','acne','wrinkle','anti-aging','serum',
          'moistur','cleanser','retinol','glow','hair','shampoo','nail','cosmetic'],
         'Beauty & Personal Care'),
        (['pet','dog','cat','puppy','kitten','paw','leash','collar','aquarium','fish'],
         'Pet Care'),
        (['kitchen','cook','blender','air fryer','coffee','clean','vacuum','mop',
          'organiz','storage','furniture','mattress','pillow','blanket','sheet'],
         'Home & Kitchen'),
        (['fitness','gym','exercise','workout','yoga','weight','protein','supplement',
          'vitamin','creatine'], 'Fitness & Sports'),
        (['phone','charger','headphone','earbuds','speaker','camera','laptop',
          'tablet','smart watch','electronic','tech','gadget'], 'Electronics'),
        (['baby','toddler','infant','nursing','stroller','car seat','toy','kids'],
         'Baby & Kids'),
        (['cloth','fashion','dress','shoe','boot','sneaker','jacket','activewear',
          'legging','bra','jewelry','watch','sunglass','bag','purse'], 'Fashion & Apparel'),
    ]
    for keywords, category in RULES:
        if any(k in canon for k in keywords):
            return category
    return None


def _commercial_intent(per_source: dict[str, dict[str, Any]]) -> str:
    ai = per_source.get("ai_intent")
    if ai:
        intent = (ai.get("metadata") or {}).get("intent")
        if intent in {"info", "mixed", "transactional"}:
            return intent
    rd = per_source.get("reddit")
    if rd and (rd.get("metadata") or {}).get("explicit_buy", 0) > 0:
        return "transactional"
    fb = per_source.get("fb_ads")
    if fb:
        meta = fb.get("metadata") or {}
        if (meta.get("ad_variants_max", 0) or 0) >= 20 and (meta.get("site_count", 0) or 0) >= 1:
            return "transactional"
    return "mixed"


def _weighted_score(per_source: dict[str, dict[str, Any]]) -> float:
    """Re-normalize weights across observed sources only."""
    present = {s: SOURCE_WEIGHTS[s] for s in per_source if s in SOURCE_WEIGHTS}
    if not present:
        return 0.0
    total_w = sum(present.values()) or 1.0
    return round(
        sum(per_source[s]["score"] * (w / total_w) for s, w in present.items()),
        2,
    )


def _pick_display(per_source: dict[str, dict[str, Any]]) -> str:
    """Choose the human-readable original keyword for this canonical group.

    Priority:
      1. fb_ads's max-score variant (most curated, English-clean)
      2. otherwise the variant whose source ranks highest in SOURCE_PRIORITY
    """
    for src in SOURCE_PRIORITY:
        if src in per_source:
            return per_source[src]["display_keyword"]
    # impossible, but safe
    return next(iter(per_source.values()))["display_keyword"]


def _top_brands(per_source: dict[str, dict[str, Any]], k: int = 3) -> list[str]:
    """Extract top brands from ANY source that has them, not just fb_ads."""
    seen: list[str] = []
    # Priority: fb_ads > amazon_movers > tiktok_creative > reddit > ai_intent
    for src in SOURCE_PRIORITY:
        agg = per_source.get(src)
        if not agg:
            continue
        meta = agg.get("metadata") or {}
        brands = meta.get("top_brands") or []
        for b in brands:
            b = (b or "").strip()
            if b and b not in seen:
                seen.append(b)
            if len(seen) >= k:
                return seen
        # Also try page_name / brand_name from metadata
        for key in ("page_name", "brand_name", "brand"):
            val = (meta.get(key) or "").strip()
            if val and val not in seen and val != "Unknown Brand":
                seen.append(val)
            if len(seen) >= k:
                return seen
    return seen


def _sample_url(per_source: dict[str, dict[str, Any]]) -> str:
    """Extract sample URL from ANY source, not just fb_ads."""
    # Try all sources in priority order
    for src in SOURCE_PRIORITY:
        agg = per_source.get(src)
        if not agg:
            continue
        meta = agg.get("metadata") or {}
        # Direct URL fields
        for key in ("sample_url", "sample_post", "link_url", "url", "landing_url"):
            val = (meta.get(key) or "").strip()
            if val and val.startswith("http") and "https:://" not in val:
                return val
        # Fix broken protocol
        for key in ("sample_url", "sample_post", "link_url", "url", "landing_url"):
            val = (meta.get(key) or "").strip()
            if val and "https:://" in val:
                return val.replace("https:://", "https://")
            if val and "http:://" in val:
                return val.replace("http:://", "http://")
    return ""


def aggregate(conn: sqlite3.Connection,
              window_days: int = MOMENTUM_WINDOW_DAYS) -> list[dict[str, Any]]:
    """Build the V3 keyword view from `signals` rows. Pure function — no writes."""
    rows = _load_signals(conn)
    if not rows:
        return []

    # canonical_kw → source → aggregated dict (max score per source)
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        canon = canonical_keyword(r["keyword"])
        if not canon:
            continue
        src = r["source"]
        existing = grouped[canon].get(src)
        if existing is None or r["score"] > existing["score"]:
            grouped[canon][src] = {
                **r,
                "display_keyword": r["keyword"],
            }

    prior = _load_prior_snapshot(conn, window_days)
    today = date.today()
    out: list[dict[str, Any]] = []
    for canon, per_src in grouped.items():
        weighted = _weighted_score(per_src)
        prior_w = prior.get(canon, 0.0)
        if prior_w > 0:
            momentum = round((weighted - prior_w) / prior_w, 4)
        else:
            momentum = 0.0
        last_seen = max((p.get("last_seen") or "1970-01-01") for p in per_src.values())
        first_seen = min((p.get("first_seen") or today.isoformat())
                          for p in per_src.values())
        try:
            freshness = (today - date.fromisoformat(last_seen)).days
        except Exception:
            freshness = -1

        out.append({
            "canonical_keyword": canon,
            "display_keyword":   _pick_display(per_src),
            "primary_category":  _vote_category(per_src),
            "source_count":      len(per_src),
            "sources":           ",".join(sorted(per_src.keys())),
            "weighted_score":    weighted,
            "momentum":          momentum,
            "freshness_days":    freshness,
            "score_fb_ads":      round(per_src.get("fb_ads", {}).get("score", 0.0), 2),
            "score_amazon":      round(per_src.get("amazon_movers", {}).get("score", 0.0), 2),
            "score_tiktok":      round(per_src.get("tiktok_creative", {}).get("score", 0.0), 2),
            "score_reddit":      round(per_src.get("reddit", {}).get("score", 0.0), 2),
            "score_ai":          round(per_src.get("ai_intent", {}).get("score", 0.0), 2),
            "commercial_intent": _commercial_intent(per_src),
            "first_seen":        first_seen,
            "last_seen":         last_seen,
            "top_brands":        ", ".join(_top_brands(per_src)),
            "sample_url":        _sample_url(per_src),
        })

    out.sort(key=lambda r: r["weighted_score"], reverse=True)
    return out


# ============================================================
# 4. Persisters
# ============================================================
def write_keyword_attrs(conn: sqlite3.Connection,
                        rows: list[dict[str, Any]]) -> int:
    """Upsert into keyword_attrs. Indexed by canonical keyword."""
    n = 0
    today = date.today().isoformat()
    conn.execute("BEGIN")
    try:
        for r in rows:
            conn.execute(
                """
                INSERT INTO keyword_attrs
                  (keyword, primary_category, source_count, weighted_score,
                   momentum, freshness_days, first_seen, last_seen, notes,
                   updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(keyword) DO UPDATE SET
                  primary_category = excluded.primary_category,
                  source_count     = excluded.source_count,
                  weighted_score   = excluded.weighted_score,
                  momentum         = excluded.momentum,
                  freshness_days   = excluded.freshness_days,
                  first_seen       = COALESCE(keyword_attrs.first_seen, excluded.first_seen),
                  last_seen        = excluded.last_seen,
                  notes            = excluded.notes,
                  updated_at       = CURRENT_TIMESTAMP
                """,
                (
                    r["canonical_keyword"],
                    r["primary_category"],
                    r["source_count"],
                    r["weighted_score"],
                    r["momentum"],
                    r["freshness_days"],
                    r["first_seen"],
                    r["last_seen"],
                    json.dumps({
                        "display":  r["display_keyword"],
                        "sources":  r["sources"],
                        "intent":   r["commercial_intent"],
                        "brands":   r["top_brands"],
                    }, ensure_ascii=False),
                ),
            )
            n += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n


def write_snapshot(conn: sqlite3.Connection,
                   rows: list[dict[str, Any]]) -> int:
    today = date.today().isoformat()
    n = 0
    conn.execute("BEGIN")
    try:
        for r in rows:
            conn.execute(
                """INSERT OR REPLACE INTO aggregate_snapshots
                   (snap_date, keyword, weighted_score, source_count)
                   VALUES (?, ?, ?, ?)""",
                (today, r["canonical_keyword"],
                 r["weighted_score"], r["source_count"]),
            )
            n += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n


def write_xlsx_sheet(rows: list[dict[str, Any]],
                     master_path: Path,
                     sheet_name: str = SHEET_NAME) -> int:
    """Replace (or create) the V3_SIGNALS sheet in master.xlsx.

    Uses openpyxl directly (not pandas) so we don't disturb other sheets.
    """
    try:
        from openpyxl import load_workbook, Workbook
    except ImportError:
        log("agg", "✗ openpyxl missing — skip xlsx write")
        return 0

    if master_path.exists():
        wb = load_workbook(master_path)
    else:
        wb = Workbook()
        # Remove the default sheet that ships with new workbook
        if "Sheet" in wb.sheetnames and len(wb.sheetnames) == 1:
            del wb["Sheet"]

    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name, index=0)  # put on top

    headers = [
        "canonical_keyword", "display_keyword", "primary_category",
        "source_count", "sources",
        "weighted_score", "momentum", "freshness_days",
        "score_fb_ads", "score_amazon", "score_tiktok", "score_reddit", "score_ai",
        "commercial_intent",
        "first_seen", "last_seen",
        "top_brands", "sample_url",
    ]
    ws.append(headers)
    for r in rows:
        ws.append([r.get(h, "") for h in headers])

    # Friendly header row
    from openpyxl.styles import Font, PatternFill, Alignment
    bold = Font(bold=True, color="FFFFFF")
    fill = PatternFill("solid", fgColor="2E5BBA")
    for cell in ws[1]:
        cell.font = bold
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"

    # Auto-size key columns (rough)
    widths = {
        "A": 36, "B": 36, "C": 14, "D": 8, "E": 28,
        "F": 14, "G": 10, "H": 12,
        "I": 12, "J": 12, "K": 12, "L": 12, "M": 12,
        "N": 16, "O": 12, "P": 12, "Q": 36, "R": 48,
    }
    for col, w in widths.items():
        ws.column_dimensions[col].width = w

    master_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(master_path)
    return len(rows)


# ============================================================
# 5. CLI
# ============================================================
def _print_summary(rows: list[dict[str, Any]]) -> None:
    n = len(rows)
    if not n:
        log("agg", "✗ no rows aggregated")
        return
    src_dist = Counter()
    for r in rows:
        src_dist[r["source_count"]] += 1
    cats = Counter(r["primary_category"] or "(none)" for r in rows)
    intents = Counter(r["commercial_intent"] for r in rows)
    rising = sum(1 for r in rows if r["momentum"] >= 0.10)
    falling = sum(1 for r in rows if r["momentum"] <= -0.10)

    log("agg", "─" * 56)
    log("agg", f"aggregated: {n} unique canonical keywords")
    log("agg", f"  source-coverage: " + "  ".join(
        f"{k}src={v}" for k, v in sorted(src_dist.items())))
    log("agg", f"  intent: " + "  ".join(f"{k}={v}" for k, v in intents.items()))
    log("agg", f"  momentum: rising≥+10%={rising}  falling≤-10%={falling}")
    log("agg", "  top categories:")
    for c, v in cats.most_common(5):
        log("agg", f"    {c:<14} {v}")
    log("agg", "  top 5 by weighted_score:")
    for r in rows[:5]:
        log("agg", f"    {r['weighted_score']:>5.1f}  "
                   f"[{r['source_count']}src,{r['commercial_intent'][:5]}]  "
                   f"{r['display_keyword'][:60]}")


def main():
    ap = argparse.ArgumentParser(description="V3 keyword aggregator (signals → master.xlsx)")
    ap.add_argument("--db", default=None, help="signals.db path (default: pipeline/signals/signals.db)")
    ap.add_argument("--master", default=str(DEFAULT_MASTER),
                    help="facebook-ad-keywords-master.xlsx path")
    ap.add_argument("--no-xlsx", action="store_true",
                    help="only update db, don't touch master.xlsx")
    ap.add_argument("--no-db", action="store_true",
                    help="only print/export, don't touch keyword_attrs/snapshots")
    ap.add_argument("--no-snapshot", action="store_true",
                    help="skip writing today's snapshot row (useful for re-runs)")
    ap.add_argument("--top", type=int, default=20, help="rows to print")
    ap.add_argument("--print", action="store_true", help="print top N to stdout")
    ap.add_argument("--window", type=int, default=MOMENTUM_WINDOW_DAYS,
                    help="momentum lookback in days (default 7)")
    args = ap.parse_args()

    conn = connect(Path(args.db) if args.db else None)
    _ensure_snapshot_table(conn)

    rows = aggregate(conn, window_days=args.window)
    log("agg", f"build view: {len(rows)} canonical keywords (from raw signals)")

    if not args.no_db:
        n_attrs = write_keyword_attrs(conn, rows)
        log("agg", f"✓ keyword_attrs upserted: {n_attrs}")
        if not args.no_snapshot:
            n_snap = write_snapshot(conn, rows)
            log("agg", f"✓ aggregate_snapshots wrote: {n_snap}  (date={date.today().isoformat()})")

    if not args.no_xlsx:
        n_xls = write_xlsx_sheet(rows, Path(args.master))
        if n_xls:
            log("agg", f"✓ {Path(args.master).name} :: {SHEET_NAME} sheet → {n_xls} rows")

    _print_summary(rows)
    if args.print:
        log("agg", "─" * 56)
        log("agg", f"top {args.top} rows:")
        for r in rows[:args.top]:
            log("agg",
                f"  {r['weighted_score']:>5.1f}  src={r['source_count']}  "
                f"mom={r['momentum']:+.2f}  intent={r['commercial_intent']:<13}  "
                f"{r['display_keyword'][:50]}")
    sys.exit(0 if rows else 1)


if __name__ == "__main__":
    main()

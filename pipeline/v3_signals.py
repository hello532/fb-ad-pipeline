"""pipeline/v3_signals.py — v3 Signal Aggregator (Layer 1).

Multi-source signal snapshots for the v3 decision brain. v0 covers 5 signal
types, all derived from already-present DBs (no external network calls).

Signals (v0):
  fb_ad           — pipeline.db ad_archive trailing windows (brand/keyword)
  google_trends   — gtrends-bulk/cache.db trends_v3 + related_queries (keyword)
  amazon_aba      — amz-keyword-scanner/kw_scanner.db aba_keywords + kw_analysis (keyword)
  commercial      — signals.combined_signal_score over recent ads (brand)
  insights_2026   — insights_2026.insight_score over recent ads (brand)

Placeholders (Layer 1.5):
  tiktok, lp, geo, seasonality — return None + confidence=0 for now

Table: signal_snapshot (long-form, one row per entity x signal_type x date).
JSON payload per row; schema_version field for future evolution.

Public API:
    collect_brand_signals(domain, date) -> dict[signal_type, snapshot]
    collect_keyword_signals(kw, date)   -> dict[signal_type, snapshot]
    write_snapshot(...)                 -> None
    get_snapshot(entity_type, entity_ref, signal_type, on_date=None)
    get_history(entity_type, entity_ref, signal_type, days=30)
    get_coverage(date)                  -> dict[signal_type, pct]

All collectors return (data_dict | None, confidence: 0.0-1.0, source: str).
Failures swallowed → confidence=0, source='error:<reason>'; no exception bubble.
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from core.logger import get_logger

from .paths import NEW_DIR, SKILLS_DIR
from .runtime import resolve_env_path

log = get_logger(__name__)


# ── Paths ────────────────────────────────────────────────────────────────────

DB_FILE = resolve_env_path("FB_DB_FILE", NEW_DIR / "pipeline.db", NEW_DIR)
GTRENDS_DB = resolve_env_path("GTRENDS_DB", SKILLS_DIR / "gtrends-bulk" / "cache.db", SKILLS_DIR)
AMZ_DB = resolve_env_path("AMZ_DB", SKILLS_DIR / "amz-keyword-scanner" / "kw_scanner.db", SKILLS_DIR)

SCHEMA_VERSION = 1
V0_SIGNAL_TYPES = (
    "fb_ad", "google_trends", "amazon_aba", "commercial", "insights_2026",
)
PLACEHOLDER_SIGNAL_TYPES = ("tiktok", "lp", "geo", "seasonality")
ALL_SIGNAL_TYPES = V0_SIGNAL_TYPES + PLACEHOLDER_SIGNAL_TYPES


# ── Schema ───────────────────────────────────────────────────────────────────

V3_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_snapshot (
  snapshot_date  TEXT NOT NULL,
  entity_type    TEXT NOT NULL,
  entity_ref     TEXT NOT NULL,
  signal_type    TEXT NOT NULL,
  data_json      TEXT NOT NULL,
  confidence     REAL NOT NULL,
  source         TEXT NOT NULL,
  schema_version INTEGER DEFAULT 1,
  captured_at    TEXT NOT NULL,
  PRIMARY KEY (snapshot_date, entity_type, entity_ref, signal_type)
);
CREATE INDEX IF NOT EXISTS idx_ss_entity ON signal_snapshot(entity_type, entity_ref, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_ss_date   ON signal_snapshot(snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_ss_type   ON signal_snapshot(signal_type, snapshot_date DESC);
"""


_SCHEMA_READY = False


def _ensure_schema() -> None:
    """Idempotent schema bootstrap. Called lazily on first write/read."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    conn = sqlite3.connect(str(DB_FILE))
    try:
        conn.executescript(V3_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _SCHEMA_READY = True


# ── Connection helpers ───────────────────────────────────────────────────────

@contextmanager
def _pipeline_conn():
    """Read/write connection to pipeline.db (the v2 main DB)."""
    _ensure_schema()
    conn = sqlite3.connect(str(DB_FILE), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def _readonly_ext_conn(db_path: Path):
    """Read-only connection to an external sibling skill DB.

    Returns None inside the context if the DB is missing or unreadable —
    callers branch on that, no exception.
    """
    if not db_path.exists():
        yield None
        return
    try:
        # uri mode=ro guarantees we never write to a sibling skill's DB
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()
    except Exception as e:
        log.info(f"v3_signals: external DB unreadable {db_path}: {e}")
        yield None


# ── Date helpers ─────────────────────────────────────────────────────────────

def _today_local() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _shift_days(date_str: str, delta: int) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=delta)
    return d.strftime("%Y-%m-%d")


def _to_iso_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── Write / read primitives ──────────────────────────────────────────────────

def write_snapshot(
    entity_type: str,
    entity_ref: str,
    signal_type: str,
    data: Optional[dict],
    confidence: float,
    source: str,
    snapshot_date: Optional[str] = None,
) -> None:
    """Upsert one (entity, signal_type, date) row.

    None data is allowed when confidence==0 (failure/placeholder); we still
    write a row so `coverage` queries can distinguish "tried but failed" from
    "never attempted".
    """
    if signal_type not in ALL_SIGNAL_TYPES:
        raise ValueError(f"unknown signal_type {signal_type!r}")
    if entity_type not in ("brand", "keyword", "niche"):
        raise ValueError(f"unknown entity_type {entity_type!r}")
    snapshot_date = snapshot_date or _today_local()
    payload = json.dumps(data or {}, ensure_ascii=False, separators=(",", ":"))
    confidence = max(0.0, min(1.0, float(confidence)))
    captured_at = _to_iso_ts()
    with _pipeline_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO signal_snapshot
              (snapshot_date, entity_type, entity_ref, signal_type,
               data_json, confidence, source, schema_version, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (snapshot_date, entity_type, entity_ref, signal_type,
             payload, confidence, source, SCHEMA_VERSION, captured_at),
        )


def upsert_signals_batch(rows: list[tuple]) -> int:
    """Batch upsert. Each row: (snapshot_date, entity_type, entity_ref,
    signal_type, data_dict, confidence, source). Returns rows written."""
    if not rows:
        return 0
    norm = []
    captured_at = _to_iso_ts()
    for r in rows:
        snapshot_date, entity_type, entity_ref, signal_type, data, conf, src = r
        if signal_type not in ALL_SIGNAL_TYPES:
            continue
        payload = json.dumps(data or {}, ensure_ascii=False, separators=(",", ":"))
        conf = max(0.0, min(1.0, float(conf)))
        norm.append((snapshot_date, entity_type, entity_ref, signal_type,
                     payload, conf, src, SCHEMA_VERSION, captured_at))
    with _pipeline_conn() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO signal_snapshot
              (snapshot_date, entity_type, entity_ref, signal_type,
               data_json, confidence, source, schema_version, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, norm,
        )
    return len(norm)


def get_snapshot(
    entity_type: str,
    entity_ref: str,
    signal_type: str,
    on_date: Optional[str] = None,
) -> Optional[dict]:
    """Return the latest row at-or-before on_date (default: latest ever)."""
    with _pipeline_conn() as conn:
        if on_date:
            row = conn.execute(
                """
                SELECT * FROM signal_snapshot
                WHERE entity_type=? AND entity_ref=? AND signal_type=?
                  AND snapshot_date <= ?
                ORDER BY snapshot_date DESC LIMIT 1
                """, (entity_type, entity_ref, signal_type, on_date),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM signal_snapshot
                WHERE entity_type=? AND entity_ref=? AND signal_type=?
                ORDER BY snapshot_date DESC LIMIT 1
                """, (entity_type, entity_ref, signal_type),
            ).fetchone()
    if not row:
        return None
    return {
        "snapshot_date": row["snapshot_date"],
        "data": json.loads(row["data_json"]) if row["data_json"] else {},
        "confidence": row["confidence"],
        "source": row["source"],
        "schema_version": row["schema_version"],
        "captured_at": row["captured_at"],
    }


def get_history(
    entity_type: str,
    entity_ref: str,
    signal_type: str,
    days: int = 30,
) -> list[dict]:
    """Last `days` of snapshots for one (entity, signal_type), newest first."""
    from_date = _shift_days(_today_local(), -max(1, int(days)))
    with _pipeline_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM signal_snapshot
            WHERE entity_type=? AND entity_ref=? AND signal_type=?
              AND snapshot_date >= ?
            ORDER BY snapshot_date DESC
            """, (entity_type, entity_ref, signal_type, from_date),
        ).fetchall()
    return [
        {
            "snapshot_date": r["snapshot_date"],
            "data": json.loads(r["data_json"]) if r["data_json"] else {},
            "confidence": r["confidence"],
            "source": r["source"],
            "captured_at": r["captured_at"],
        }
        for r in rows
    ]


def get_coverage(snapshot_date: Optional[str] = None) -> dict[str, Any]:
    """Per-signal coverage stats for a given date.

    Returns:
        {
          "date": "YYYY-MM-DD",
          "entity_counts": {"brand": N, "keyword": M},
          "signals": {
            "fb_ad": {"rows": N, "conf_avg": 0.85, "conf_zero_pct": 0.05},
            ...
          },
          "completeness": 0..1   # rows-with-conf>0 / (entities * v0_signal_count)
        }
    """
    snapshot_date = snapshot_date or _today_local()
    with _pipeline_conn() as conn:
        per_signal = conn.execute(
            """
            SELECT signal_type,
                   COUNT(*)                            AS rows,
                   AVG(confidence)                     AS conf_avg,
                   SUM(CASE WHEN confidence=0 THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0)
                                                       AS conf_zero_pct
            FROM signal_snapshot
            WHERE snapshot_date=?
            GROUP BY signal_type
            """, (snapshot_date,),
        ).fetchall()
        entity_counts_row = conn.execute(
            """
            SELECT entity_type, COUNT(DISTINCT entity_ref) AS n
            FROM signal_snapshot
            WHERE snapshot_date=?
            GROUP BY entity_type
            """, (snapshot_date,),
        ).fetchall()
        total_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM signal_snapshot WHERE snapshot_date=? AND confidence>0",
            (snapshot_date,),
        ).fetchone()
    entity_counts = {r["entity_type"]: r["n"] for r in entity_counts_row}
    total_entities = sum(entity_counts.values()) or 1
    completeness = (total_rows["n"] or 0) / (total_entities * len(V0_SIGNAL_TYPES))
    return {
        "date": snapshot_date,
        "entity_counts": entity_counts,
        "signals": {
            r["signal_type"]: {
                "rows": r["rows"],
                "conf_avg": round(r["conf_avg"] or 0.0, 3),
                "conf_zero_pct": round(r["conf_zero_pct"] or 0.0, 3),
            }
            for r in per_signal
        },
        "completeness": round(min(1.0, completeness), 3),
    }


# ── Collector 1: FB Ad signal (brand) ────────────────────────────────────────

def _fb_ad_for_brand(domain: str, snapshot_date: str) -> tuple[Optional[dict], float, str]:
    """Trailing windows of ad activity for one brand.

    Reads ad_archive WHERE domain=? AND scan_date in trailing windows.
    Returns counts (now/1d/7d/30d), velocity, unique pages, top CTAs, spend.
    """
    try:
        with _pipeline_conn() as conn:
            d_30 = _shift_days(snapshot_date, -30)
            d_7  = _shift_days(snapshot_date, -7)
            d_1  = _shift_days(snapshot_date, -1)
            rows = conn.execute(
                """
                SELECT scan_date, COUNT(*) AS n,
                       COUNT(DISTINCT page_id) AS pages,
                       AVG(NULLIF(spend_lower, 0)) AS spend_avg
                FROM ad_archive
                WHERE domain = ? AND scan_date >= ?
                GROUP BY scan_date
                """, (domain, d_30),
            ).fetchall()
            if not rows:
                return (None, 0.0, "pipeline.db:ad_archive:empty")
            count_now = sum(r["n"] for r in rows if r["scan_date"] == snapshot_date)
            count_1d = sum(r["n"] for r in rows if r["scan_date"] >= d_1)
            count_7d = sum(r["n"] for r in rows if r["scan_date"] >= d_7)
            count_30d = sum(r["n"] for r in rows)
            avg_1d = count_1d / 1.0
            avg_7d = count_7d / 7.0
            avg_30d = count_30d / 30.0
            velocity_pct = (
                (avg_7d - avg_30d) / avg_30d * 100.0 if avg_30d > 0 else 0.0
            )
            unique_pages = max((r["pages"] or 0) for r in rows) if rows else 0
            spend_lower_avg = (
                sum((r["spend_avg"] or 0) for r in rows) / max(len(rows), 1)
            )

            cta_rows = conn.execute(
                """
                SELECT cta_type, COUNT(*) AS n FROM ad_archive
                WHERE domain=? AND scan_date >= ? AND cta_type != ''
                GROUP BY cta_type ORDER BY n DESC LIMIT 3
                """, (domain, d_30),
            ).fetchall()
            top_cta_types = [r["cta_type"] for r in cta_rows if r["cta_type"]]

            fmt_rows = conn.execute(
                """
                SELECT display_format, COUNT(*) AS n FROM ad_archive
                WHERE domain=? AND scan_date >= ?
                GROUP BY display_format
                """, (domain, d_30),
            ).fetchall()
            fmt_total = sum(r["n"] for r in fmt_rows) or 1

            def _fmt_pct(needle: str) -> float:
                hit = sum(r["n"] for r in fmt_rows
                          if needle in (r["display_format"] or "").lower())
                return round(hit / fmt_total, 3)

            active_rows = conn.execute(
                """
                SELECT AVG(active_days) AS avg_active FROM ad_archive
                WHERE domain=? AND scan_date >= ?
                """, (domain, d_30),
            ).fetchone()
            avg_active_days = float(active_rows["avg_active"] or 0)

            data = {
                "ad_count_now": int(count_now),
                "ad_count_1d_avg": round(avg_1d, 2),
                "ad_count_7d_avg": round(avg_7d, 2),
                "ad_count_30d_avg": round(avg_30d, 2),
                "velocity_pct_7d_vs_30d": round(velocity_pct, 1),
                "unique_pages": int(unique_pages),
                "top_cta_types": top_cta_types,
                "avg_spend_lower_usd": round(spend_lower_avg, 1),
                "avg_active_days": round(avg_active_days, 1),
                "format_mix": {
                    "video": _fmt_pct("video"),
                    "image": _fmt_pct("image"),
                    "carousel": _fmt_pct("carousel"),
                },
            }
            # confidence: more days observed = higher (cap at 0.95)
            days_seen = len({r["scan_date"] for r in rows})
            conf = min(0.95, 0.3 + days_seen / 30 * 0.65)
            return (data, conf, "pipeline.db:ad_archive")
    except Exception as e:
        return (None, 0.0, f"error:fb_ad_brand:{type(e).__name__}")


# ── Collector 1b: FB Ad signal (keyword) ─────────────────────────────────────

def _fb_ad_for_keyword(kw: str, snapshot_date: str) -> tuple[Optional[dict], float, str]:
    """Same shape as brand, scoped to one keyword in ad_archive."""
    try:
        with _pipeline_conn() as conn:
            d_30 = _shift_days(snapshot_date, -30)
            d_7 = _shift_days(snapshot_date, -7)
            d_1 = _shift_days(snapshot_date, -1)
            rows = conn.execute(
                """
                SELECT scan_date, COUNT(*) AS n,
                       COUNT(DISTINCT page_id) AS pages,
                       COUNT(DISTINCT domain) AS domains
                FROM ad_archive
                WHERE keyword=? AND scan_date >= ?
                GROUP BY scan_date
                """, (kw, d_30),
            ).fetchall()
            if not rows:
                return (None, 0.0, "pipeline.db:ad_archive:empty")
            count_now = sum(r["n"] for r in rows if r["scan_date"] == snapshot_date)
            count_1d = sum(r["n"] for r in rows if r["scan_date"] >= d_1)
            count_7d = sum(r["n"] for r in rows if r["scan_date"] >= d_7)
            count_30d = sum(r["n"] for r in rows)
            avg_7d = count_7d / 7.0
            avg_30d = count_30d / 30.0
            velocity_pct = (avg_7d - avg_30d) / avg_30d * 100.0 if avg_30d > 0 else 0.0
            unique_pages = max((r["pages"] or 0) for r in rows) if rows else 0
            unique_domains = max((r["domains"] or 0) for r in rows) if rows else 0
            data = {
                "ad_count_now": int(count_now),
                "ad_count_1d_avg": round(count_1d, 2),
                "ad_count_7d_avg": round(avg_7d, 2),
                "ad_count_30d_avg": round(avg_30d, 2),
                "velocity_pct_7d_vs_30d": round(velocity_pct, 1),
                "unique_pages": int(unique_pages),
                "unique_brands": int(unique_domains),
            }
            days_seen = len({r["scan_date"] for r in rows})
            conf = min(0.95, 0.3 + days_seen / 30 * 0.65)
            return (data, conf, "pipeline.db:ad_archive")
    except Exception as e:
        return (None, 0.0, f"error:fb_ad_keyword:{type(e).__name__}")


# ── Collector 2: Google Trends signal (keyword) ──────────────────────────────

def _google_trends(kw: str, snapshot_date: str, geo: str = "US") -> tuple[Optional[dict], float, str]:
    """Read trends_v3 + related_queries + region_interest from gtrends-bulk.

    External DB is opened read-only; missing/unreadable returns conf=0.
    """
    try:
        with _readonly_ext_conn(GTRENDS_DB) as conn:
            if conn is None:
                return (None, 0.0, "gtrends:db_missing")
            row = conn.execute(
                """
                SELECT keyword, geo, timeframe, avg_score, median, last_30d, peak,
                       peak_date, trend_dir, confidence, volatility,
                       seasonal_peak_month, yoy_change, data_points
                FROM trends_v3
                WHERE keyword=? AND (geo=? OR geo='')
                ORDER BY ts DESC LIMIT 1
                """, (kw, geo),
            ).fetchone()
            if not row:
                return (None, 0.0, "gtrends:no_data")
            related = conn.execute(
                """
                SELECT related_kw, query_type, value FROM related_queries
                WHERE keyword=? AND (geo=? OR geo='') AND query_type='rising'
                ORDER BY ts DESC LIMIT 5
                """, (kw, geo),
            ).fetchall()
            regions = conn.execute(
                """
                SELECT region, value FROM region_interest
                WHERE keyword=? AND (geo=? OR geo='')
                ORDER BY value DESC LIMIT 5
                """, (kw, geo),
            ).fetchall()
            yoy_pct = float(row["yoy_change"] or 0) * 100.0
            avg_score = row["avg_score"]
            # breakout: rising avg + recent month spike
            breakout_score = 0.0
            if avg_score and avg_score > 0:
                breakout_score = round(
                    (row["last_30d"] or 0) / avg_score, 2
                )
            data = {
                "interest_now": int(row["last_30d"] or 0),
                "interest_yoy_pct": round(yoy_pct, 1),
                "breakout_score": breakout_score,
                "avg_score": round(avg_score or 0, 1),
                "peak": int(row["peak"] or 0),
                "trend_direction": (row["trend_dir"] or "flat"),
                "volatility": round(row["volatility"] or 0, 3),
                "seasonal_peak_month": row["seasonal_peak_month"],
                "top_related_queries": [
                    {"q": r["related_kw"], "rising": True, "value": r["value"]}
                    for r in related
                ],
                "geo_top_regions": [
                    {"region": r["region"], "value": r["value"]}
                    for r in regions
                ],
            }
            # confidence from data_points + native confidence field
            dp = int(row["data_points"] or 0)
            native_conf = (row["confidence"] or "").lower()
            base = 0.4 + min(0.4, dp / 52 * 0.4)
            if native_conf == "high":
                base += 0.1
            elif native_conf == "low":
                base -= 0.15
            return (data, max(0.1, min(0.95, base)), "gtrends-bulk:trends_v3")
    except Exception as e:
        return (None, 0.0, f"error:gtrends:{type(e).__name__}")


# ── Collector 3: Amazon ABA signal (keyword) ─────────────────────────────────

def _amazon_aba(kw: str, snapshot_date: str, marketplace: str = "US") -> tuple[Optional[dict], float, str]:
    """Read aba_keywords + kw_analysis from amz-keyword-scanner.

    rank_velocity_4w = current_rank - rank_at_4_weeks_ago. Negative = improving.
    """
    try:
        with _readonly_ext_conn(AMZ_DB) as conn:
            if conn is None:
                return (None, 0.0, "amz:db_missing")
            # 5 most recent weekly snapshots (newest first)
            recent = conn.execute(
                """
                SELECT scan_week, rank_current, rank_previous, rank_change, direction, velocity
                FROM aba_keywords
                WHERE keyword=? AND marketplace=?
                ORDER BY scan_week DESC LIMIT 5
                """, (kw, marketplace),
            ).fetchall()
            if not recent:
                return (None, 0.0, "amz:no_data")
            cur = recent[0]
            # 4-week velocity = current rank - rank 4 weeks ago (if available)
            rank_4w_ago = recent[-1]["rank_current"] if len(recent) >= 4 else cur["rank_previous"]
            rank_velocity_4w = (cur["rank_current"] or 0) - (rank_4w_ago or 0)
            analysis = conn.execute(
                "SELECT is_brand, category_guess, velocity_score, opportunity FROM kw_analysis WHERE keyword=?",
                (kw,),
            ).fetchone()
            data = {
                "rank_us_current": int(cur["rank_current"] or 0),
                "rank_us_previous": int(cur["rank_previous"] or 0),
                "rank_change": int(cur["rank_change"] or 0),
                "rank_velocity_4w": int(rank_velocity_4w or 0),
                "direction": cur["direction"] or "flat",
                "marketplace": marketplace,
                "scan_week": cur["scan_week"],
                "is_brand": bool(analysis["is_brand"]) if analysis else False,
                "category_guess": (analysis["category_guess"] if analysis else "") or "",
                "velocity_score": round(analysis["velocity_score"] or 0, 2) if analysis else 0.0,
                "opportunity": (analysis["opportunity"] if analysis else "") or "",
            }
            # confidence: more weeks observed = higher
            conf = min(0.9, 0.4 + len(recent) / 5 * 0.5)
            return (data, conf, "amz-keyword-scanner:aba_keywords")
    except Exception as e:
        return (None, 0.0, f"error:amz:{type(e).__name__}")


# ── Collector 4: Commercial signals (brand) ──────────────────────────────────

def _commercial(domain: str, snapshot_date: str) -> tuple[Optional[dict], float, str]:
    """Aggregate signals.combined_signal_score over recent ads for one brand."""
    try:
        from .signals import combined_signal_score, extract_signals
    except Exception as e:
        return (None, 0.0, f"error:commercial:import:{type(e).__name__}")
    try:
        with _pipeline_conn() as conn:
            d_30 = _shift_days(snapshot_date, -30)
            rows = conn.execute(
                """
                SELECT title, body, link_description, link_caption
                FROM ad_archive
                WHERE domain=? AND scan_date >= ?
                ORDER BY scan_date DESC LIMIT 10
                """, (domain, d_30),
            ).fetchall()
        if not rows:
            return (None, 0.0, "pipeline.db:no_ads")
        scores: list[int] = []
        dim_acc: dict[str, int] = {}
        for r in rows:
            blob = " ".join(filter(None, [
                r["title"], r["body"], r["link_description"], r["link_caption"],
            ]))
            try:
                sigs = extract_signals(blob)
                s = combined_signal_score(sigs)
                scores.append(s)
                # dimensional bookkeeping (approximate, follow signals.py weights)
                if sigs.get("price_main"):
                    dim_acc["price_band"] = dim_acc.get("price_band", 0) + 5
                if sigs.get("has_discount"):
                    dim_acc["discount"] = dim_acc.get("discount", 0) + 3
                if sigs.get("review_count_max", 0) >= 1000:
                    dim_acc["reviews"] = dim_acc.get("reviews", 0) + 6
                elif sigs.get("review_count_max", 0) >= 100:
                    dim_acc["reviews"] = dim_acc.get("reviews", 0) + 3
                if sigs.get("star_rating", 0) >= 4.5:
                    dim_acc["stars"] = dim_acc.get("stars", 0) + 3
                if sigs.get("has_urgency"):
                    dim_acc["urgency"] = dim_acc.get("urgency", 0) + 2
                if sigs.get("guarantee_markers"):
                    dim_acc["guarantee"] = dim_acc.get("guarantee", 0) + 4
            except Exception:
                continue
        if not scores:
            return (None, 0.0, "pipeline.db:extract_failed")
        # report median (more robust to outliers than mean)
        scores.sort()
        median = scores[len(scores) // 2]
        # normalize dims to averages
        dims_avg = {k: round(v / max(len(scores), 1), 2) for k, v in dim_acc.items()}
        data = {
            "combined_score": int(median),
            "sample_size": len(scores),
            "dimensions": dims_avg,
        }
        conf = min(0.9, 0.4 + len(scores) / 10 * 0.5)
        return (data, conf, "pipeline.db:ad_archive+signals.py")
    except Exception as e:
        return (None, 0.0, f"error:commercial:{type(e).__name__}")


# ── Collector 5: Insights 2026 signal (brand) ────────────────────────────────

def _insights_2026(domain: str, snapshot_date: str) -> tuple[Optional[dict], float, str]:
    """Aggregate insights_2026.insight_score over recent ads for one brand."""
    try:
        from .insights_2026 import insight_score
    except Exception as e:
        return (None, 0.0, f"error:insights_2026:import:{type(e).__name__}")
    try:
        with _pipeline_conn() as conn:
            d_30 = _shift_days(snapshot_date, -30)
            rows = conn.execute(
                """
                SELECT title, body, link_url, page_name
                FROM ad_archive
                WHERE domain=? AND scan_date >= ?
                ORDER BY scan_date DESC LIMIT 10
                """, (domain, d_30),
            ).fetchall()
        if not rows:
            return (None, 0.0, "pipeline.db:no_ads")
        bonuses: list[int] = []
        tag_counter: Counter[str] = Counter()
        for r in rows:
            try:
                b, tags = insight_score(
                    ad_title=r["title"] or "",
                    ad_caption=r["body"] or "",
                    landing_page=r["link_url"] or "",
                    brand_name=r["page_name"] or "",
                )
                bonuses.append(int(b))
                for t in tags:
                    tag_counter[t] += 1
            except Exception:
                continue
        if not bonuses:
            return (None, 0.0, "pipeline.db:scoring_failed")
        bonuses.sort()
        median = bonuses[len(bonuses) // 2]
        # Only include tags hit by at least 2 ads (denoise single-ad tags)
        strong_tags = [t for t, n in tag_counter.most_common() if n >= 2]
        if not strong_tags and tag_counter:
            # fall back to top single-occurrence tag
            strong_tags = [tag_counter.most_common(1)[0][0]]
        data = {
            "bonus": int(median),
            "sample_size": len(bonuses),
            "tags": strong_tags,
        }
        conf = min(0.85, 0.4 + len(bonuses) / 10 * 0.45)
        return (data, conf, "pipeline.db:ad_archive+insights_2026.py")
    except Exception as e:
        return (None, 0.0, f"error:insights_2026:{type(e).__name__}")


# ── Placeholders (Layer 1.5 to be filled later) ──────────────────────────────

def _placeholder(_entity: str, _date: str, name: str) -> tuple[None, float, str]:
    return (None, 0.0, f"placeholder:{name}")


# ── Public collection orchestration ──────────────────────────────────────────

def collect_brand_signals(
    domain: str,
    snapshot_date: Optional[str] = None,
    persist: bool = True,
) -> dict[str, dict]:
    """Collect all brand-applicable signals for one domain on one date."""
    snapshot_date = snapshot_date or _today_local()
    out: dict[str, dict] = {}
    for sig_type, fn in (
        ("fb_ad", _fb_ad_for_brand),
        ("commercial", _commercial),
        ("insights_2026", _insights_2026),
    ):
        data, conf, src = fn(domain, snapshot_date)
        out[sig_type] = {"data": data, "confidence": conf, "source": src}
        if persist:
            write_snapshot("brand", domain, sig_type, data, conf, src, snapshot_date)
    # placeholders
    for sig_type in ("lp", "geo"):
        data, conf, src = _placeholder(domain, snapshot_date, sig_type)
        out[sig_type] = {"data": data, "confidence": conf, "source": src}
        if persist:
            write_snapshot("brand", domain, sig_type, data, conf, src, snapshot_date)
    return out


def collect_keyword_signals(
    kw: str,
    snapshot_date: Optional[str] = None,
    persist: bool = True,
    geo: str = "US",
) -> dict[str, dict]:
    """Collect all keyword-applicable signals for one keyword on one date."""
    snapshot_date = snapshot_date or _today_local()
    out: dict[str, dict] = {}
    for sig_type, fn in (
        ("fb_ad", lambda k, d: _fb_ad_for_keyword(k, d)),
        ("google_trends", lambda k, d: _google_trends(k, d, geo=geo)),
        ("amazon_aba", lambda k, d: _amazon_aba(k, d)),
    ):
        data, conf, src = fn(kw, snapshot_date)
        out[sig_type] = {"data": data, "confidence": conf, "source": src}
        if persist:
            write_snapshot("keyword", kw, sig_type, data, conf, src, snapshot_date)
    # placeholders
    for sig_type in ("tiktok", "seasonality"):
        data, conf, src = _placeholder(kw, snapshot_date, sig_type)
        out[sig_type] = {"data": data, "confidence": conf, "source": src}
        if persist:
            write_snapshot("keyword", kw, sig_type, data, conf, src, snapshot_date)
    return out


def get_full_snapshot(entity_type: str, entity_ref: str, on_date: Optional[str] = None) -> dict:
    """Return all signal types for one entity at-or-before `on_date`."""
    if entity_type == "brand":
        candidate_signals = ("fb_ad", "commercial", "insights_2026", "lp", "geo")
    elif entity_type in ("keyword", "niche"):
        candidate_signals = ("fb_ad", "google_trends", "amazon_aba", "tiktok", "seasonality", "geo")
    else:
        candidate_signals = ALL_SIGNAL_TYPES
    signals = {}
    present = 0
    for sig in candidate_signals:
        snap = get_snapshot(entity_type, entity_ref, sig, on_date=on_date)
        signals[sig] = snap
        if snap and snap.get("confidence", 0) > 0:
            present += 1
    v0_present = sum(
        1 for s in V0_SIGNAL_TYPES
        if signals.get(s) and signals[s].get("confidence", 0) > 0
    )
    completeness = round(v0_present / len(V0_SIGNAL_TYPES), 3)
    snapshot_dates = [
        s["snapshot_date"] for s in signals.values()
        if s and s.get("snapshot_date")
    ]
    latest_date = max(snapshot_dates) if snapshot_dates else None
    return {
        "entity_type": entity_type,
        "entity_ref": entity_ref,
        "snapshot_date": latest_date,
        "signals": signals,
        "completeness": completeness,
    }


# ── Module-level health (used by /v3/health) ─────────────────────────────────

def layer1_health() -> dict:
    """Self-report Layer 1 status for /v3/health."""
    try:
        _ensure_schema()
    except Exception as e:
        return {"status": "error", "reason": f"schema:{e}"}
    try:
        cov = get_coverage()
    except Exception as e:
        return {"status": "error", "reason": f"coverage:{e}"}
    gtrends_ok = GTRENDS_DB.exists()
    amz_ok = AMZ_DB.exists()
    status = "green"
    if cov["completeness"] < 0.3:
        status = "yellow"
    if not gtrends_ok and not amz_ok:
        status = "yellow"
    return {
        "status": status,
        "coverage": cov,
        "external_dbs": {
            "gtrends": "ok" if gtrends_ok else "missing",
            "amazon_aba": "ok" if amz_ok else "missing",
        },
        "schema_version": SCHEMA_VERSION,
        "v0_signals": list(V0_SIGNAL_TYPES),
        "placeholder_signals": list(PLACEHOLDER_SIGNAL_TYPES),
    }

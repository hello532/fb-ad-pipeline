"""Shared SQLite schema for signals.db (v2).

Three tables:

  signals       — one row per (keyword, source). The original raw signal.
  keyword_attrs — one row per keyword. Cross-source aggregated attributes
                  (category, momentum, freshness, source_count, etc.).
                  Maintained by aggregate_keywords.py downstream — but
                  collectors can already write hints here when they know
                  something authoritative (e.g. amazon_movers knows the
                  exact category).
  sku_details   — one row per ASIN / TikTok product / Shopify product
                  discovered. Carries price / rating / image_url so
                  shopify-product-pipeline + landing-page renderer can
                  pull rich content without re-scraping.

All three use upsert-on-conflict so collectors are idempotent.

PRIMARY KEY (keyword, source) means each collector owns its own slice;
re-running a collector overwrites stale rows for that source only.
"""
from __future__ import annotations
import sqlite3
import json
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping, Any

DB_PATH = Path(__file__).parent / "signals.db"

SCHEMA = """
-- ============================================================
-- 1. SIGNALS — raw per-source observations
-- ============================================================
CREATE TABLE IF NOT EXISTS signals (
  keyword       TEXT NOT NULL,
  source        TEXT NOT NULL,
  score         REAL NOT NULL,
  raw_metric    REAL,
  category      TEXT,
  first_seen    DATE,
  last_seen     DATE,
  metadata      TEXT,                  -- JSON
  fetched_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (keyword, source)
);
CREATE INDEX IF NOT EXISTS idx_sig_source     ON signals(source);
CREATE INDEX IF NOT EXISTS idx_sig_keyword    ON signals(keyword);
CREATE INDEX IF NOT EXISTS idx_sig_last_seen  ON signals(last_seen);
CREATE INDEX IF NOT EXISTS idx_sig_score      ON signals(score);
CREATE INDEX IF NOT EXISTS idx_sig_category   ON signals(category);

-- ============================================================
-- 2. KEYWORD_ATTRS — cross-source aggregated facts about a keyword
-- ============================================================
CREATE TABLE IF NOT EXISTS keyword_attrs (
  keyword         TEXT PRIMARY KEY,
  primary_category TEXT,               -- voted across sources
  source_count    INTEGER DEFAULT 0,   -- how many sources cover it
  weighted_score  REAL,                -- aggregate 0-100
  momentum        REAL,                -- (recent score - older score) / older
  freshness_days  INTEGER,             -- days since last_seen across all sources
  first_seen      DATE,
  last_seen       DATE,
  -- pre-decision constraints (filled by category_constraints layer later)
  fb_restricted   INTEGER,             -- 0/1 booleans stored as int
  shipping_complex TEXT,               -- 'low' / 'med' / 'high' / 'banned'
  est_margin      REAL,                -- 0.0-1.0
  est_return_rate REAL,                -- 0.0-1.0
  notes           TEXT,
  updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_attr_cat   ON keyword_attrs(primary_category);
CREATE INDEX IF NOT EXISTS idx_attr_score ON keyword_attrs(weighted_score);

-- ============================================================
-- 3. SKU_DETAILS — concrete products discovered while collecting
-- ============================================================
CREATE TABLE IF NOT EXISTS sku_details (
  sku_id        TEXT NOT NULL,         -- ASIN / TikTok product_id / shop slug
  source        TEXT NOT NULL,         -- 'amazon' / 'tiktok' / 'fb_landing'
  keyword       TEXT,                  -- the keyword that surfaced this SKU
  title         TEXT,
  brand         TEXT,
  price_usd     REAL,
  rating        REAL,                  -- 1-5
  reviews_count INTEGER,
  image_url     TEXT,
  product_url   TEXT,
  category      TEXT,
  metadata      TEXT,                  -- JSON
  fetched_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (sku_id, source)
);
CREATE INDEX IF NOT EXISTS idx_sku_keyword  ON sku_details(keyword);
CREATE INDEX IF NOT EXISTS idx_sku_category ON sku_details(category);
CREATE INDEX IF NOT EXISTS idx_sku_price    ON sku_details(price_usd);

-- ============================================================
-- 4. COLLECTOR_RUNS — health log per collector run
-- ============================================================
CREATE TABLE IF NOT EXISTS collector_runs (
  source        TEXT NOT NULL,
  started_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  finished_at   TIMESTAMP,
  rows_written  INTEGER DEFAULT 0,
  status        TEXT,
  error         TEXT,
  duration_sec  REAL,
  PRIMARY KEY (source, started_at)
);
CREATE INDEX IF NOT EXISTS idx_run_source ON collector_runs(source);
"""


# Lightweight forward-migrations: (table, column, type)
# CREATE TABLE IF NOT EXISTS will not ALTER an existing table, so we
# defensively add new columns here. Each one is wrapped in try/except —
# OperationalError "duplicate column" is the success case.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("collector_runs", "duration_sec",  "REAL"),
    ("keyword_attrs",  "fb_restricted",  "INTEGER"),
    ("keyword_attrs",  "shipping_complex", "TEXT"),
    ("keyword_attrs",  "est_margin",     "REAL"),
    ("keyword_attrs",  "est_return_rate", "REAL"),
    ("keyword_attrs",  "notes",          "TEXT"),
    ("sku_details",    "rating",         "REAL"),
    ("sku_details",    "reviews_count",  "INTEGER"),
    ("sku_details",    "image_url",      "TEXT"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, col, ctype in _MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError as e:
            # already exists (most common) or table missing (first run)
            msg = str(e).lower()
            if "duplicate column" in msg or "no such table" in msg:
                continue
            raise


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(db_path) if db_path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    return conn


# ---------- signals ----------
def upsert_signal(
    conn: sqlite3.Connection,
    keyword: str,
    source: str,
    score: float,
    raw_metric: float | None = None,
    category: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    today = date.today().isoformat()
    meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
    conn.execute(
        """
        INSERT INTO signals
          (keyword, source, score, raw_metric, category, first_seen, last_seen, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(keyword, source) DO UPDATE SET
          score      = excluded.score,
          raw_metric = excluded.raw_metric,
          category   = COALESCE(excluded.category, signals.category),
          last_seen  = excluded.last_seen,
          metadata   = excluded.metadata,
          fetched_at = CURRENT_TIMESTAMP
        """,
        (keyword.strip().lower(), source, float(score),
         None if raw_metric is None else float(raw_metric),
         category, today, today, meta_json),
    )


def upsert_many(
    conn: sqlite3.Connection,
    source: str,
    rows: Iterable[Mapping[str, Any]],
) -> int:
    n = 0
    conn.execute("BEGIN")
    try:
        for r in rows:
            upsert_signal(
                conn,
                keyword=r["keyword"],
                source=source,
                score=r["score"],
                raw_metric=r.get("raw_metric"),
                category=r.get("category"),
                metadata=r.get("metadata"),
            )
            n += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n


# ---------- sku_details ----------
def upsert_sku(
    conn: sqlite3.Connection,
    sku_id: str,
    source: str,
    *,
    keyword: str | None = None,
    title: str | None = None,
    brand: str | None = None,
    price_usd: float | None = None,
    rating: float | None = None,
    reviews_count: int | None = None,
    image_url: str | None = None,
    product_url: str | None = None,
    category: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
    conn.execute(
        """
        INSERT INTO sku_details
          (sku_id, source, keyword, title, brand, price_usd, rating,
           reviews_count, image_url, product_url, category, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sku_id, source) DO UPDATE SET
          keyword       = COALESCE(excluded.keyword,       sku_details.keyword),
          title         = COALESCE(excluded.title,         sku_details.title),
          brand         = COALESCE(excluded.brand,         sku_details.brand),
          price_usd     = COALESCE(excluded.price_usd,     sku_details.price_usd),
          rating        = COALESCE(excluded.rating,        sku_details.rating),
          reviews_count = COALESCE(excluded.reviews_count, sku_details.reviews_count),
          image_url     = COALESCE(excluded.image_url,     sku_details.image_url),
          product_url   = COALESCE(excluded.product_url,   sku_details.product_url),
          category      = COALESCE(excluded.category,      sku_details.category),
          metadata      = COALESCE(excluded.metadata,      sku_details.metadata),
          fetched_at    = CURRENT_TIMESTAMP
        """,
        (sku_id, source, keyword, title, brand, price_usd, rating,
         reviews_count, image_url, product_url, category, meta_json),
    )


def upsert_skus(conn: sqlite3.Connection, source: str,
                rows: Iterable[Mapping[str, Any]]) -> int:
    n = 0
    conn.execute("BEGIN")
    try:
        for r in rows:
            upsert_sku(conn, sku_id=r["sku_id"], source=source, **{
                k: v for k, v in r.items() if k != "sku_id"
            })
            n += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return n


# ---------- keyword_attrs ----------
def upsert_attrs(
    conn: sqlite3.Connection,
    keyword: str,
    **fields,
) -> None:
    """Sparse-update style upsert. Only non-None fields overwrite."""
    cols = ["keyword"] + list(fields.keys())
    placeholders = ",".join("?" * len(cols))
    set_clause = ",".join(
        f"{k} = COALESCE(excluded.{k}, keyword_attrs.{k})"
        for k in fields.keys()
    )
    conn.execute(
        f"""
        INSERT INTO keyword_attrs ({",".join(cols)})
        VALUES ({placeholders})
        ON CONFLICT(keyword) DO UPDATE SET
          {set_clause},
          updated_at = CURRENT_TIMESTAMP
        """,
        [keyword.strip().lower()] + list(fields.values()),
    )


# ---------- run logging ----------
def log_run(
    conn: sqlite3.Connection,
    source: str,
    rows_written: int,
    status: str = "ok",
    error: str | None = None,
    duration_sec: float | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO collector_runs
          (source, finished_at, rows_written, status, error, duration_sec)
        VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
        """,
        (source, rows_written, status, error, duration_sec),
    )


# ---------- maintenance ----------
def cleanup_stale(conn: sqlite3.Connection, days: int = 30) -> int:
    """Delete signal rows whose last_seen is older than `days`. Returns count."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    cur = conn.execute("DELETE FROM signals WHERE last_seen < ?", (cutoff,))
    return cur.rowcount or 0


def vacuum(conn: sqlite3.Connection) -> None:
    conn.execute("VACUUM")


# ---------- snapshots ----------
def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for src, n in conn.execute(
        "SELECT source, COUNT(*) FROM signals GROUP BY source ORDER BY source"
    ):
        out[f"signals.{src}"] = n
    out["signals._total_keywords"] = conn.execute(
        "SELECT COUNT(DISTINCT keyword) FROM signals"
    ).fetchone()[0]
    out["sku_details"] = conn.execute(
        "SELECT COUNT(*) FROM sku_details"
    ).fetchone()[0]
    out["keyword_attrs"] = conn.execute(
        "SELECT COUNT(*) FROM keyword_attrs"
    ).fetchone()[0]
    return out


def latest_runs(conn: sqlite3.Connection, n: int = 10) -> list[tuple]:
    return conn.execute(
        """SELECT source, started_at, rows_written, status, duration_sec, error
           FROM collector_runs ORDER BY started_at DESC LIMIT ?""",
        (n,),
    ).fetchall()


if __name__ == "__main__":
    c = connect()
    upsert_signal(c, "magnetic phone holder", "test", 42.0,
                  raw_metric=128, category="auto", metadata={"hello": "world"})
    upsert_sku(c, sku_id="B0TEST123", source="amazon",
               keyword="magnetic phone holder", title="Test Holder",
               price_usd=12.99, rating=4.5, reviews_count=2300,
               category="auto")
    upsert_attrs(c, "magnetic phone holder",
                 primary_category="auto", source_count=1, weighted_score=42.0)
    print("schema v2 OK")
    for k, v in stats(c).items():
        print(f"  {k}: {v}")
    # cleanup test row
    c.execute("DELETE FROM signals WHERE source='test'")
    c.execute("DELETE FROM sku_details WHERE source='amazon' AND sku_id='B0TEST123'")
    c.execute("DELETE FROM keyword_attrs WHERE keyword='magnetic phone holder' AND source_count=1")

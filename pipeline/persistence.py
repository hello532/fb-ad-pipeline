"""pipeline/persistence.py — SQLite 持久化层 (stdlib sqlite3).

作为 brand_intelligence.json / LEARNED_FILE / scans 的索引副本. JSON 仍是 source of truth.
只在主进程调用, 避免 ProcessPoolExecutor 多写冲突.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from core.logger import get_logger

from .paths import NEW_DIR
from .runtime import resolve_env_path
from .shards import ensure_rotation, attach_and_build_views, months_for_days

log = get_logger(__name__)

DB_FILE = resolve_env_path("FB_DB_FILE", NEW_DIR / "pipeline.db", NEW_DIR)

SCHEMA = """
CREATE TABLE IF NOT EXISTS brands (
  domain TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  appearance_count INTEGER DEFAULT 1,
  total_ads_across_scans INTEGER DEFAULT 0,
  highest_likes INTEGER DEFAULT 0,
  orbit_score INTEGER DEFAULT 0,
  raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_brands_orbit ON brands(orbit_score DESC);
CREATE INDEX IF NOT EXISTS idx_brands_appear ON brands(appearance_count DESC);

CREATE TABLE IF NOT EXISTS brand_keywords (
  domain TEXT NOT NULL,
  keyword TEXT NOT NULL,
  PRIMARY KEY(domain, keyword)
);
CREATE INDEX IF NOT EXISTS idx_bk_keyword ON brand_keywords(keyword);

CREATE TABLE IF NOT EXISTS brand_page_names (
  domain TEXT NOT NULL,
  name TEXT NOT NULL,
  PRIMARY KEY(domain, name)
);

CREATE TABLE IF NOT EXISTS scans (
  keyword TEXT NOT NULL,
  scan_date TEXT NOT NULL,
  ads_count INTEGER,
  brands_count INTEGER,
  corrected_file TEXT,
  PRIMARY KEY(keyword, scan_date)
);
CREATE INDEX IF NOT EXISTS idx_scans_date ON scans(scan_date DESC);

CREATE TABLE IF NOT EXISTS learned_keywords (
  keyword TEXT NOT NULL,
  kw_type TEXT NOT NULL,
  subtype TEXT,
  source_kw TEXT NOT NULL,
  learned_date TEXT NOT NULL,
  PRIMARY KEY(keyword, kw_type, source_kw)
);
CREATE INDEX IF NOT EXISTS idx_learned_type ON learned_keywords(kw_type);

-- v9.1: per-ad rich archive (populated from graphql sniffer + dom extractor)
CREATE TABLE IF NOT EXISTS ad_archive (
  ad_archive_id TEXT PRIMARY KEY,
  page_id TEXT,
  page_name TEXT,
  domain TEXT,
  keyword TEXT,
  scan_date TEXT,
  start_date_iso TEXT,
  end_date_iso TEXT,
  is_active INTEGER,
  active_days INTEGER,
  display_format TEXT,
  archive_type TEXT,
  cta_type TEXT,
  cta_text TEXT,
  title TEXT,
  body TEXT,
  link_url TEXT,
  link_caption TEXT,
  link_description TEXT,
  ig_handle TEXT,
  ig_followers INTEGER,
  byline TEXT,
  page_likes INTEGER,
  page_categories TEXT,
  publisher_platforms TEXT,
  spend_lower INTEGER,
  spend_upper INTEGER,
  imp_lower INTEGER,
  imp_upper INTEGER,
  currency TEXT,
  target_locations TEXT,
  target_ages TEXT,
  target_genders TEXT,
  languages TEXT,
  regional_distribution TEXT,
  demographic_distribution TEXT,
  cards_count INTEGER,
  cards TEXT,
  video_duration_sec REAL,
  video_url TEXT,
  image_url TEXT,
  ad_snapshot_url TEXT,
  total_active_time INTEGER,
  eu_total_reach INTEGER,
  eu_political_ad INTEGER,
  has_user_reported INTEGER,
  contains_sensitive_content INTEGER,
  contains_digital_created_media INTEGER,
  branded_content INTEGER,
  collation_count INTEGER,
  source TEXT,
  inserted_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ad_archive_keyword ON ad_archive(keyword);
CREATE INDEX IF NOT EXISTS idx_ad_archive_domain ON ad_archive(domain);
CREATE INDEX IF NOT EXISTS idx_ad_archive_scan_date ON ad_archive(scan_date);
CREATE INDEX IF NOT EXISTS idx_ad_archive_active ON ad_archive(is_active);
CREATE INDEX IF NOT EXISTS idx_ad_archive_start ON ad_archive(start_date_iso);
CREATE INDEX IF NOT EXISTS idx_ad_archive_page_id ON ad_archive(page_id);
CREATE INDEX IF NOT EXISTS idx_ad_archive_page_name ON ad_archive(page_name);
CREATE INDEX IF NOT EXISTS idx_ad_archive_active_days ON ad_archive(active_days);
CREATE INDEX IF NOT EXISTS idx_ad_archive_inserted ON ad_archive(inserted_at);
CREATE INDEX IF NOT EXISTS idx_ad_archive_inserted_domain ON ad_archive(inserted_at, domain);

-- v10: FTS5 full-text index for body/title search at 10M scale
CREATE VIRTUAL TABLE IF NOT EXISTS ad_fts USING fts5(
  ad_archive_id UNINDEXED,
  title,
  body,
  content='ad_archive',
  content_rowid='rowid'
);

-- v10: recommendations table managed by recommendations.py (owns its own schema)

-- v11: auxiliary tables for incremental brand aggregation
CREATE TABLE IF NOT EXISTS brand_platforms (
  domain TEXT NOT NULL,
  platform TEXT NOT NULL,
  PRIMARY KEY(domain, platform)
);
CREATE TABLE IF NOT EXISTS brand_landing_urls (
  domain TEXT NOT NULL,
  url TEXT NOT NULL,
  PRIMARY KEY(domain, url)
);
"""


class Persistence:
    def __init__(self, db_file: Path = DB_FILE):
        self.db_file = Path(db_file)
        self._ensure_schema()
        # 进程级一次性月份轮转检查 (pipeline.db 可能被归档到 shards/, 新建空库)
        ensure_rotation(self.db_file, SCHEMA)

    @staticmethod
    def _apply_pragmas(conn: sqlite3.Connection, *, foreign_keys: bool = False) -> None:
        """Apply consistent SQLite PRAGMAs across all connections.

        Centralizes tuning so secondary connections (schema bootstrap, vacuum,
        FTS rebuild, write path) don't drift from the read path. Mismatched
        cache/mmap settings cause silent perf regressions on long-running ops.
        """
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-131072")      # 128MB page cache
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=1073741824")    # 1GB mmap (covers 425MB DB)
        conn.execute("PRAGMA busy_timeout=10000")      # 10s wait on lock contention
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")

    def _ensure_schema(self) -> None:
        # 直接开原生连接(不走 _conn, 避免 attach 循环)
        conn = sqlite3.connect(str(self.db_file))
        self._apply_pragmas(conn)
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _conn(self, *, months: Optional[list[str]] = None):
        """Read connection with shard views.

        months=None  → use the thread-local pool (shards pre-attached, views
                       built once). Hot path for the API server: amortizes the
                       connect+ATTACH+CREATE-VIEW cost across requests instead
                       of paying it every time.
        months=[...] → ephemeral connection with selective ATTACH. Used by the
                       few callers that want to narrow UNION ALL fan-out for a
                       date window.
        """
        if months is not None:
            conn = sqlite3.connect(str(self.db_file))
            conn.row_factory = sqlite3.Row
            self._apply_pragmas(conn, foreign_keys=True)
            try:
                attach_and_build_views(conn, months=months)
            except Exception as e:
                log.info(f"persistence: attach shards 失败, 仅查当前库: {e}")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
            return

        with self._read_pool().acquire() as conn:
            yield conn

    def _read_pool(self):
        pool = getattr(self, "_pool_inst", None)
        if pool is None:
            from .db_pool import ReadPool

            def _init(c: sqlite3.Connection) -> None:
                self._apply_pragmas(c, foreign_keys=True)
                try:
                    attach_and_build_views(c)
                except Exception as e:
                    log.info(f"persistence: attach shards 失败 (pool init): {e}")

            pool = ReadPool(self.db_file, _init)
            self._pool_inst = pool
        return pool

    def close_pool(self) -> None:
        pool = getattr(self, "_pool_inst", None)
        if pool is not None:
            pool.close_all()
            self._pool_inst = None

    # ── 写入 ────────────────────────────────────────────────────────────────

    def upsert_brand_incremental(self, comp: dict, keyword: str) -> None:
        """v11: Single-comp incremental upsert — no full brands dict needed.

        Called once per competitor per file. Uses SQL-level aggregation
        (MAX, +) to avoid needing the whole dict in memory.
        """
        from .domain import canonical_domain, resolve_domain
        brand_name = comp.get('brand', '')
        domain = comp.get('landing', '') or resolve_domain(brand_name)
        if not domain:
            return
        canon = canonical_domain(domain)
        now_iso = datetime.now().strftime('%Y-%m-%d')
        ads_count = int(comp.get('ads', 0) or 0)
        likes = int(comp.get('likes', 0) or 0)
        ad_age = int(comp.get('ad_age_days', 0) or 0)

        with self._conn() as conn:
            # Check if (domain, keyword) pair already exists — prevents double-increment
            already_seen = False
            if keyword:
                cur = conn.execute(
                    "SELECT 1 FROM brand_keywords WHERE domain = ? AND keyword = ?",
                    (canon, keyword),
                )
                already_seen = cur.fetchone() is not None

            # Upsert brands row — only increment appearance_count when keyword is new
            conn.execute(
                """INSERT INTO brands(domain, canonical_name, first_seen, last_seen,
                                      appearance_count, total_ads_across_scans,
                                      highest_likes, orbit_score, raw_json)
                   VALUES (?,?,?, ?, 1, ?, ?, 0, '{}')
                   ON CONFLICT(domain) DO UPDATE SET
                     canonical_name = CASE
                       WHEN excluded.canonical_name <> '' THEN excluded.canonical_name
                       ELSE brands.canonical_name END,
                     last_seen = excluded.last_seen,
                     appearance_count = CASE
                       WHEN ? THEN brands.appearance_count
                       ELSE brands.appearance_count + 1 END,
                     total_ads_across_scans = brands.total_ads_across_scans + excluded.total_ads_across_scans,
                     highest_likes = MAX(brands.highest_likes, excluded.highest_likes)""",
                (canon, brand_name, now_iso, now_iso, ads_count, likes, already_seen),
            )
            # brand_keywords
            if keyword:
                conn.execute(
                    "INSERT OR IGNORE INTO brand_keywords(domain, keyword) VALUES (?,?)",
                    (canon, keyword),
                )
            # brand_page_names
            for nm in [brand_name] + (comp.get('related_brands') or []):
                if nm:
                    conn.execute(
                        "INSERT OR IGNORE INTO brand_page_names(domain, name) VALUES (?,?)",
                        (canon, nm),
                    )
            # brand_platforms (new table for orbit_score calculation)
            plats = comp.get('platforms', {})
            if isinstance(plats, dict):
                for p in plats:
                    if p:
                        try:
                            conn.execute(
                                "INSERT OR IGNORE INTO brand_platforms(domain, platform) VALUES (?,?)",
                                (canon, p),
                            )
                        except Exception:
                            pass
            # landing_urls — store top 5 in a lightweight table
            url = comp.get('landing_url', '')
            if url:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO brand_landing_urls(domain, url) VALUES (?,?)",
                        (canon, url),
                    )
                except Exception:
                    pass

    def recalc_orbit_scores(self) -> int:
        """v11: Recalculate orbit_score for all brands from SQL.

        Formula (mirrors intelligence.py):
          MIN(appearance_count * 15 + total_ads_across_scans * 3
              + page_names_count * 10 + platforms_count * 5
              + highest_likes * 5 / 10000, 100)
        """
        with self._conn() as conn:
            conn.execute("""
                UPDATE brands SET orbit_score = MIN(
                    brands.appearance_count * 15
                    + brands.total_ads_across_scans * 3
                    + COALESCE((SELECT COUNT(*) FROM brand_page_names WHERE domain = brands.domain), 0) * 10
                    + COALESCE((SELECT COUNT(*) FROM brand_platforms WHERE domain = brands.domain), 0) * 5
                    + brands.highest_likes / 10000 * 5,
                    100
                )
            """)
            n = conn.execute("SELECT changes()").fetchone()[0]
        log.info(f"persistence: recalculated orbit_score for {n} brands")
        return n

    def list_orbit_brands_from_sql(self, min_appearance: int = 3,
                                    limit: int = 200) -> list[dict]:
        """v11: Generate orbit_brands list from SQL (no JSON file needed)."""
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT b.domain, b.canonical_name AS name,
                          b.orbit_score AS score,
                          b.appearance_count AS scans,
                          GROUP_CONCAT(bk.keyword, ',') AS keywords
                   FROM brands b
                   JOIN brand_keywords bk ON bk.domain = b.domain
                   WHERE b.appearance_count >= ?
                   GROUP BY b.domain
                   ORDER BY b.orbit_score DESC, b.appearance_count DESC
                   LIMIT ?""",
                (min_appearance, limit),
            )]

    def export_brands_json(self, limit: int = 50000) -> dict:
        """v11: Export brands from SQLite to the legacy JSON format.

        Only called when the JSON file is needed (e.g. show_orbits fallback).
        At 10M scale, the JSON is capped at `limit` brands.
        """
        with self._conn() as conn:
            brands = {}
            for r in conn.execute(
                """SELECT domain, canonical_name, first_seen, last_seen,
                          appearance_count, total_ads_across_scans,
                          highest_likes, orbit_score
                   FROM brands
                   ORDER BY orbit_score DESC
                   LIMIT ?""",
                (limit,),
            ):
                d = r['domain']
                brands[d] = {
                    'canonical_name': r['canonical_name'],
                    'first_seen': r['first_seen'],
                    'last_seen': r['last_seen'],
                    'appearance_count': r['appearance_count'],
                    'total_ads_across_scans': r['total_ads_across_scans'],
                    'highest_likes': r['highest_likes'],
                    'orbit_score': r['orbit_score'],
                    'keywords_appeared_in': [
                        kw['keyword'] for kw in conn.execute(
                            "SELECT keyword FROM brand_keywords WHERE domain = ?", (d,)
                        )
                    ],
                    'all_page_names': [
                        nm['name'] for nm in conn.execute(
                            "SELECT name FROM brand_page_names WHERE domain = ?", (d,)
                        )
                    ],
                    'landing_urls': [
                        u['url'] for u in conn.execute(
                            "SELECT url FROM brand_landing_urls WHERE domain = ? ORDER BY rowid LIMIT 5", (d,)
                        )
                    ],
                    'platforms_observed': [
                        p['platform'] for p in conn.execute(
                            "SELECT platform FROM brand_platforms WHERE domain = ?", (d,)
                        )
                    ],
                    'max_ad_age': 0,
                }
            orbit_brands = self.list_orbit_brands_from_sql()
            return {
                'version': '8.0',
                'last_updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
                'brands': brands,
                'orbit_brands': orbit_brands,
            }

    def upsert_brands_bulk(self, brands: dict) -> int:
        """brands: brand_intelligence.json['brands'] 字典 — {domain: entry}"""
        if not brands:
            return 0
        rows = []
        kw_rows = []
        name_rows = []
        for domain, entry in brands.items():
            if not isinstance(entry, dict):
                continue
            rows.append((
                domain,
                entry.get('canonical_name', ''),
                entry.get('first_seen', ''),
                entry.get('last_seen', ''),
                int(entry.get('appearance_count', 0) or 0),
                int(entry.get('total_ads_across_scans', 0) or 0),
                int(entry.get('highest_likes', 0) or 0),
                int(entry.get('orbit_score', 0) or 0),
                json.dumps(entry, ensure_ascii=False),
            ))
            for kw in entry.get('keywords_appeared_in', []) or []:
                if kw:
                    kw_rows.append((domain, kw))
            for nm in entry.get('all_page_names', []) or []:
                if nm:
                    name_rows.append((domain, nm))

        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO brands(domain, canonical_name, first_seen, last_seen,
                                      appearance_count, total_ads_across_scans,
                                      highest_likes, orbit_score, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(domain) DO UPDATE SET
                     canonical_name=excluded.canonical_name,
                     last_seen=excluded.last_seen,
                     appearance_count=excluded.appearance_count,
                     total_ads_across_scans=excluded.total_ads_across_scans,
                     highest_likes=max(brands.highest_likes, excluded.highest_likes),
                     orbit_score=excluded.orbit_score,
                     raw_json=excluded.raw_json""",
                rows,
            )
            if kw_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO brand_keywords(domain, keyword) VALUES (?,?)",
                    kw_rows,
                )
            if name_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO brand_page_names(domain, name) VALUES (?,?)",
                    name_rows,
                )
        log.info(f"persistence: upsert {len(rows)} brands, {len(kw_rows)} kw links, {len(name_rows)} names")
        return len(rows)

    def upsert_scan(self, keyword: str, scan_date: str, ads: int, brands: int,
                    corrected_file: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO scans(keyword, scan_date, ads_count, brands_count, corrected_file)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(keyword, scan_date) DO UPDATE SET
                     ads_count=excluded.ads_count,
                     brands_count=excluded.brands_count,
                     corrected_file=excluded.corrected_file""",
                (keyword, scan_date, ads, brands, corrected_file),
            )

    def upsert_learned(self, entries: Iterable[dict]) -> int:
        """entries: [{keyword, kw_type, subtype, source_kw, learned_date}, ...]"""
        rows = [
            (e['keyword'], e['kw_type'], e.get('subtype', ''),
             e['source_kw'], e['learned_date'])
            for e in entries
            if e.get('keyword') and e.get('kw_type') and e.get('source_kw')
        ]
        if not rows:
            return 0
        with self._conn() as conn:
            conn.executemany(
                """INSERT OR IGNORE INTO learned_keywords
                   (keyword, kw_type, subtype, source_kw, learned_date)
                   VALUES (?,?,?,?,?)""",
                rows,
            )
        return len(rows)

    # ── FTS5 触发器: 自动同步 ad_archive → ad_fts ─────────────────────────

    def _ensure_fts_triggers(self, conn) -> None:
        """Create INSERT/UPDATE/DELETE triggers to keep ad_fts in sync."""
        conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS ad_ai AFTER INSERT ON ad_archive BEGIN
                INSERT INTO ad_fts(rowid, ad_archive_id, title, body)
                VALUES (new.rowid, new.ad_archive_id, new.title, new.body);
            END;
            CREATE TRIGGER IF NOT EXISTS ad_ad AFTER DELETE ON ad_archive BEGIN
                INSERT INTO ad_fts(ad_fts, rowid, ad_archive_id, title, body)
                VALUES ('delete', old.rowid, old.ad_archive_id, old.title, old.body);
            END;
            CREATE TRIGGER IF NOT EXISTS ad_au AFTER UPDATE ON ad_archive BEGIN
                INSERT INTO ad_fts(ad_fts, rowid, ad_archive_id, title, body)
                VALUES ('delete', old.rowid, old.ad_archive_id, old.title, old.body);
                INSERT INTO ad_fts(rowid, ad_archive_id, title, body)
                VALUES (new.rowid, new.ad_archive_id, new.title, new.body);
            END;
        """)

    def upsert_ad_archive_bulk(self, ads: list, *,
                                keyword: str, scan_date: str,
                                batch_size: int = 5000) -> int:
        """v10: batch-committed upsert. 5000 rows per commit to keep
        WAL checkpoint smooth at 10M+ scale."""
        if not ads:
            return 0
        from datetime import datetime as _dt

        now_iso = _dt.now().isoformat(timespec='seconds')
        rows = []
        for ad in ads:
            try:
                aid = (ad.get('ad_archive_id') or ad.get('id') or '').strip()
                if not aid:
                    continue
                spend = ad.get('spend') if isinstance(ad.get('spend'), dict) else {}
                imp = ad.get('impressions') if isinstance(ad.get('impressions'), dict) else {}

                start_iso = ad.get('start_date_iso') or ''
                if not start_iso and ad.get('start_date'):
                    sd = ad.get('start_date')
                    try:
                        if isinstance(sd, (int, float)) and sd > 1_000_000:
                            start_iso = _dt.fromtimestamp(sd).strftime('%Y-%m-%d')
                        elif isinstance(sd, str) and sd:
                            start_iso = sd[:10]
                    except Exception:
                        start_iso = ''

                end_iso = ad.get('end_date_iso') or ''
                if not end_iso and ad.get('end_date'):
                    ed = ad.get('end_date')
                    try:
                        if isinstance(ed, (int, float)) and ed > 1_000_000:
                            end_iso = _dt.fromtimestamp(ed).strftime('%Y-%m-%d')
                        elif isinstance(ed, str) and ed:
                            end_iso = ed[:10]
                    except Exception:
                        end_iso = ''

                active_days = None
                if start_iso:
                    try:
                        sd = _dt.strptime(start_iso, '%Y-%m-%d')
                        ed = _dt.strptime(end_iso, '%Y-%m-%d') if end_iso else _dt.now()
                        active_days = max(0, (ed - sd).days)
                    except Exception:
                        active_days = None

                def _jstr(v):
                    if v in (None, '', [], {}):
                        return None
                    if isinstance(v, (list, dict)):
                        return json.dumps(v, ensure_ascii=False)[:50000]
                    return str(v)

                # regional_distribution / demographic — accept either parsed list or raw JSON string
                rd = ad.get('regional_distribution')
                if not rd and ad.get('regional_distribution_raw'):
                    rd = ad.get('regional_distribution_raw')
                dd = ad.get('demographic_distribution')
                if not dd and ad.get('demographic_distribution_raw'):
                    dd = ad.get('demographic_distribution_raw')

                rows.append((
                    aid,
                    ad.get('page_id') or None,
                    ad.get('page_name') or None,
                    ad.get('domain') or None,
                    keyword,
                    scan_date,
                    start_iso or None,
                    end_iso or None,
                    1 if ad.get('is_active') else 0,
                    active_days,
                    ad.get('display_format') or ad.get('format') or None,
                    ad.get('archive_type') or None,
                    ad.get('cta_type') or ad.get('cta') or None,
                    ad.get('cta_text') or None,
                    (ad['title'][:1000] if ad.get('title') else None),
                    (ad['body'][:5000] if ad.get('body') else None),
                    ad.get('link_url') or None,
                    ad.get('link_caption') or None,
                    ad.get('link_description') or None,
                    ad.get('ig_handle') or None,
                    int(ad.get('ig_followers') or 0),
                    ad.get('byline') or None,
                    int(ad.get('page_like_count') or ad.get('page_likes') or 0),
                    _jstr(ad.get('page_categories') or []),
                    _jstr(ad.get('publisher_platforms') or ad.get('platforms') or []),
                    spend.get('lower') if isinstance(spend, dict) else None,
                    spend.get('upper') if isinstance(spend, dict) else None,
                    imp.get('lower') if isinstance(imp, dict) else None,
                    imp.get('upper') if isinstance(imp, dict) else None,
                    ad.get('currency') or None,
                    _jstr(ad.get('target_locations') or []),
                    ad.get('target_ages') or None,
                    ad.get('target_genders') or None,
                    _jstr(ad.get('languages') or []),
                    _jstr(rd) if isinstance(rd, str) else _jstr(rd),
                    _jstr(dd) if isinstance(dd, str) else _jstr(dd),
                    int(ad.get('cards_count') or 0),
                    _jstr(ad.get('cards') or []),
                    (float(ad['video_duration_sec']) if ad.get('video_duration_sec') not in (None, '', 0) else None),
                    ad.get('video_url') or None,
                    ad.get('image_url') or None,
                    ad.get('ad_snapshot_url') or None,
                    ad.get('total_active_time') if ad.get('total_active_time') not in (None, '', 0) else None,
                    ad.get('eu_total_reach') if ad.get('eu_total_reach') not in (None, '', 0) else None,
                    1 if ad.get('eu_political_ad') else 0,
                    1 if ad.get('has_user_reported') else 0,
                    1 if ad.get('contains_sensitive_content') else 0,
                    1 if ad.get('contains_digital_created_media') else 0,
                    1 if ad.get('branded_content') else 0,
                    int(ad.get('collation_count') or 1),
                    ad.get('_source') or 'dom',
                    now_iso,
                    now_iso,
                ))
            except Exception as e:
                log.debug(f"upsert_ad_archive_bulk skip {ad.get('ad_archive_id')}: {e}")
                continue
        if not rows:
            return 0

        # v10: batch-commit in chunks of batch_size
        _UPSERT_SQL = """INSERT INTO ad_archive(
                     ad_archive_id, page_id, page_name, domain, keyword, scan_date,
                     start_date_iso, end_date_iso, is_active, active_days,
                     display_format, archive_type, cta_type, cta_text,
                     title, body, link_url, link_caption, link_description,
                     ig_handle, ig_followers, byline, page_likes,
                     page_categories, publisher_platforms,
                     spend_lower, spend_upper, imp_lower, imp_upper, currency,
                     target_locations, target_ages, target_genders, languages,
                     regional_distribution, demographic_distribution,
                     cards_count, cards, video_duration_sec, video_url, image_url,
                     ad_snapshot_url, total_active_time, eu_total_reach, eu_political_ad,
                     has_user_reported, contains_sensitive_content,
                     contains_digital_created_media, branded_content, collation_count,
                     source, inserted_at, updated_at
                   ) VALUES (
                     ?,?,?,?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?,?,?, ?,?,?,?,
                     ?,?, ?,?,?,?,?, ?,?,?,?, ?,?, ?,?,?,?,?, ?,?,?,?,
                     ?,?, ?,?,?, ?,?,?
                   )
                   ON CONFLICT(ad_archive_id) DO UPDATE SET
                     page_id=excluded.page_id,
                     page_name=excluded.page_name,
                     domain=COALESCE(excluded.domain, ad_archive.domain),
                     keyword=COALESCE(excluded.keyword, ad_archive.keyword),
                     scan_date=excluded.scan_date,
                     start_date_iso=COALESCE(excluded.start_date_iso, ad_archive.start_date_iso),
                     end_date_iso=excluded.end_date_iso,
                     is_active=excluded.is_active,
                     active_days=excluded.active_days,
                     display_format=COALESCE(excluded.display_format, ad_archive.display_format),
                     cta_type=COALESCE(excluded.cta_type, ad_archive.cta_type),
                     cta_text=COALESCE(excluded.cta_text, ad_archive.cta_text),
                     title=COALESCE(excluded.title, ad_archive.title),
                     body=COALESCE(excluded.body, ad_archive.body),
                     link_url=COALESCE(excluded.link_url, ad_archive.link_url),
                     link_description=COALESCE(excluded.link_description, ad_archive.link_description),
                     ig_handle=COALESCE(excluded.ig_handle, ad_archive.ig_handle),
                     ig_followers=MAX(IFNULL(excluded.ig_followers,0), IFNULL(ad_archive.ig_followers,0)),
                     byline=COALESCE(excluded.byline, ad_archive.byline),
                     page_likes=MAX(IFNULL(excluded.page_likes,0), IFNULL(ad_archive.page_likes,0)),
                     page_categories=COALESCE(excluded.page_categories, ad_archive.page_categories),
                     publisher_platforms=COALESCE(excluded.publisher_platforms, ad_archive.publisher_platforms),
                     spend_lower=COALESCE(excluded.spend_lower, ad_archive.spend_lower),
                     spend_upper=COALESCE(excluded.spend_upper, ad_archive.spend_upper),
                     imp_lower=COALESCE(excluded.imp_lower, ad_archive.imp_lower),
                     imp_upper=COALESCE(excluded.imp_upper, ad_archive.imp_upper),
                     currency=COALESCE(excluded.currency, ad_archive.currency),
                     target_locations=COALESCE(excluded.target_locations, ad_archive.target_locations),
                     target_ages=COALESCE(excluded.target_ages, ad_archive.target_ages),
                     target_genders=COALESCE(excluded.target_genders, ad_archive.target_genders),
                     languages=COALESCE(excluded.languages, ad_archive.languages),
                     regional_distribution=COALESCE(excluded.regional_distribution, ad_archive.regional_distribution),
                     demographic_distribution=COALESCE(excluded.demographic_distribution, ad_archive.demographic_distribution),
                     cards_count=excluded.cards_count,
                     cards=COALESCE(excluded.cards, ad_archive.cards),
                     video_duration_sec=COALESCE(excluded.video_duration_sec, ad_archive.video_duration_sec),
                     video_url=COALESCE(excluded.video_url, ad_archive.video_url),
                     image_url=COALESCE(excluded.image_url, ad_archive.image_url),
                     ad_snapshot_url=COALESCE(excluded.ad_snapshot_url, ad_archive.ad_snapshot_url),
                     total_active_time=COALESCE(excluded.total_active_time, ad_archive.total_active_time),
                     eu_total_reach=COALESCE(excluded.eu_total_reach, ad_archive.eu_total_reach),
                     eu_political_ad=excluded.eu_political_ad,
                     has_user_reported=excluded.has_user_reported,
                     contains_sensitive_content=excluded.contains_sensitive_content,
                     contains_digital_created_media=excluded.contains_digital_created_media,
                     branded_content=excluded.branded_content,
                     collation_count=excluded.collation_count,
                     source=excluded.source,
                     updated_at=excluded.updated_at"""

        # Write connection: no shards attached, direct to main DB
        conn = sqlite3.connect(str(self.db_file))
        self._apply_pragmas(conn)
        try:
            self._ensure_fts_triggers(conn)
            for i in range(0, len(rows), batch_size):
                chunk = rows[i:i + batch_size]
                self._retry_executemany(conn, _UPSERT_SQL, chunk)
                conn.commit()
        finally:
            conn.close()
        return len(rows)

    def _retry_executemany(self, conn, sql: str, rows: list, *,
                            max_retries: int = 3, base_delay: float = 1.0) -> None:
        """Execute executemany with retry on transient 'malformed' / 'database is locked' errors."""
        import time as _time
        for attempt in range(max_retries + 1):
            try:
                conn.executemany(sql, rows)
                return
            except sqlite3.DatabaseError as e:
                msg = str(e).lower()
                is_transient = ('malformed' in msg or 'database is locked' in msg
                                or 'disk i/o error' in msg)
                if not is_transient or attempt >= max_retries:
                    raise
                delay = base_delay * (2 ** attempt)
                log.warning(f"persistence: transient DB error (attempt {attempt+1}/{max_retries}), "
                            f"retrying in {delay:.1f}s: {e}")
                _time.sleep(delay)
                # Force WAL checkpoint on retry — clears stale WAL state
                if 'malformed' in msg:
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    except Exception:
                        pass


    # ── 查询 ────────────────────────────────────────────────────────────────
    # 注意: 读查询走 v_* UNION ALL 视图 (覆盖 main + 所有历史分片),
    # 同一主键可能在多个月份分片出现, 故所有 list/count 需 GROUP BY + 聚合.

    def get_brand(self, domain: str) -> Optional[dict]:
        with self._conn() as conn:
            # 跨分片取最新一行 (last_seen 最大)
            row = conn.execute(
                """SELECT * FROM v_brands
                   WHERE domain = ?
                   ORDER BY last_seen DESC LIMIT 1""",
                (domain,),
            ).fetchone()
            if not row:
                return None
            brand = dict(row)
            brand['keywords'] = [
                r['keyword']
                for r in conn.execute(
                    "SELECT DISTINCT keyword FROM v_brand_keywords WHERE domain = ?",
                    (domain,),
                )
            ]
            brand['page_names'] = [
                r['name']
                for r in conn.execute(
                    "SELECT DISTINCT name FROM v_brand_page_names WHERE domain = ?",
                    (domain,),
                )
            ]
            return brand

    def list_orbit_brands(self, min_appearance: int = 3, limit: int = 50,
                          days: int = 0, offset: int = 0) -> list[dict]:
        sql = """SELECT domain,
                        MAX(canonical_name) AS canonical_name,
                        MAX(appearance_count) AS appearance_count,
                        MAX(orbit_score) AS orbit_score,
                        MAX(highest_likes) AS highest_likes,
                        MAX(total_ads_across_scans) AS total_ads_across_scans,
                        MAX(last_seen) AS last_seen
                 FROM v_brands
                 GROUP BY domain
                 HAVING MAX(appearance_count) >= ?"""
        params: list = [min_appearance]
        if days > 0:
            sql += " AND MAX(last_seen) >= date('now', '-' || ? || ' days')"
            params.append(days)
        sql += " ORDER BY orbit_score DESC, appearance_count DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        months = months_for_days(days) if days > 0 else None
        with self._conn(months=months) as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def count_brands(self, min_appearance: int = 1, days: int = 0) -> int:
        sql = """SELECT COUNT(*) FROM (
                   SELECT 1 FROM v_brands
                   GROUP BY domain
                   HAVING MAX(appearance_count) >= ?"""
        params: list = [min_appearance]
        if days > 0:
            sql += " AND MAX(last_seen) >= date('now', '-' || ? || ' days')"
            params.append(days)
        sql += ")"
        months = months_for_days(days) if days > 0 else None
        with self._conn(months=months) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def list_brands_by_keyword(self, keyword: str) -> list[dict]:
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT b.domain,
                          MAX(b.canonical_name) AS canonical_name,
                          MAX(b.orbit_score) AS orbit_score,
                          MAX(b.appearance_count) AS appearance_count
                   FROM v_brands b
                   JOIN v_brand_keywords bk ON bk.domain = b.domain
                   WHERE bk.keyword = ?
                   GROUP BY b.domain
                   ORDER BY orbit_score DESC""",
                (keyword,),
            )]

    def search_brands(self, query: str, limit: int = 20) -> list[dict]:
        q = f"%{query.lower()}%"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(
                """SELECT domain,
                          MAX(canonical_name) AS canonical_name,
                          MAX(orbit_score) AS orbit_score,
                          MAX(appearance_count) AS appearance_count
                   FROM v_brands
                   WHERE LOWER(domain) LIKE ? OR LOWER(canonical_name) LIKE ?
                   GROUP BY domain
                   ORDER BY orbit_score DESC LIMIT ?""",
                (q, q, limit),
            )]

    def search_learned(self, kw_type: Optional[str] = None,
                       query: Optional[str] = None, limit: int = 100,
                       days: int = 0, offset: int = 0) -> list[dict]:
        sql = ("SELECT keyword, kw_type, MAX(subtype) AS subtype, source_kw, "
               "       MAX(learned_date) AS learned_date "
               "FROM v_learned_keywords WHERE 1=1")
        params: list = []
        if kw_type:
            sql += " AND kw_type = ?"
            params.append(kw_type)
        if query:
            sql += " AND LOWER(keyword) LIKE ?"
            params.append(f"%{query.lower()}%")
        if days > 0:
            sql += " AND learned_date >= date('now', '-' || ? || ' days')"
            params.append(days)
        sql += (" GROUP BY keyword, kw_type, source_kw "
                "ORDER BY learned_date DESC LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        months = months_for_days(days) if days > 0 else None
        with self._conn(months=months) as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def count_learned(self, kw_type: Optional[str] = None, query: Optional[str] = None, days: int = 0) -> int:
        sql = ("SELECT COUNT(*) FROM ("
               " SELECT 1 FROM v_learned_keywords WHERE 1=1")
        params: list = []
        if kw_type:
            sql += " AND kw_type = ?"
            params.append(kw_type)
        if query:
            sql += " AND LOWER(keyword) LIKE ?"
            params.append(f"%{query.lower()}%")
        if days > 0:
            sql += " AND learned_date >= date('now', '-' || ? || ' days')"
            params.append(days)
        sql += " GROUP BY keyword, kw_type, source_kw)"
        months = months_for_days(days) if days > 0 else None
        with self._conn(months=months) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def recent_scans(self, limit: int = 20, days: int = 0, offset: int = 0) -> list[dict]:
        # scans 主键 (keyword, scan_date), 同主键可能跨分片 → GROUP BY dedup
        sql = ("SELECT keyword, scan_date, MAX(ads_count) AS ads_count, "
               "       MAX(brands_count) AS brands_count, "
               "       MAX(corrected_file) AS corrected_file "
               "FROM v_scans")
        params: list = []
        if days > 0:
            sql += " WHERE scan_date >= date('now', '-' || ? || ' days')"
            params.append(days)
        sql += (" GROUP BY keyword, scan_date "
                "ORDER BY scan_date DESC LIMIT ? OFFSET ?")
        params.extend([limit, offset])
        months = months_for_days(days) if days > 0 else None
        with self._conn(months=months) as conn:
            return [dict(r) for r in conn.execute(sql, params)]

    def count_scans(self, days: int = 0) -> int:
        sql = ("SELECT COUNT(*) FROM ("
               " SELECT 1 FROM v_scans")
        params: list = []
        if days > 0:
            sql += " WHERE scan_date >= date('now', '-' || ? || ' days')"
            params.append(days)
        sql += " GROUP BY keyword, scan_date)"
        months = months_for_days(days) if days > 0 else None
        with self._conn(months=months) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def vacuum(self) -> None:
        """回收空间, 建议每几百次写入后调用一次."""
        conn = sqlite3.connect(str(self.db_file))
        self._apply_pragmas(conn)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()

    # ── v10: 10M-scale query methods ────────────────────────────────────────

    def search_ads_fts(self, query: str, limit: int = 50, offset: int = 0,
                       days: int = 0) -> list[dict]:
        """FTS5 full-text search across ad_archive title/body."""
        with self._conn() as conn:
            sql = """SELECT a.* FROM ad_archive a
                     JOIN ad_fts f ON f.ad_archive_id = a.ad_archive_id
                     WHERE ad_fts MATCH ?"""
            params: list = [query]
            if days > 0:
                sql += " AND a.scan_date >= date('now', '-' || ? || ' days')"
                params.append(days)
            sql += " ORDER BY a.scan_date DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            return [dict(r) for r in conn.execute(sql, params)]

    def count_ads(self, keyword: Optional[str] = None, domain: Optional[str] = None,
                  days: int = 0, is_active: Optional[int] = None) -> int:
        """Count ads with optional filters — uses index, no full scan."""
        months = months_for_days(days) if days > 0 else None
        with self._conn(months=months) as conn:
            sql = "SELECT COUNT(*) FROM v_ad_archive WHERE 1=1"
            params: list = []
            if keyword:
                sql += " AND keyword = ?"
                params.append(keyword)
            if domain:
                sql += " AND domain = ?"
                params.append(domain)
            if days > 0:
                sql += " AND scan_date >= date('now', '-' || ? || ' days')"
                params.append(days)
            if is_active is not None:
                sql += " AND is_active = ?"
                params.append(is_active)
            return conn.execute(sql, params).fetchone()[0]

    def list_ads(self, keyword: Optional[str] = None, domain: Optional[str] = None,
                 days: int = 0, is_active: Optional[int] = None,
                 limit: int = 200, offset: int = 0,
                 order_by: str = 'scan_date DESC') -> list[dict]:
        """Paginated ad listing with index-driven filters."""
        months = months_for_days(days) if days > 0 else None
        with self._conn(months=months) as conn:
            sql = "SELECT * FROM v_ad_archive WHERE 1=1"
            params: list = []
            if keyword:
                sql += " AND keyword = ?"
                params.append(keyword)
            if domain:
                sql += " AND domain = ?"
                params.append(domain)
            if days > 0:
                sql += " AND scan_date >= date('now', '-' || ? || ' days')"
                params.append(days)
            if is_active is not None:
                sql += " AND is_active = ?"
                params.append(is_active)
            sql += f" ORDER BY {order_by} LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            return [dict(r) for r in conn.execute(sql, params)]

    def rebuild_fts(self) -> int:
        """Rebuild FTS5 index from scratch (run after bulk imports)."""
        conn = sqlite3.connect(str(self.db_file))
        self._apply_pragmas(conn)
        try:
            conn.execute("INSERT INTO ad_fts(ad_fts) VALUES ('rebuild')")
            conn.commit()
            n = conn.execute("SELECT COUNT(*) FROM ad_fts").fetchone()[0]
            log.info(f"FTS5 rebuild: {n} docs indexed")
            return n
        finally:
            conn.close()

    # ── recommendations: all CRUD is in recommendations.py, which owns the schema ──


_persistence: Optional[Persistence] = None


def get_persistence() -> Persistence:
    """Lazy singleton, safe to call in main process."""
    global _persistence
    if _persistence is None:
        _persistence = Persistence()
    return _persistence

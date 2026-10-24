"""pipeline/insights_v3.py — Aggregations powering the V2 dashboard's new tabs.

Reads the `ad_archive` table populated by raw_processor → scan_result →
apply_scan_side_effects. Each function returns a JSON-serializable dict.

Tabs covered:
- creative_mix:    video / image / carousel ratio · CTA distribution · format mix
                   · video duration histogram · headline / body length percentiles
- geo_distribution: top countries / top US states · regional concentration
- demographics:    age x gender heatmap · top targeted ages / genders
- platforms:       FB / IG / Messenger / Threads / Audience Network mix
- creative_fatigue: active-days histogram · long-running winners · turnover rate
- copy_patterns:   top headlines · top hooks · urgency words · price-mention rate
- summary:         master overview combining headline numbers from each tab
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from typing import Optional

from core.logger import get_logger

log = get_logger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────

def _conn():
    """Return Persistence._conn() context manager. Caller MUST use `with`.

    BUGFIX 2026-05-05: previously did `db._conn().__enter__()` which leaked the
    contextmanager — under Py3.14 the cm is GC-finalized immediately after
    __enter__() returns, triggering the `finally: conn.close()` in
    Persistence._conn before the caller can use the conn. Result was every
    /admin/insights/v3/* endpoint raising
    `sqlite3.ProgrammingError: Cannot operate on a closed database.`
    Returning the cm directly lets `with _conn() as conn:` keep it alive for
    the whole block.
    """
    from .persistence import get_persistence
    return get_persistence()._conn()


def _has_ad_archive(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_archive'"
    ).fetchone()
    return bool(row)


def _filter_clause(days: Optional[int], domain: Optional[str], keyword: Optional[str]):
    parts = []
    params: list = []
    if days and days > 0:
        parts.append("scan_date >= date('now', '-' || ? || ' days')")
        params.append(days)
    if domain:
        parts.append("LOWER(domain) = LOWER(?)")
        params.append(domain)
    if keyword:
        parts.append("LOWER(keyword) = LOWER(?)")
        params.append(keyword)
    where = (" WHERE " + " AND ".join(parts)) if parts else ""
    return where, params


# ── Aggregations ───────────────────────────────────────────────────────────

def creative_mix(days: int = 14, domain: Optional[str] = None,
                 keyword: Optional[str] = None) -> dict:
    """媒体格式 / CTA / 文案长度 分布."""
    with _conn() as conn:
        if not _has_ad_archive(conn):
            return {'error': 'ad_archive table not yet populated'}
        where, params = _filter_clause(days, domain, keyword)

        # format mix
        fmt_rows = conn.execute(
            f"SELECT display_format, COUNT(*) AS n FROM ad_archive{where}"
            " GROUP BY display_format ORDER BY n DESC", params
        ).fetchall()
        format_mix = [{'format': (r['display_format'] or 'unknown'), 'count': r['n']}
                      for r in fmt_rows]

        # video / image / carousel — derived from cards_count + video_duration_sec
        media_rows = conn.execute(
            f"""SELECT
                  SUM(CASE WHEN cards_count > 1 THEN 1 ELSE 0 END) AS carousel_n,
                  SUM(CASE WHEN cards_count <= 1 AND video_duration_sec IS NOT NULL THEN 1 ELSE 0 END) AS video_n,
                  SUM(CASE WHEN cards_count <= 1 AND (video_duration_sec IS NULL OR video_duration_sec = 0)
                            AND image_url IS NOT NULL AND image_url != '' THEN 1 ELSE 0 END) AS image_n,
                  COUNT(*) AS total
                FROM ad_archive{where}""",
            params
        ).fetchone()
        media_mix = {
            'carousel': media_rows['carousel_n'] or 0,
            'video':    media_rows['video_n'] or 0,
            'image':    media_rows['image_n'] or 0,
            'total':    media_rows['total'] or 0,
        }

        # CTA distribution
        cta_sql = "SELECT cta_type, COUNT(*) AS n FROM ad_archive"
        cta_where = where + (" AND " if where else " WHERE ") + "cta_type != ''"
        cta_sql += cta_where + " GROUP BY cta_type ORDER BY n DESC LIMIT 20"
        cta_rows = conn.execute(cta_sql, params).fetchall()
        cta_dist = [{'cta': r['cta_type'], 'count': r['n']} for r in cta_rows]

        # video duration histogram
        dur_rows = conn.execute(
            f"SELECT video_duration_sec FROM ad_archive{where}"
            + (" AND " if where else " WHERE ") + "video_duration_sec IS NOT NULL AND video_duration_sec > 0",
            params
        ).fetchall()
        durations = [r['video_duration_sec'] for r in dur_rows]
        buckets = [(0, 5), (5, 10), (10, 15), (15, 30), (30, 45), (45, 60),
                   (60, 90), (90, 120), (120, 180), (180, 99999)]
        dur_hist = []
        for lo, hi in buckets:
            n = sum(1 for d in durations if lo <= d < hi)
            label = f">{lo//60}m" if lo >= 60 else f"{lo}-{hi}s"
            dur_hist.append({'range': label, 'lo': lo, 'hi': hi, 'count': n})

        # body / title length percentiles
        len_rows = conn.execute(
            f"SELECT length(title) AS lt, length(body) AS lb FROM ad_archive{where}",
            params
        ).fetchall()
        title_lens = sorted([r['lt'] or 0 for r in len_rows])
        body_lens = sorted([r['lb'] or 0 for r in len_rows])

        def _pct(arr, p):
            if not arr:
                return 0
            i = max(0, min(len(arr) - 1, int(len(arr) * p / 100)))
            return arr[i]

        text_len_stats = {
            'title': {'p25': _pct(title_lens, 25), 'p50': _pct(title_lens, 50),
                      'p75': _pct(title_lens, 75), 'p95': _pct(title_lens, 95)},
            'body':  {'p25': _pct(body_lens, 25), 'p50': _pct(body_lens, 50),
                      'p75': _pct(body_lens, 75), 'p95': _pct(body_lens, 95)},
        }

    return {
        'window_days': days,
        'media_mix': media_mix,
        'format_mix': format_mix,
        'cta_distribution': cta_dist,
        'video_duration_histogram': dur_hist,
        'text_length_percentiles': text_len_stats,
    }


def geo_distribution(days: int = 14, domain: Optional[str] = None,
                     keyword: Optional[str] = None) -> dict:
    """从 regional_distribution / target_locations 聚合地理分布."""
    countries = Counter()
    states = Counter()
    eu_reach_total = 0
    eu_ad_count = 0
    with _conn() as conn:
        if not _has_ad_archive(conn):
            return {'error': 'ad_archive table not yet populated'}
        where, params = _filter_clause(days, domain, keyword)
        rows = conn.execute(
            f"SELECT regional_distribution, target_locations, eu_total_reach, eu_political_ad"
            f" FROM ad_archive{where}",
            params
        ).fetchall()
        for r in rows:
            try:
                reg = json.loads(r['regional_distribution'] or '[]')
                if isinstance(reg, list):
                    for entry in reg:
                        if not isinstance(entry, dict):
                            continue
                        region = entry.get('region') or entry.get('country') or ''
                        pct = entry.get('percentage') or entry.get('pct') or 0
                        try:
                            pct = float(pct)
                        except (TypeError, ValueError):
                            pct = 0.0
                        if region:
                            # Heuristic: 2-letter US state code => state; longer => country
                            if isinstance(region, str) and len(region) == 2 and region.isupper():
                                states[region] += pct
                            else:
                                countries[region] += pct
            except Exception:
                pass
            try:
                tloc = json.loads(r['target_locations'] or '[]')
                if isinstance(tloc, list):
                    for v in tloc:
                        if isinstance(v, str) and v:
                            countries[v] += 1
            except Exception:
                pass
            if r['eu_total_reach']:
                eu_reach_total += int(r['eu_total_reach'] or 0)
            if r['eu_political_ad']:
                eu_ad_count += 1

    return {
        'window_days': days,
        'top_countries': [{'country': c, 'weight': round(v, 1)}
                          for c, v in countries.most_common(25)],
        'top_us_states': [{'state': s, 'weight': round(v, 1)}
                          for s, v in states.most_common(20)],
        'eu_total_reach_sum': eu_reach_total,
        'eu_political_ad_count': eu_ad_count,
    }


def demographics(days: int = 14, domain: Optional[str] = None,
                 keyword: Optional[str] = None) -> dict:
    """从 demographic_distribution 聚合 age × gender heatmap."""
    age_buckets = ['13-17', '18-24', '25-34', '35-44', '45-54', '55-64', '65+']
    grid = {age: {'male': 0.0, 'female': 0.0, 'unknown': 0.0} for age in age_buckets}
    target_age_counter = Counter()
    target_gender_counter = Counter()

    with _conn() as conn:
        if not _has_ad_archive(conn):
            return {'error': 'ad_archive table not yet populated'}
        where, params = _filter_clause(days, domain, keyword)
        rows = conn.execute(
            f"SELECT demographic_distribution, target_ages, target_genders"
            f" FROM ad_archive{where}",
            params
        ).fetchall()
        for r in rows:
            try:
                dem = json.loads(r['demographic_distribution'] or '[]')
                if isinstance(dem, list):
                    for entry in dem:
                        if not isinstance(entry, dict):
                            continue
                        age = entry.get('age') or entry.get('age_range') or ''
                        gender = (entry.get('gender') or '').lower()
                        pct = entry.get('percentage') or entry.get('pct') or 0
                        try:
                            pct = float(pct)
                        except (TypeError, ValueError):
                            pct = 0.0
                        if age in grid:
                            if gender in ('male', 'female'):
                                grid[age][gender] += pct
                            else:
                                grid[age]['unknown'] += pct
            except Exception:
                pass
            ta = (r['target_ages'] or '').strip()
            if ta:
                target_age_counter[ta] += 1
            tg = (r['target_genders'] or '').strip()
            if tg:
                target_gender_counter[tg] += 1

    heatmap = []
    for age in age_buckets:
        cell = grid[age]
        heatmap.append({
            'age': age,
            'male': round(cell['male'], 1),
            'female': round(cell['female'], 1),
            'unknown': round(cell['unknown'], 1),
        })
    return {
        'window_days': days,
        'age_gender_heatmap': heatmap,
        'top_target_ages': [{'value': k, 'count': v} for k, v in target_age_counter.most_common(15)],
        'top_target_genders': [{'value': k, 'count': v} for k, v in target_gender_counter.most_common(5)],
    }


def platform_mix(days: int = 14, domain: Optional[str] = None,
                 keyword: Optional[str] = None) -> dict:
    """publisher_platforms 跨广告聚合."""
    counter = Counter()
    combo_counter = Counter()
    total = 0
    with _conn() as conn:
        if not _has_ad_archive(conn):
            return {'error': 'ad_archive table not yet populated'}
        where, params = _filter_clause(days, domain, keyword)
        rows = conn.execute(
            f"SELECT publisher_platforms FROM ad_archive{where}",
            params
        ).fetchall()
        for r in rows:
            total += 1
            try:
                plats = json.loads(r['publisher_platforms'] or '[]')
            except Exception:
                plats = []
            if not isinstance(plats, list):
                plats = []
            plats = sorted({p.upper().replace('AUDIENCE_NETWORK', 'AN') for p in plats if p})
            for p in plats:
                counter[p] += 1
            if plats:
                combo_counter['+'.join(plats)] += 1

    return {
        'window_days': days,
        'total_ads': total,
        'platforms': [{'platform': p, 'count': n,
                       'pct': round(n / total * 100, 1) if total else 0}
                      for p, n in counter.most_common(10)],
        'top_combinations': [{'combo': k, 'count': v}
                             for k, v in combo_counter.most_common(10)],
    }


def creative_fatigue(days: int = 30, domain: Optional[str] = None,
                     keyword: Optional[str] = None) -> dict:
    """active_days 分布 + 长寿赢家 + brand 创意周转."""
    with _conn() as conn:
        if not _has_ad_archive(conn):
            return {'error': 'ad_archive table not yet populated'}
        where, params = _filter_clause(days, domain, keyword)
        rows = conn.execute(
            f"SELECT ad_archive_id, page_name, domain, active_days, is_active,"
            f" start_date_iso, total_active_time"
            f" FROM ad_archive{where}",
            params
        ).fetchall()

    days_list = [r['active_days'] for r in rows if r['active_days'] is not None]
    days_list = sorted(days_list)

    buckets = [(0, 7), (7, 14), (14, 30), (30, 60), (60, 90),
               (90, 180), (180, 365), (365, 99999)]
    hist = []
    for lo, hi in buckets:
        n = sum(1 for d in days_list if lo <= d < hi)
        label = (f"{lo}-{hi}d" if hi < 99999 else f">{lo}d")
        hist.append({'range': label, 'lo': lo, 'hi': hi, 'count': n})

    # long-running winners (still active and >= 60d)
    winners = [
        {'ad_archive_id': r['ad_archive_id'], 'page_name': r['page_name'],
         'domain': r['domain'], 'active_days': r['active_days'],
         'start_date_iso': r['start_date_iso']}
        for r in rows
        if (r['is_active'] or 0) and (r['active_days'] or 0) >= 60
    ]
    winners.sort(key=lambda x: -(x['active_days'] or 0))
    winners = winners[:30]

    # brand turnover: ads per brand in window vs avg ad lifespan
    brand_stats: defaultdict = defaultdict(lambda: {'ads': 0, 'avg_days': 0, '_sum_days': 0})
    for r in rows:
        if not r['domain']:
            continue
        bs = brand_stats[r['domain']]
        bs['ads'] += 1
        bs['_sum_days'] += int(r['active_days'] or 0)
    brand_turnover = []
    for d, bs in brand_stats.items():
        if bs['ads'] >= 3:
            avg = bs['_sum_days'] / bs['ads']
            brand_turnover.append({
                'domain': d,
                'ads_in_window': bs['ads'],
                'avg_active_days': round(avg, 1),
                'turnover_score': round(bs['ads'] / max(avg, 1), 2),
            })
    brand_turnover.sort(key=lambda x: -x['turnover_score'])

    return {
        'window_days': days,
        'active_days_histogram': hist,
        'long_running_winners': winners,
        'brand_turnover': brand_turnover[:30],
    }


URGENCY_RX = re.compile(
    r'\b(today|hurry|limited|sale|free|now|don\'?t miss|last chance|'
    r'while supplies last|ends today|act now|24 hours|flash|exclusive)\b',
    re.IGNORECASE
)
PRICE_RX = re.compile(r'\$\s?\d+', re.IGNORECASE)
DISCOUNT_RX = re.compile(r'\b(\d{1,2})\s?%\s?off\b', re.IGNORECASE)


def copy_patterns(days: int = 14, domain: Optional[str] = None,
                  keyword: Optional[str] = None) -> dict:
    """文案分析: 顶级 headline · urgency 词频 · 价格出现率 · discount %."""
    with _conn() as conn:
        if not _has_ad_archive(conn):
            return {'error': 'ad_archive table not yet populated'}
        where, params = _filter_clause(days, domain, keyword)
        rows = conn.execute(
            f"SELECT title, body, cta_text FROM ad_archive{where}",
            params
        ).fetchall()

    title_counter = Counter()
    cta_text_counter = Counter()
    urgency_hits = 0
    price_hits = 0
    discount_hits = 0
    discount_values = Counter()
    total = 0

    for r in rows:
        total += 1
        t = (r['title'] or '').strip()
        if t:
            title_counter[t.lower()[:120]] += 1
        ct = (r['cta_text'] or '').strip()
        if ct:
            cta_text_counter[ct.lower()[:60]] += 1
        body = (r['body'] or '').strip()
        full = (t + ' ' + body).lower()
        if URGENCY_RX.search(full):
            urgency_hits += 1
        if PRICE_RX.search(full):
            price_hits += 1
        m = DISCOUNT_RX.search(full)
        if m:
            discount_hits += 1
            try:
                discount_values[int(m.group(1))] += 1
            except Exception:
                pass

    pct = (lambda n: round(n / total * 100, 1) if total else 0)
    return {
        'window_days': days,
        'total_ads': total,
        'top_titles': [{'title': t, 'count': n}
                       for t, n in title_counter.most_common(20)],
        'top_cta_texts': [{'cta_text': t, 'count': n}
                          for t, n in cta_text_counter.most_common(15)],
        'urgency_rate_pct': pct(urgency_hits),
        'price_mention_rate_pct': pct(price_hits),
        'discount_offer_rate_pct': pct(discount_hits),
        'top_discount_pcts': [{'pct': k, 'count': v}
                              for k, v in discount_values.most_common(10)],
    }


def summary(days: int = 14) -> dict:
    """一站式 summary — V2 dashboard top-line."""
    with _conn() as conn:
        if not _has_ad_archive(conn):
            return {'error': 'ad_archive table not yet populated', 'rows': 0}
        where, params = _filter_clause(days, None, None)
        head = conn.execute(
            f"SELECT COUNT(*) AS total,"
            f" COUNT(DISTINCT page_id) AS brands,"
            f" COUNT(DISTINCT keyword) AS keywords,"
            f" SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active,"
            f" SUM(CASE WHEN cards_count > 1 THEN 1 ELSE 0 END) AS carousel,"
            f" SUM(CASE WHEN video_duration_sec > 0 THEN 1 ELSE 0 END) AS video"
            f" FROM ad_archive{where}",
            params
        ).fetchone()
        coverage = conn.execute(
            f"SELECT COUNT(*) AS rich FROM ad_archive{where}"
            + (" AND " if where else " WHERE ")
            + "(regional_distribution IS NOT NULL AND regional_distribution != '[]'"
              " AND regional_distribution != '')",
            params
        ).fetchone()
        rich_rows = (coverage['rich'] if coverage else 0) or 0
        total = head['total'] or 0

    return {
        'window_days': days,
        'total_ads':       total,
        'unique_brands':   head['brands'] or 0,
        'unique_keywords': head['keywords'] or 0,
        'active_ads':      head['active'] or 0,
        'video_ads':       head['video'] or 0,
        'carousel_ads':    head['carousel'] or 0,
        'graphql_coverage_pct':
            round((rich_rows / total * 100), 1) if total else 0.0,
    }


def latest_ads(limit: int = 50, hours: int = 0,
               keyword: Optional[str] = None,
               domain: Optional[str] = None,
               only_with_graphql: bool = False) -> dict:
    """最新数据 — 按 inserted_at DESC 取最新 N 条 ad-level 记录.

    hours=0 表示不限制时间窗口. only_with_graphql 只取来自 graphql 嗅探的 (字段最齐).
    每条返回足以在 dashboard 上一览的核心字段 + 跳到 FB Ad Library 的链接.
    """
    out: list = []
    with _conn() as conn:
        if not _has_ad_archive(conn):
            return {'error': 'ad_archive table not yet populated', 'items': []}
        sql = ("SELECT ad_archive_id, page_id, page_name, domain, keyword, scan_date,"
               " start_date_iso, end_date_iso, is_active, active_days, display_format,"
               " cta_type, cta_text, title, body, link_url, link_caption,"
               " ig_handle, ig_followers, byline, page_likes,"
               " spend_lower, spend_upper, imp_lower, imp_upper, currency,"
               " regional_distribution, demographic_distribution, cards_count,"
               " video_duration_sec, video_url, image_url, ad_snapshot_url,"
               " eu_total_reach, eu_political_ad,"
               " has_user_reported, contains_sensitive_content,"
               " contains_digital_created_media, branded_content,"
               " collation_count, source, inserted_at, updated_at"
               " FROM ad_archive WHERE 1=1")
        params: list = []
        if hours and hours > 0:
            sql += " AND datetime(inserted_at) >= datetime('now', '-' || ? || ' hours')"
            params.append(hours)
        if keyword:
            sql += " AND LOWER(keyword) = LOWER(?)"
            params.append(keyword)
        if domain:
            sql += " AND LOWER(domain) = LOWER(?)"
            params.append(domain)
        if only_with_graphql:
            sql += " AND source LIKE '%graphql%'"
        sql += " ORDER BY datetime(inserted_at) DESC LIMIT ?"
        params.append(int(limit))
        for r in conn.execute(sql, params):
            d = dict(r)
            # parse JSON-ish columns
            for k in ('regional_distribution', 'demographic_distribution'):
                v = d.get(k)
                if isinstance(v, str) and v.strip().startswith(('[', '{')):
                    try:
                        d[k] = json.loads(v)
                    except Exception:
                        pass
            d['ad_library_url'] = (
                f"https://www.facebook.com/ads/library/?id={d['ad_archive_id']}"
                if d.get('ad_archive_id') else ''
            )
            d['has_video'] = bool(d.get('video_url'))
            d['has_image'] = bool(d.get('image_url'))
            d['is_carousel'] = (d.get('cards_count') or 0) > 1
            out.append(d)

        # also report total in window for context
        total_sql = "SELECT COUNT(*) AS n FROM ad_archive WHERE 1=1"
        total_params: list = []
        if hours and hours > 0:
            total_sql += " AND datetime(inserted_at) >= datetime('now', '-' || ? || ' hours')"
            total_params.append(hours)
        total = conn.execute(total_sql, total_params).fetchone()['n']

    return {
        'count': len(out),
        'total_in_window': total,
        'hours': hours,
        'items': out,
    }

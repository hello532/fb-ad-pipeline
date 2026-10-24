"""pipeline/recommendations.py — 产品推荐引擎.

扫描所有 _corrected.json + brand_intelligence, 按规则评分, 挑出
"值得在 Facebook+Shopify 上架投广告" 的品牌/产品.

评分维度 (0-100):
- Shopify 形态落地页 (有 /products/ 或 /collections/): +25
- 广告龄 >= 90 天 (爆品验证过): +20  / 30-90: +10
- ad_count >= 5 (在持续投): +15
- is_active = true (还在跑): +10
- orbit_score >= 50 (跨关键词复现): +15
- 文本分析完整 (pain/scene/product 都有): +10
- spend_estimate 存在: +5

只保留 score >= 60 的.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from core.logger import get_logger

from .paths import RESULTS_DIR
from .persistence import get_persistence
from .signals import combined_signal_score
from .categories import get_high_ticket_threshold
from .fb_fitness import fb_fitness_score, fb_fitness_label
from .defaults import DASH, UNKNOWN, NONE_LABEL, ZERO_MONEY, safe_str, safe_int, safe_float, scrub_output

log = get_logger(__name__)

SHOPIFY_PATH_HINT = re.compile(r'/(products?|collections?)/', re.I)
SHOPIFY_DOMAIN_HINT = re.compile(r'\.myshopify\.com', re.I)


RECOMMENDATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS recommendations (
  domain TEXT PRIMARY KEY,
  brand_name TEXT,
  landing_url TEXT,
  product_slug TEXT,
  keyword TEXT,
  score INTEGER,
  ads_count INTEGER,
  ad_age_days INTEGER,
  is_active INTEGER,
  orbit_score INTEGER,
  is_shopify_shape INTEGER,
  spend_estimate TEXT,
  top_pain TEXT,
  top_scene TEXT,
  top_product TEXT,
  reasons TEXT,
  fb_fitness INTEGER,
  fb_breakdown TEXT,
  fb_tags TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_rec_score ON recommendations(score DESC);
"""


def _shopify_info(url: str) -> tuple[bool, str]:
    """返回 (is_shopify_shape, product_slug)."""
    if not url:
        return False, ''
    shopify = bool(SHOPIFY_DOMAIN_HINT.search(url))
    slug = ''
    try:
        p = urlparse(url)
        m = SHOPIFY_PATH_HINT.search(p.path or '')
        if m:
            shopify = True
            # 取路径最后一段作为 slug
            parts = [x for x in p.path.split('/') if x]
            if len(parts) >= 2:
                slug = parts[-1].replace('-', ' ')
    except Exception:
        pass
    return shopify, slug


def _text_completeness(ta: dict) -> bool:
    """text_analysis 字段有内容"""
    if not isinstance(ta, dict):
        return False
    hits = sum(1 for k in ('top_pain_keywords', 'top_scene_keywords', 'top_product_keywords',
                           'top_value_keywords', 'hook_patterns')
               if ta.get(k))
    return hits >= 3


def score_competitor(comp: dict, keyword: str) -> Optional[dict]:
    """返回一条 recommendation 记录, 不够格返回 None.

    评分模型 v3 — 线性/按比例, 不是 step function:
      Shopify 形态:  +20 (确认有 /products or /collections)
      广告龄:        min(age/6, 25) — 150 天封顶 (150*6=25分)
      广告量:        min(ads*2, 20) — 10 条封顶
      活跃中:        +8
      轨道分:        orbit * 0.2 — 100 分时给 20
      文本完整:      +7
      商业信号:      0-30 (来自 signals.py)
      高客单:        +10 (单价 >= threshold)
      是否有花费:    +5

    总分封顶 120. 最低准入门槛: score >= 55.
    """
    if not isinstance(comp, dict):
        return None
    landing = comp.get('landing_url', '') or ''
    domain = comp.get('landing', '') or ''
    if not domain:
        return None

    is_shopify, slug = _shopify_info(landing)
    score = 0.0
    reasons: list[str] = []

    if is_shopify:
        score += 20
        reasons.append("Shopify")

    age = int(comp.get('ad_age_days', 0) or 0)
    age_pts = min(age / 6.0, 25)
    score += age_pts
    if age >= 90:
        reasons.append(f"爆品{age}d")
    elif age >= 30:
        reasons.append(f"验证{age}d")
    elif age > 0:
        reasons.append(f"测试{age}d")

    ads = int(comp.get('ads', 0) or 0)
    score += min(ads * 2, 20)
    if ads >= 5:
        reasons.append(f"{ads}广告")

    if comp.get('is_active'):
        score += 8
        reasons.append("活跃")

    orbit = int(comp.get('brand_score', 0) or 0)
    score += min(orbit * 0.2, 20)
    if orbit >= 50:
        reasons.append(f"轨道{orbit}")

    if _text_completeness(comp.get('text_analysis', {})):
        score += 7
        reasons.append("文本完整")

    if comp.get('spend_estimate'):
        score += 5

    ta = comp.get('text_analysis', {}) or {}
    cs = comp.get('commercial_signals', {}) or {}
    sig_score = combined_signal_score(cs)
    if sig_score:
        score += sig_score
        if cs.get('price_main'):
            reasons.append(f"${cs['price_main']}")
        if cs.get('review_count_max', 0) >= 1000:
            rc = cs['review_count_max']
            reasons.append(f"{rc//1000}K+评论")
        if cs.get('star_rating', 0) >= 4.5:
            reasons.append(f"{cs['star_rating']}★")
        if cs.get('has_urgency'):
            reasons.append("紧迫")
        if cs.get('guarantee_markers'):
            reasons.append("保障")
        if cs.get('authority_markers'):
            reasons.append("权威")

    ht_threshold = get_high_ticket_threshold()
    is_high_ticket = bool(cs.get('price_main') and cs['price_main'] >= ht_threshold)
    if is_high_ticket:
        score += 10
        reasons.append(f"高客单")

    # FB Ads 适配性评分 (0-100, 贡献 0-30 到总分)
    fb_score, fb_breakdown, fb_tags = fb_fitness_score(comp, keyword)
    score += fb_score * 0.3
    if fb_score >= 75:
        reasons.append("FB强适配")
    elif fb_score >= 60:
        reasons.append("FB可投")
    if fb_tags:
        for t in fb_tags[:3]:
            if t.startswith('policy-risk') or t == 'saturated-PENALTY':
                reasons.append(f"⚠{t}")

    final = int(round(min(score, 120)))
    if final < 40:
        return None

    return {
        'domain': domain,
        'brand_name': safe_str(comp.get('brand'), UNKNOWN),
        'landing_url': safe_str(landing, DASH),
        'product_slug': safe_str(slug, DASH),
        'keyword': safe_str(keyword, UNKNOWN),
        'score': final,
        'ads_count': ads,
        'ad_age_days': age,
        'is_active': 1 if comp.get('is_active') else 0,
        'orbit_score': orbit,
        'is_shopify_shape': 1 if is_shopify else 0,
        'spend_estimate': json.dumps(comp.get('spend_estimate'), ensure_ascii=False) if comp.get('spend_estimate') else DASH,
        'top_pain': ', '.join((ta.get('top_pain_keywords') or [])[:3]) or NONE_LABEL,
        'top_scene': ', '.join((ta.get('top_scene_keywords') or [])[:3]) or NONE_LABEL,
        'top_product': ', '.join((ta.get('top_product_keywords') or [])[:3]) or slug or NONE_LABEL,
        'price_main': safe_float(cs.get('price_main')),
        'discount_pct': safe_int((cs.get('discount_pct') or [0])[0]) if cs.get('discount_pct') else 0,
        'review_count': safe_int(cs.get('review_count_max')),
        'star_rating': safe_float(cs.get('star_rating')),
        'has_urgency': 1 if cs.get('has_urgency') else 0,
        'has_guarantee': 1 if cs.get('guarantee_markers') else 0,
        'has_authority': 1 if cs.get('authority_markers') else 0,
        'signal_score': safe_int(sig_score),
        'fb_fitness': safe_int(fb_score),
        'fb_breakdown': json.dumps(fb_breakdown, ensure_ascii=False) if fb_breakdown else '{}',
        'fb_tags': json.dumps(fb_tags, ensure_ascii=False) if fb_tags else '[]',
        'reasons': ' | '.join(reasons) or DASH,
        'updated_at': datetime.now().isoformat(timespec='seconds'),
    }


def rebuild_recommendations() -> int:
    """扫所有 _corrected.json, 重建 recommendations 表. 返回条目数.
    健壮性: 每个文件独立 try/except, 单个坏文件不影响整体.
    """
    db = get_persistence()
    with db._conn() as conn:
        conn.executescript(RECOMMENDATIONS_SCHEMA)

    best: dict[str, dict] = {}
    files = list(RESULTS_DIR.glob('*_corrected.json'))
    ok_files = 0
    bad_files = 0
    for jf in files:
        if '汇总' in jf.name:
            continue
        try:
            data = json.loads(jf.read_text(encoding='utf-8'))
        except Exception as e:
            log.warning(f"损坏的 corrected: {jf.name}: {e}")
            bad_files += 1
            continue
        ok_files += 1
        kw = data.get('keyword', '') or ''
        scan_time = data.get('time') or data.get('scraped_at') or ''
        for comp in data.get('top_competitors', []):
            try:
                rec = score_competitor(comp, kw)
                if not rec:
                    continue
                # 记录该 rec 来源文件的扫描时间
                rec['scanned_at'] = scan_time
                ex = best.get(rec['domain'])
                if (not ex) or rec['score'] > ex['score']:
                    best[rec['domain']] = rec
                elif ex and scan_time > (ex.get('scanned_at') or ''):
                    # 同品牌不同源: 保留最新扫描时间
                    ex['scanned_at'] = scan_time
            except Exception as e:
                log.debug(f"score_competitor failed: {e}")

    if best:
        # 1. 按 score 降序排序, 硬封顶 Top N (env 可调)
        import os as _os
        RECOMMENDATIONS_CAP = int(_os.getenv('FB_RECOMMENDATIONS_CAP', '50000'))
        sorted_recs = sorted(best.values(), key=lambda r: r['score'], reverse=True)
        total_candidates = len(sorted_recs)
        cap_hit = total_candidates > RECOMMENDATIONS_CAP
        final_recs = sorted_recs[:RECOMMENDATIONS_CAP]

        # 2. 统计新增 + 保留 first_seen_at (跨 rebuild)
        db2 = get_persistence()
        prev_first_seen: dict[str, str] = {}
        try:
            with db2._conn() as c2:
                for r in c2.execute(
                    "SELECT domain, first_seen_at FROM recommendations"
                ):
                    if r[1]:
                        prev_first_seen[r[0]] = r[1]
        except Exception:
            pass  # 列或表还没有
        prev_domains = set(prev_first_seen.keys())
        new_domains = {r['domain'] for r in final_recs} - prev_domains

        now_iso = datetime.now().isoformat(timespec='seconds')
        for r in final_recs:
            r['first_seen_at'] = prev_first_seen.get(r['domain']) or now_iso

        # 3. 明显的日志块 (不弹桌面通知, 抓取过程不打扰)
        from .notify import banner
        cutoff_score = final_recs[-1]['score'] if final_recs else 0
        summary = (f"推荐 {len(final_recs)}/{RECOMMENDATIONS_CAP} "
                   f"(新增 {len(new_domains)}, score>={cutoff_score})")
        extra = []
        if cap_hit:
            extra.append(f"⚠️  候选 {total_candidates} 个, 超过 {RECOMMENDATIONS_CAP} "
                         f"硬顶, 已截断低分 {total_candidates - RECOMMENDATIONS_CAP} 个")
        banner(
            "✨ 新数据入库" if new_domains else "推荐重建完成",
            summary, *extra,
            highlight=bool(new_domains) or cap_hit,
        )

        rows = [(
            r['domain'], r['brand_name'], r['landing_url'], r['product_slug'],
            r['keyword'], r['score'], r['ads_count'], r['ad_age_days'],
            r['is_active'], r['orbit_score'], r['is_shopify_shape'],
            r['spend_estimate'], r['top_pain'], r['top_scene'], r['top_product'],
            r.get('price_main', 0), r.get('discount_pct', 0), r.get('review_count', 0),
            r.get('star_rating', 0.0), r.get('has_urgency', 0), r.get('has_guarantee', 0),
            r.get('has_authority', 0), r.get('signal_score', 0),
            r['reasons'], r.get('fb_fitness', 0), r.get('fb_breakdown', '{}'),
            r.get('fb_tags', '[]'),
            r['updated_at'],
            r.get('scanned_at', ''), r['first_seen_at'],
            r.get('strategy_tags', ''),
        ) for r in final_recs]
        with db._conn() as conn:
            conn.executescript(RECOMMENDATIONS_SCHEMA)
            for col_sql in [
                "ALTER TABLE recommendations ADD COLUMN price_main REAL",
                "ALTER TABLE recommendations ADD COLUMN discount_pct INTEGER",
                "ALTER TABLE recommendations ADD COLUMN review_count INTEGER",
                "ALTER TABLE recommendations ADD COLUMN star_rating REAL",
                "ALTER TABLE recommendations ADD COLUMN has_urgency INTEGER",
                "ALTER TABLE recommendations ADD COLUMN has_guarantee INTEGER",
                "ALTER TABLE recommendations ADD COLUMN has_authority INTEGER",
                "ALTER TABLE recommendations ADD COLUMN signal_score INTEGER",
                "ALTER TABLE recommendations ADD COLUMN scanned_at TEXT",
                "ALTER TABLE recommendations ADD COLUMN first_seen_at TEXT",
                "ALTER TABLE recommendations ADD COLUMN strategy_tags TEXT",
                "ALTER TABLE recommendations ADD COLUMN fb_fitness INTEGER",
                "ALTER TABLE recommendations ADD COLUMN fb_breakdown TEXT",
                "ALTER TABLE recommendations ADD COLUMN fb_tags TEXT",
            ]:
                try:
                    conn.execute(col_sql)
                except Exception:
                    pass
            # v10: UPSERT instead of DELETE+INSERT to preserve strategy_tags
            conn.executemany(
                """INSERT INTO recommendations
                   (domain, brand_name, landing_url, product_slug, keyword, score,
                    ads_count, ad_age_days, is_active, orbit_score, is_shopify_shape,
                    spend_estimate, top_pain, top_scene, top_product,
                    price_main, discount_pct, review_count, star_rating,
                    has_urgency, has_guarantee, has_authority, signal_score,
                    reasons, fb_fitness, fb_breakdown, fb_tags, updated_at,
                    scanned_at, first_seen_at, strategy_tags)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(domain) DO UPDATE SET
                    brand_name=excluded.brand_name, landing_url=excluded.landing_url,
                    product_slug=excluded.product_slug, keyword=excluded.keyword,
                    score=excluded.score, ads_count=excluded.ads_count,
                    ad_age_days=excluded.ad_age_days, is_active=excluded.is_active,
                    orbit_score=excluded.orbit_score, is_shopify_shape=excluded.is_shopify_shape,
                    spend_estimate=excluded.spend_estimate, top_pain=excluded.top_pain,
                    top_scene=excluded.top_scene, top_product=excluded.top_product,
                    price_main=excluded.price_main, discount_pct=excluded.discount_pct,
                    review_count=excluded.review_count, star_rating=excluded.star_rating,
                    has_urgency=excluded.has_urgency, has_guarantee=excluded.has_guarantee,
                    has_authority=excluded.has_authority, signal_score=excluded.signal_score,
                    reasons=excluded.reasons, fb_fitness=excluded.fb_fitness,
                    fb_breakdown=excluded.fb_breakdown, fb_tags=excluded.fb_tags,
                    updated_at=excluded.updated_at, scanned_at=excluded.scanned_at,
                    first_seen_at=excluded.first_seen_at,
                    strategy_tags=excluded.strategy_tags""",
                rows,
            )
            # Remove stale recommendations no longer in top results
            domains_in_batch = [r[0] for r in rows]
            if domains_in_batch:
                placeholders = ','.join('?' * len(domains_in_batch))
                conn.execute(
                    f"DELETE FROM recommendations WHERE domain NOT IN ({placeholders})",
                    domains_in_batch,
                )

    log.info(f"recommendations: {len(best)} 品牌入榜 (扫 {ok_files} corrected, 坏 {bad_files})")
    return len(best)


def list_recommendations(limit: int = 100, min_score: int = 60,
                         only_shopify: bool = False, days: int = 0,
                         offset: int = 0, strategy_tag: Optional[str] = None,
                         tier: Optional[str] = None,
                         exclude_domains: Optional[tuple] = None) -> list[dict]:
    """v10: SQL-level tier + date + shopify + domain filtering."""
    db = get_persistence()
    sql = "SELECT * FROM recommendations WHERE score >= ?"
    params: list = [min_score]
    if only_shopify:
        sql += " AND is_shopify_shape = 1"
    if days > 0:
        sql += " AND updated_at >= datetime('now', '-' || ? || ' days')"
        params.append(days)
    if strategy_tag:
        sql += " AND strategy_tags LIKE ?"
        params.append(f"%{strategy_tag}%")
    if tier == "testing":
        sql += " AND (ad_age_days IS NULL OR ad_age_days < 30)"
    elif tier == "validated":
        sql += " AND ad_age_days >= 30 AND ad_age_days < 90"
    elif tier == "proven":
        sql += " AND ad_age_days >= 90"
    if exclude_domains:
        placeholders = ','.join('?' * len(exclude_domains))
        sql += f" AND domain NOT IN ({placeholders})"
        params.extend(exclude_domains)
    sql += " ORDER BY score DESC, ad_age_days DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with db._conn() as conn:
        try:
            rows = [dict(r) for r in conn.execute(sql, params)]
            return [scrub_output(r) for r in rows]
        except Exception:
            conn.executescript(RECOMMENDATIONS_SCHEMA)
            return []


def count_recommendations(min_score: int = 60, only_shopify: bool = False, days: int = 0,
                          strategy_tag: Optional[str] = None,
                          tier: Optional[str] = None) -> int:
    db = get_persistence()
    sql = "SELECT COUNT(*) FROM recommendations WHERE score >= ?"
    params: list = [min_score]
    if only_shopify:
        sql += " AND is_shopify_shape = 1"
    if days > 0:
        sql += " AND updated_at >= datetime('now', '-' || ? || ' days')"
        params.append(days)
    if strategy_tag:
        sql += " AND strategy_tags LIKE ?"
        params.append(f"%{strategy_tag}%")
    if tier == "testing":
        sql += " AND (ad_age_days IS NULL OR ad_age_days < 30)"
    elif tier == "validated":
        sql += " AND ad_age_days >= 30 AND ad_age_days < 90"
    elif tier == "proven":
        sql += " AND ad_age_days >= 90"
    with db._conn() as conn:
        try:
            return conn.execute(sql, params).fetchone()[0]
        except Exception:
            return 0

"""pipeline/veteran_signals.py — 30 年老兵直觉转代码 (v9.2).

13 条洞见, 现有 ad_archive 数据 (commerce-only) 即可计算:

A. 现有数据立即可做 (本模块全部实现):
  1. 🎯 测-放大型 operator (test-and-scale) — 单 brand·单 SKU·collation_count 高
  2. 🐲 同图 dropshipper — image_url 在 ≥5 个 brand 复用
  3. 🚀 Rocket profile — domain 首次入库 < 14d + active 广告 ≥ 5
  4. 💰 订阅/补充制 — body 含 subscribe / refill / month
  5. 📦 Bundle/AOV — cards_count ≥ 3 + body 含 bundle / kit / pack
  6. 🌎 i18n 扩展 — target_locations ≥ 5 + languages ≥ 3
  7. ⚠️ TM/合规雷区 — body 含商标黑名单 / 健康宣称缺免责
  8. 🔁 创意疲劳速率 — 7d 新 active / 旧 active 比值
  9. 🎬 短视频甜区 — < 15s + Reels/IG 平台占比
 10. 📺 跨平台杠杆 — FB+IG+Messenger+Threads 同投
 11. 🌡️ 渗透率/HHI — Top-3 brand 占 keyword 的 ads %
 12. 🎯 跟买决策卡 — 单 brand 综合卡

B. 需外部集成 (本模块预留接口, 现阶段 placeholder):
 13. 🌐 LP 价格 / Klaviyo / 订阅签名 (lp_prober.py 已有, 后续 join)

API:
  veteran_flags(days=30, limit=50, only_active=True) -> {brands: [...]}
  saturation(keyword: str, days=30) -> {top3_share, hhi, new_entrants_per_week, verdict}
  follow_card(domain: str) -> {brand, portfolio, top_hook, suggested_entry_price, verdict}
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


# ── 词典 ───────────────────────────────────────────────────────────────────

# 常见被告 TM (FB Shopify 历史诉讼热点) — 保守列表, 命中即标黄
TM_BLACKLIST = {
    'squatty potty', 'spanx', 'sodastream', 'magic bullet', 'shark tank',
    'as seen on tv', 'instant pot', 'crocs', 'nintendo', 'disney',
    'pokemon', 'marvel', 'hello kitty', 'star wars',
    'apple watch', 'airpods', 'iphone', 'samsung galaxy',
    'rolex', 'gucci', 'louis vuitton', 'chanel', 'nike', 'adidas',
    'lululemon', 'yeti', 'hydro flask', 'patagonia',
    'tesla', 'taylor swift', 'beyonce',
}

# FDA / FTC 健康宣称地雷词 (无 disclaimer 又用这些词 → 高风险)
HEALTH_CLAIM_RX = re.compile(
    r'\b(fda\s*approved|cure[sd]?\b|treats?\s+(cancer|diabetes|covid|alzheimer)|'
    r'clinically\s+proven|guaranteed\s+results?|miracle\s+cure|'
    r'doctor\s+recommended|100%\s*safe)\b', re.I
)

# 订阅 / 补充制信号
SUBSCRIPTION_RX = re.compile(
    r'\b(subscribe|subscription|auto.?(deliver|ship|renew)|refill|'
    r'monthly\s+(box|kit|delivery)|recurring|\d+.day\s+supply|'
    r'cancel\s+anytime)\b', re.I
)

# Bundle / AOV 信号
BUNDLE_RX = re.compile(
    r'\b(bundle|kit|combo|set|gift\s+set|\d+.pack|pack\s+of\s+\d|'
    r'starter\s+pack|essentials|complete\s+collection)\b', re.I
)

# 紧迫感 (从 insights_v3 借)
URGENCY_RX = re.compile(
    r'\b(today|hurry|limited|sale|free\s+shipping|don\'?t\s+miss|'
    r'last\s+chance|while\s+supplies\s+last|ends\s+today|act\s+now|'
    r'flash|exclusive|while\s+stock|expir\w+)\b', re.I
)

PRICE_RX = re.compile(r'\$\s?\d+(\.\d{2})?\b')
DISCOUNT_RX = re.compile(r'\b(\d{1,2})\s?%\s?off\b', re.I)


# ── helpers ────────────────────────────────────────────────────────────────

def _conn():
    """Return Persistence._conn() context manager. Caller MUST use `with`.

    BUGFIX 2026-05-05: see insights_v3._conn — leaked-cm pattern closed conn
    before use under Py3.14 GC, raising ProgrammingError.
    """
    from .persistence import get_persistence
    return get_persistence()._conn()


def _has_ad_archive(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ad_archive'"
    ).fetchone())


def _safe_json(s, default):
    if not s or not isinstance(s, str):
        return default
    try:
        v = json.loads(s)
        return v if v is not None else default
    except Exception:
        return default


# ── 单 brand 信号计算 ───────────────────────────────────────────────────────

def _compute_brand_signals(conn: sqlite3.Connection, domain: str,
                            days: int = 30) -> dict:
    """对单个 brand (domain), 跑全部 13 条信号并返回 flags + 派生指标."""
    rows = conn.execute(
        f"""SELECT ad_archive_id, page_id, page_name, keyword, scan_date,
            start_date_iso, end_date_iso, is_active, active_days, display_format,
            cta_type, cta_text, title, body, link_url, ig_handle, ig_followers,
            page_likes, publisher_platforms, target_locations, target_ages,
            target_genders, languages, cards_count, video_duration_sec,
            video_url, image_url, collation_count, source, inserted_at
            FROM ad_archive
            WHERE LOWER(domain) = LOWER(?)
              AND inserted_at >= datetime('now', '-' || ? || ' days')""",
        (domain, days)
    ).fetchall()
    if not rows:
        return None

    rows = [dict(r) for r in rows]
    ads_n = len(rows)
    active_ads = [r for r in rows if r.get('is_active')]
    active_n = len(active_ads)

    flags: list = []   # [(emoji, code, reason)]
    score = 0          # 综合分 0-100, 越高越值得跟

    # 收集基础聚合
    page_names = Counter(r.get('page_name') or '' for r in rows)
    page_name = page_names.most_common(1)[0][0] if page_names else ''
    page_likes_max = max((r.get('page_likes') or 0) for r in rows)
    ig_handles = {r.get('ig_handle') for r in rows if r.get('ig_handle')}
    ig_handle = next(iter(ig_handles), '') if ig_handles else ''
    ig_followers_max = max((r.get('ig_followers') or 0) for r in rows)

    keywords_set = {r.get('keyword') for r in rows if r.get('keyword')}
    active_days_list = sorted([r.get('active_days') or 0 for r in active_ads])

    p50 = active_days_list[len(active_days_list)//2] if active_days_list else 0
    pmax = active_days_list[-1] if active_days_list else 0
    avg_active_days = sum(active_days_list) / max(len(active_days_list), 1)

    # ── #1 测-放大型 operator ───────────────────────────────
    coll_max = max((r.get('collation_count') or 1) for r in rows)
    coll_total = sum((r.get('collation_count') or 1) for r in rows)
    if coll_max >= 20 and avg_active_days < 30:
        flags.append(('🎯', 'TEST_SCALE',
                      f'单广告 {coll_max} 变体 + 平均 {avg_active_days:.0f}d 活跃 = 测-放大型操盘手'))
        score += 20

    # ── #3 Rocket profile (简化版: 库内首次见到 < 14d + active ≥ 5) ─────────
    first_seen = min((r.get('inserted_at') or '') for r in rows)
    if first_seen:
        try:
            first_dt = datetime.fromisoformat(first_seen.split('.')[0])
            days_since_first = (datetime.now() - first_dt).days
        except Exception:
            days_since_first = 999
    else:
        days_since_first = 999
    if days_since_first < 14 and active_n >= 5:
        flags.append(('🚀', 'ROCKET',
                      f'首次入库 {days_since_first}d, 当前 {active_n} active 广告 = 闪电起势'))
        score += 25

    # ── #4 订阅/补充制 ─────────────────────────────────────
    sub_hits = sum(1 for r in rows if SUBSCRIPTION_RX.search(r.get('body') or ''))
    if sub_hits >= max(3, ads_n * 0.2):
        flags.append(('💰', 'SUBSCRIPTION',
                      f'{sub_hits}/{ads_n} 广告含订阅词 = LTV 复利型'))
        score += 15

    # ── #5 Bundle / AOV ────────────────────────────────────
    bundle_hits = sum(1 for r in rows
                      if (r.get('cards_count') or 0) >= 3
                      or BUNDLE_RX.search(r.get('body') or ''))
    if bundle_hits >= max(2, ads_n * 0.15):
        flags.append(('📦', 'BUNDLE_AOV',
                      f'{bundle_hits}/{ads_n} 广告主推 bundle/kit = AOV 优化型'))
        score += 10

    # ── #6 i18n 扩展 ───────────────────────────────────────
    locs_set: set = set()
    langs_set: set = set()
    for r in rows:
        locs_set.update(_safe_json(r.get('target_locations'), []))
        langs_set.update(_safe_json(r.get('languages'), []))
    if len(locs_set) >= 5 and len(langs_set) >= 3:
        flags.append(('🌎', 'I18N',
                      f'已投 {len(locs_set)} 国 / {len(langs_set)} 语 = 国际化品牌'))
        score += 8

    # ── #7 TM / 健康宣称合规雷区 ───────────────────────────
    tm_hits: list = []
    health_hits = 0
    for r in rows:
        body_lower = (r.get('body') or '').lower()
        title_lower = (r.get('title') or '').lower()
        full = body_lower + ' ' + title_lower
        for tm in TM_BLACKLIST:
            if tm in full:
                tm_hits.append(tm)
                break
        if HEALTH_CLAIM_RX.search(full):
            health_hits += 1
    if tm_hits:
        unique_tm = sorted(set(tm_hits))[:3]
        flags.append(('⚠️', 'TM_RISK',
                      f'命中 TM 黑名单: {", ".join(unique_tm)} = 法律风险'))
        score -= 25
    if health_hits >= max(2, ads_n * 0.2):
        flags.append(('🩺', 'HEALTH_CLAIM_RISK',
                      f'{health_hits} 条广告含 FDA/cure 类宣称 = 合规雷'))
        score -= 15

    # ── #8 创意疲劳速率 (7d 新 active / 旧 active) ────────────
    cutoff_iso = (datetime.now()).strftime('%Y-%m-%d')
    new_active = sum(1 for r in active_ads
                     if (r.get('inserted_at') or '')[:10] >= (
                         (datetime.now().replace(microsecond=0)).strftime('%Y-%m-%d')
                     ))
    # 简化: 用 inserted_at 七日内计数
    week_ago = conn.execute(
        "SELECT datetime('now', '-7 days') AS d"
    ).fetchone()['d']
    new_7d = sum(1 for r in rows
                 if (r.get('inserted_at') or '') >= week_ago and r.get('is_active'))
    old_active = max(active_n - new_7d, 0)
    if active_n >= 5:
        ratio = new_7d / max(active_n, 1)
        if ratio > 0.6:
            flags.append(('🔁', 'HIGH_CHURN',
                          f'7d 新 {new_7d}/{active_n} active = 高轮换 (难抄)'))
            score -= 5
        elif ratio < 0.15 and active_n >= 8:
            flags.append(('💎', 'LOW_CHURN',
                          f'7d 新 {new_7d}/{active_n} active = 单一爆款型 (易抄)'))
            score += 12

    # ── #9 短视频甜区 (Reels < 15s) ────────────────────────
    short_video = sum(1 for r in rows
                      if (r.get('video_duration_sec') or 0) > 0
                      and (r.get('video_duration_sec') or 0) < 15)
    video_total = sum(1 for r in rows if (r.get('video_duration_sec') or 0) > 0)
    if video_total >= 3 and short_video / video_total > 0.5:
        flags.append(('🎬', 'SHORT_VIDEO',
                      f'{short_video}/{video_total} 视频 < 15s = Reels 风口'))
        score += 8

    # ── #10 跨平台杠杆 ─────────────────────────────────────
    plats_per_ad = []
    for r in rows:
        plats = _safe_json(r.get('publisher_platforms'), [])
        plats_per_ad.append(len(set(p for p in plats if p)))
    if plats_per_ad and max(plats_per_ad) >= 4:
        flags.append(('📺', 'CROSS_PLATFORM',
                      f'最高单广告投 {max(plats_per_ad)} 平台 = scaling 中, 跟买信心强'))
        score += 10

    # ── 长寿赢家 (extra) ───────────────────────────────────
    if pmax >= 90:
        flags.append(('🏆', 'LONG_RUNNER',
                      f'最长 {pmax}d 仍跑 = 验证过的核心爆款'))
        score += 20

    # ── 整理 ──────────────────────────────────────────────
    score = max(0, min(100, score + 30))   # base 30, 加减后 clip 到 [0,100]

    verdict = (
        'GO_FOLLOW'    if score >= 70 and not tm_hits else
        'OBSERVE'      if score >= 45 else
        'AVOID'        if tm_hits or health_hits >= 5 else
        'WATCH'
    )

    return {
        'domain': domain,
        'page_name': page_name,
        'page_likes_max': page_likes_max,
        'ig_handle': ig_handle,
        'ig_followers': ig_followers_max,
        'ads_total': ads_n,
        'ads_active': active_n,
        'first_seen': first_seen[:10] if first_seen else '',
        'days_since_first': days_since_first,
        'avg_active_days': round(avg_active_days, 1),
        'max_active_days': pmax,
        'collation_max': coll_max,
        'collation_total': coll_total,
        'targets_countries': len(locs_set),
        'targets_languages': len(langs_set),
        'keywords_count': len(keywords_set),
        'flags': [{'emoji': e, 'code': c, 'reason': r} for e, c, r in flags],
        'flag_codes': [c for _, c, _ in flags],
        'score': score,
        'verdict': verdict,
    }


# ── 公开 API ────────────────────────────────────────────────────────────────

def veteran_flags(days: int = 30, limit: int = 50,
                  only_active: bool = True,
                  min_ads: int = 2,
                  verdict: Optional[str] = None) -> dict:
    """对最近 N 天活跃过的 brand 计算老兵 flags. 按 score DESC 返回 top-N.

    only_active: 只考虑当前还有 active 广告的 brand
    verdict: 过滤 'GO_FOLLOW' / 'OBSERVE' / 'WATCH' / 'AVOID' (None=全部)
    """
    out: list = []
    with _conn() as conn:
        if not _has_ad_archive(conn):
            return {'error': 'ad_archive table not yet populated', 'brands': []}

        # 候选 brands: 最近 N 天有过广告 + 域名非空 + 至少 min_ads 条
        sql = (
            "SELECT domain, COUNT(*) AS n, "
            "SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS active_n "
            "FROM ad_archive WHERE domain != '' "
            "AND inserted_at >= datetime('now', '-' || ? || ' days') "
            "GROUP BY domain HAVING n >= ? "
        )
        if only_active:
            sql += " AND active_n >= 1 "
        sql += " ORDER BY active_n DESC, n DESC LIMIT 500"
        candidate_domains = [r['domain'] for r in conn.execute(sql, (days, min_ads))]

        # ── #2 同图 dropshipper 检测 ── 算所有 ad 的 image_url 出现次数, 跨 brand
        image_brand_map: dict = defaultdict(set)
        for r in conn.execute(
            "SELECT image_url, domain FROM ad_archive "
            "WHERE image_url != '' AND domain != '' "
            "AND inserted_at >= datetime('now', '-' || ? || ' days')",
            (days,)
        ):
            url = (r['image_url'] or '').split('?')[0][:200]  # 去 query 截断
            if url:
                image_brand_map[url].add(r['domain'])
        dropship_domains: set = set()
        for url, brands in image_brand_map.items():
            if len(brands) >= 5:
                dropship_domains.update(brands)

        for domain in candidate_domains:
            sig = _compute_brand_signals(conn, domain, days=days)
            if not sig:
                continue
            # 注入 dropship flag
            if domain in dropship_domains:
                sig['flags'].insert(0, {
                    'emoji': '🐲', 'code': 'CATALOG_DROPSHIP',
                    'reason': '主图在 ≥5 个其他 brand 复用 = AliExpress 目录货, 红海避雷'
                })
                sig['flag_codes'].insert(0, 'CATALOG_DROPSHIP')
                sig['score'] = max(0, sig['score'] - 30)
                if 'AVOID' not in sig['verdict']:
                    sig['verdict'] = 'AVOID'
            if verdict and sig['verdict'] != verdict:
                continue
            out.append(sig)

        out.sort(key=lambda x: -x['score'])
        out = out[:limit]

    return {
        'window_days': days,
        'count': len(out),
        'brands': out,
    }


def saturation(keyword: str, days: int = 30) -> dict:
    """单关键词的渗透率 / HHI / 新进入者率."""
    if not keyword:
        return {'error': 'keyword required'}
    with _conn() as conn:
        if not _has_ad_archive(conn):
            return {'error': 'ad_archive table not yet populated'}
        rows = conn.execute(
            "SELECT domain, COUNT(*) AS n, MIN(inserted_at) AS first_seen "
            "FROM ad_archive WHERE LOWER(keyword)=LOWER(?) AND domain != '' "
            "AND inserted_at >= datetime('now', '-' || ? || ' days') "
            "GROUP BY domain ORDER BY n DESC",
            (keyword, days)
        ).fetchall()
        rows = [dict(r) for r in rows]
        total = sum(r['n'] for r in rows) or 1
        if not rows:
            return {'keyword': keyword, 'window_days': days,
                    'total_ads': 0, 'brands': 0,
                    'top3_share_pct': 0.0, 'hhi': 0.0,
                    'new_entrants_7d': 0, 'top_brands': [],
                    'verdict': 'NO_DATA'}

        top3_share = sum(r['n'] for r in rows[:3]) / total * 100
        # HHI: 各品牌广告占比平方和 (×10000)
        hhi = sum((r['n'] / total) ** 2 for r in rows) * 10000

        # 新进入者: 7d 内首次出现的 brand 数
        cutoff = conn.execute(
            "SELECT datetime('now', '-7 days') AS d"
        ).fetchone()['d']
        new_entrants = sum(1 for r in rows if (r['first_seen'] or '') >= cutoff)
        new_per_week = new_entrants

        # verdict
        if hhi > 4000 and top3_share > 60:
            v = 'LOCKED'        # 高度集中, 头部锁死, 不推荐
        elif hhi > 2500:
            v = 'TIGHT'         # 偏紧, 需差异化
        elif new_per_week >= 3 and hhi < 1500:
            v = 'OPEN'          # 新人持续涌入 + 分散, 可冲
        elif new_per_week >= 1:
            v = 'GROWING'       # 渐进式
        else:
            v = 'STAGNANT'      # 几无新血, 老牌坐庄

    return {
        'keyword': keyword,
        'window_days': days,
        'total_ads': total,
        'brands': len(rows),
        'top3_share_pct': round(top3_share, 1),
        'hhi': round(hhi, 0),
        'new_entrants_7d': new_per_week,
        'top_brands': [{'domain': r['domain'], 'ads': r['n'],
                        'share_pct': round(r['n']/total*100, 1),
                        'first_seen': (r['first_seen'] or '')[:10]}
                       for r in rows[:10]],
        'verdict': v,
    }


def follow_card(domain: str, days: int = 60) -> dict:
    """单 brand 跟买决策卡 — 老兵给徒弟看的那张纸."""
    if not domain:
        return {'error': 'domain required'}
    with _conn() as conn:
        if not _has_ad_archive(conn):
            return {'error': 'ad_archive table not yet populated'}

        sig = _compute_brand_signals(conn, domain, days=days)
        if not sig:
            return {'error': 'no ads found for domain in window',
                    'domain': domain, 'window_days': days}

        # 拉所有 ads 详细
        rows = [dict(r) for r in conn.execute(
            """SELECT ad_archive_id, title, body, cta_type, cta_text, link_url,
               display_format, video_duration_sec, cards_count, image_url,
               video_url, active_days, is_active, start_date_iso,
               publisher_platforms, target_locations, target_ages, target_genders,
               keyword, ad_snapshot_url
               FROM ad_archive
               WHERE LOWER(domain)=LOWER(?)
                 AND inserted_at >= datetime('now', '-' || ? || ' days')
               ORDER BY active_days DESC""",
            (domain, days)
        )]

    # Top hook: 最高频 (title|body 前 100 字符 lower)
    hook_counter = Counter()
    cta_counter = Counter()
    cta_text_counter = Counter()
    fmt_counter = Counter()
    kw_counter = Counter()
    plat_counter = Counter()
    for r in rows:
        t = (r.get('title') or '').strip()
        if t:
            hook_counter[t[:140]] += 1
        cta = r.get('cta_type') or ''
        if cta:
            cta_counter[cta] += 1
        ct = r.get('cta_text') or ''
        if ct:
            cta_text_counter[ct[:60]] += 1
        f = r.get('display_format') or ''
        if f:
            fmt_counter[f] += 1
        kw = r.get('keyword') or ''
        if kw:
            kw_counter[kw] += 1
        for p in _safe_json(r.get('publisher_platforms'), []):
            plat_counter[str(p)] += 1

    top_hooks = [{'hook': h, 'count': n} for h, n in hook_counter.most_common(5)]
    top_ctas = [{'cta_type': c, 'count': n} for c, n in cta_counter.most_common(5)]
    top_kws = [{'keyword': k, 'count': n} for k, n in kw_counter.most_common(8)]
    plat_mix = [{'platform': p, 'count': n} for p, n in plat_counter.most_common(8)]

    # 价格出现率
    price_hits = sum(1 for r in rows if PRICE_RX.search((r.get('body') or '') + ' ' + (r.get('title') or '')))
    discount_pcts: list = []
    for r in rows:
        m = DISCOUNT_RX.search((r.get('body') or '') + ' ' + (r.get('title') or ''))
        if m:
            try:
                discount_pcts.append(int(m.group(1)))
            except Exception:
                pass

    # 长寿赢家 (≥60d 仍 active, 用作模仿模板)
    winners = [r for r in rows if r.get('is_active') and (r.get('active_days') or 0) >= 60]
    winners.sort(key=lambda x: -(x.get('active_days') or 0))
    winners_summary = [{
        'ad_archive_id': w['ad_archive_id'],
        'active_days': w['active_days'],
        'title': (w.get('title') or '')[:120],
        'body': (w.get('body') or '')[:200],
        'cta_type': w.get('cta_type'),
        'cta_text': w.get('cta_text'),
        'format': w.get('display_format'),
        'has_video': bool(w.get('video_url')),
        'is_carousel': (w.get('cards_count') or 0) > 1,
        'fb_url': f"https://www.facebook.com/ads/library/?id={w['ad_archive_id']}",
    } for w in winners[:5]]

    # 操作建议
    plays = []
    if 'TEST_SCALE' in sig['flag_codes']:
        plays.append('🎯 学他们的 A/B 节奏: 1 个 SKU 跑 N 个 hook, 留下数据最好的 2 条扩量')
    if 'ROCKET' in sig['flag_codes']:
        plays.append('🚀 立即跟 — 闪电起势期, 流量红利还在, 但要 7 天内决断')
    if 'SUBSCRIPTION' in sig['flag_codes']:
        plays.append('💰 抄他们的订阅诱饵: subscribe & save / X-day supply 文案')
    if 'BUNDLE_AOV' in sig['flag_codes']:
        plays.append('📦 跟做 bundle: 单品 cards_count 至少 3 张, 走 AOV 提升路线')
    if 'LONG_RUNNER' in sig['flag_codes']:
        plays.append('🏆 抄他们最久的那条 (即长寿赢家) — 验证过的 hook')
    if 'CATALOG_DROPSHIP' in sig['flag_codes']:
        plays = ['🐲 不跟 — 同图复用 ≥5 brand 已是红海, 进去就是陪跑']
    if 'TM_RISK' in sig['flag_codes']:
        plays.append('⚠️ 跟之前先查商标! 命中黑名单 = 法律风险')
    if not plays:
        plays.append('观察期 — 信号不足, 先盯一周再决策')

    # 进入价格点建议 (没有 LP 数据, 给经验值)
    suggested = '需 LP 探针拿到价格后, 建议比对手均价低 15-20% 切入'

    return {
        'domain': domain,
        'window_days': days,
        'signal': sig,
        'portfolio': {
            'ads_total': len(rows),
            'ads_active': sum(1 for r in rows if r.get('is_active')),
            'video_count': sum(1 for r in rows if r.get('video_url')),
            'image_count': sum(1 for r in rows
                               if r.get('image_url') and not r.get('video_url')
                               and (r.get('cards_count') or 0) <= 1),
            'carousel_count': sum(1 for r in rows if (r.get('cards_count') or 0) > 1),
            'price_mention_rate_pct': round(price_hits / max(len(rows), 1) * 100, 1),
            'avg_discount_pct': round(sum(discount_pcts)/len(discount_pcts), 1) if discount_pcts else 0,
        },
        'top_hooks': top_hooks,
        'top_ctas': top_ctas,
        'top_cta_texts': [{'cta_text': t, 'count': n}
                          for t, n in cta_text_counter.most_common(5)],
        'format_mix': [{'format': k, 'count': v}
                        for k, v in fmt_counter.most_common(5)],
        'platform_mix': plat_mix,
        'top_keywords': top_kws,
        'long_running_winners': winners_summary,
        'suggested_entry_price': suggested,
        'plays': plays,
        'verdict': sig['verdict'],
        'score': sig['score'],
    }

"""insights_2026_v2.py — 顶级 FB Ads 选品大师的 2026 Q2 高阶信号库.

本模块在 insights_2026.py (Tier 1-3 静态知识) 之上, 提供"动态信号":
  • Velocity   — 速度 > 绝对值 (ad_count 14 天增长斜率)
  • Diversity  — creative variant 数量 (Andromeda 偏好)
  • Age-Curve  — 45-120 天甜蜜区 vs >365 天衰退
  • Cluster    — 扎堆 (saturated) vs 2-3 品牌角度分化 (emerging)
  • LP-Fingerprint — Shopify+Klaviyo+Judge.me+Sticky = 真 D2C
  • Geo-Expand — US → US+CA+UK → +EU  scaling 的 DNA
  • Fatigue    — 同广告 >60 天无新素材 = 将死
  • Cross-Plat — FB↔TikTok 同时跑 = 双验证
  • Engagement — comment velocity (前 48h) 是 Andromeda 阈值
  • Reddit-Val — Reddit harvester 命中次数 ≥ 2 = 真实需求

所有函数纯 stdlib + 容错, 输入字段缺失时返回 0 分, 不抛异常.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════
# ① Velocity — ad_count 14 天增长斜率 (最重要的单一信号)
# ═══════════════════════════════════════════════════════════════════════════
# 规则:
#   growth_ratio = ad_count_now / max(ad_count_14d_ago, 1)
#   ratio >= 5.0  → 火箭 (+25)
#   ratio >= 3.0  → 快速扩张 (+18)
#   ratio >= 1.8  → 健康增长 (+10)
#   0.9 <= r < 1.2 → 稳定 (0)
#   ratio < 0.7   → 萎缩 (-10)  品牌在收缩预算
def velocity_score(ad_count_now: int, ad_count_14d_ago: Optional[int]) -> tuple[int, str]:
    if not ad_count_now or not ad_count_14d_ago:
        return (0, 'velocity:unknown')
    ratio = ad_count_now / max(ad_count_14d_ago, 1)
    if ratio >= 5.0:   return (25, f'velocity:rocket×{ratio:.1f}')
    if ratio >= 3.0:   return (18, f'velocity:fast×{ratio:.1f}')
    if ratio >= 1.8:   return (10, f'velocity:healthy×{ratio:.1f}')
    if ratio >= 1.2:   return (5,  f'velocity:up×{ratio:.1f}')
    if 0.9 <= ratio < 1.2: return (0, f'velocity:flat×{ratio:.1f}')
    if ratio < 0.7:    return (-10, f'velocity:shrinking×{ratio:.1f}')
    return (0, f'velocity:slow×{ratio:.1f}')


# ═══════════════════════════════════════════════════════════════════════════
# ② Creative Diversity — Andromeda 偏好素材矩阵品牌
# ═══════════════════════════════════════════════════════════════════════════
# variant_count = 广告下 format/hook/title 去重后 variant 数.
#   1-2    → 新手 / 单爆款未矩阵 (0)
#   3-5    → 开始矩阵化 (+8)
#   6-10   → 矩阵完备 CBO/ASC+ 期 (+15)
#   11-20  → 老手在 scaling (+12, 追风险开始上升)
#   >20    → 可能饱和或大号 (+5)
def creative_diversity_score(variant_total: int) -> tuple[int, str]:
    v = int(variant_total or 0)
    if v <= 2:   return (0,  f'diversity:new({v})')
    if v <= 5:   return (8,  f'diversity:matrix-start({v})')
    if v <= 10:  return (15, f'diversity:mature({v})')
    if v <= 20:  return (12, f'diversity:scaling({v})')
    return (5, f'diversity:big-brand({v})')


# ═══════════════════════════════════════════════════════════════════════════
# ③ Age Curve — 45-120 天甜蜜区
# ═══════════════════════════════════════════════════════════════════════════
# 旧规则: >90 天 = 爆品 (错误, 会跟到在下滑的老品)
# 2026 真相:
#   <14    → 尚未证明 (-5)
#   14-44  → 测试期 (+5)     已过测款
#   45-120 → SWEET SPOT (+20)  past beta, 未饱和, 仍在 ASC+
#   121-270 → 成熟期 (+10)    仍可跟但利润压缩
#   271-365 → 晚期 (+0)       爆品末期, 小众跟投
#   >365   → 长尾衰退 (-5)    CPM 涨, ROI 降, 别新入场
def age_curve_score(ad_age_days: int) -> tuple[int, str]:
    age = int(ad_age_days or 0)
    if age < 14:    return (-5, f'age:unproven({age}d)')
    if age < 45:    return (5,  f'age:testing({age}d)')
    if age <= 120:  return (20, f'age:SWEET-SPOT({age}d)')
    if age <= 270:  return (10, f'age:mature({age}d)')
    if age <= 365:  return (0,  f'age:late({age}d)')
    return (-5, f'age:decaying({age}d)')


# ═══════════════════════════════════════════════════════════════════════════
# ④ Cluster Signal — saturated vs emerging niche
# ═══════════════════════════════════════════════════════════════════════════
# 传入同品类 (keyword/niche) 下独立品牌数 + 广告总数:
#   1 品牌                  → 孤狼测款 (0)
#   2-3 品牌 + 角度不同     → EMERGING (+15)  最好的跟投时机
#   4-6 品牌                → 起势期 (+8)     可跟, 但要差异化
#   7-10 品牌               → 拥挤 (+0)
#   >10 品牌                → SATURATED (-15)  红海, 除非有爆款级创新
#
# angle_diversity: 低 = 大家抄同一套, 高 = 创意多元(更健康的 niche)
def cluster_score(brand_count: int, angle_diversity: float = 0.5) -> tuple[int, str]:
    b = int(brand_count or 0)
    if b <= 1:   return (0,  f'cluster:solo({b})')
    if b <= 3:
        bonus = 15 if angle_diversity >= 0.5 else 8
        return (bonus, f'cluster:EMERGING({b})')
    if b <= 6:   return (8,  f'cluster:rising({b})')
    if b <= 10:  return (0,  f'cluster:crowded({b})')
    return (-15, f'cluster:SATURATED({b})')


# ═══════════════════════════════════════════════════════════════════════════
# ⑤ Landing Page Fingerprint — 真 D2C vs dropship
# ═══════════════════════════════════════════════════════════════════════════
# 检测落地页 HTML 里的技术签名 (传入 html/tags list):
#   Shopify (cdn.shopify.com)         真独立站骨架
#   Klaviyo (klaviyo.js / kla.js)     邮件营销老手
#   Judge.me / Loox / Stamped         评价系统 = 长期经营
#   ReCharge / Bold Subscription      订阅模型 = LTV 思维
#   PayPal + Shop Pay + ApplePay      正经收款
#   Hotjar / Microsoft Clarity        数据驱动
#   Zendesk / Gorgias / Intercom      客服系统 = 真订单量
#   Triple Whale / Northbeam          归因 = 花钱花得细
LP_TECH_SIGNATURES = {
    'shopify': (re.compile(r'cdn\.shopify\.com|shopify_stats|Shopify\.theme', re.I), 10),
    'klaviyo': (re.compile(r'klaviyo\.js|kla\.js|_learnq|klaviyo_identify', re.I), 8),
    'reviews': (re.compile(r'judge\.me|loox|stamped|yotpo|okendo', re.I), 6),
    'subscription': (re.compile(r'recharge|bold.?subscription|rechargepayments', re.I), 7),
    'upsell': (re.compile(r'zipify|reconvert|honeycomb|rebuy', re.I), 5),
    'analytics': (re.compile(r'triplewhale|northbeam|hotjar|clarity\.ms', re.I), 4),
    'support': (re.compile(r'gorgias|zendesk|intercom|tidio', re.I), 4),
    'payment': (re.compile(r'shop.pay|apple.pay|afterpay|klarna|stripe', re.I), 3),
}

def lp_fingerprint_score(html_or_tags: str) -> tuple[int, list[str]]:
    if not html_or_tags:
        return (0, [])
    bonus = 0
    hits = []
    for name, (regex, pts) in LP_TECH_SIGNATURES.items():
        if regex.search(html_or_tags):
            bonus += pts
            hits.append(f'lp:{name}')
    # 综合加分封顶 (避免单站堆 8 个 tag 爆分)
    return (min(bonus, 30), hits)


def lp_fingerprint_from_cache(cache: dict | None) -> tuple[int, list[str], int, int]:
    """从 lp_prober 缓存直接读取 (bonus, tags, dropship_penalty, reddit_bonus).

    dropship 命中 → -25 分 (真 D2C 和 dropshipper 的分水岭).
    reddit_bonus = trust_badges + single_product + return_policy - discount_stack.
    """
    if not cache:
        return (0, [], 0, 0)
    bonus = int(cache.get('bonus') or 0)
    tags = list(cache.get('tags') or [])
    penalty = -25 if cache.get('has_dropship') else 0
    reddit_b = int(cache.get('reddit_bonus') or 0)
    if penalty:
        tags = tags + ['lp:DROPSHIP-PENALTY']
    return (bonus, tags, penalty, reddit_b)


# ═══════════════════════════════════════════════════════════════════════════
# ⑪ Price-Band Signal — Andromeda 偏好的冲动购价格带
# ═══════════════════════════════════════════════════════════════════════════
# Reddit 2026 Q2 共识:
#   $0-19:  低客单,靠量 + 订阅复购 · 利润薄 (+0)
#   $20-39: FB 冲动买甜蜜区 1 (+12)  ← 15-90s 决策
#   $40-89: FB 冲动买甜蜜区 2 (+15)  ← Klaviyo flows 黄金区
#   $90-149: 中客单,需强证据 · AOV 爆发 (+10)
#   $150-299: 高客单,需 Afterpay/Klarna · 高毛利 (+6)
#   $300+:  高端礼品/家电 · 需 90 天 nurture (+3)
#   未知 → 0
def price_band_score(price: float | None) -> tuple[int, str]:
    if not price or price <= 0:
        return (0, '')
    p = float(price)
    if p < 20:   return (0,  f'price:low(${p:.0f})')
    if p < 40:   return (12, f'price:SWEET-1(${p:.0f})')
    if p < 90:   return (15, f'price:SWEET-2(${p:.0f})')
    if p < 150:  return (10, f'price:AOV-zone(${p:.0f})')
    if p < 300:  return (6,  f'price:premium(${p:.0f})')
    return (3, f'price:luxury(${p:.0f})')


# ═══════════════════════════════════════════════════════════════════════════
# ⑥ Geo Expansion — scaling winner 的 DNA
# ═══════════════════════════════════════════════════════════════════════════
# countries = set of ISO codes ad is active in
#   {US}              → 冷启动 (0)
#   {US, CA}          → North America (+3)
#   {US, CA, UK}      → Tier-1 英语扩张 (+10)
#   +AU/NZ/IE         → Tier-1 完整 (+15)
#   +DE/FR/NL/SE      → EU Tier-1 scaling (+20)
#   +10 countries     → 全球 scaling (+25)  真爆品
TIER1_ENGLISH = {'US', 'UK', 'GB', 'CA', 'AU', 'NZ', 'IE'}
EU_TIER1 = {'DE', 'FR', 'NL', 'SE', 'DK', 'NO', 'FI', 'CH', 'AT', 'BE'}

def geo_expansion_score(country_codes: set[str] | list[str]) -> tuple[int, str]:
    if not country_codes:
        return (0, 'geo:none')
    countries = set(c.upper() for c in country_codes)
    tier1 = len(countries & TIER1_ENGLISH)
    eu = len(countries & EU_TIER1)
    total = len(countries)

    if total >= 10:                return (25, f'geo:GLOBAL({total})')
    if eu >= 3:                    return (20, f'geo:EU-scaling({total},EU{eu})')
    if tier1 >= 4:                 return (15, f'geo:Tier1-full({total})')
    if tier1 >= 3:                 return (10, f'geo:Tier1-english({total})')
    if tier1 == 2:                 return (3,  f'geo:NA-only({total})')
    return (0, f'geo:solo({total})')


# ═══════════════════════════════════════════════════════════════════════════
# ⑦ Creative Fatigue — 同广告 >60 天无新素材 = 将死
# ═══════════════════════════════════════════════════════════════════════════
# days_since_latest_creative: 距离最新 creative 上架的天数
#   <14 → 持续出新 (+5)
#   14-45 → 正常 (0)
#   46-90 → FATIGUE risk (-5)
#   >90 → DEAD creative, 老 winner 吃老本 (-10)
def fatigue_score(days_since_latest: int) -> tuple[int, str]:
    d = int(days_since_latest or 0)
    if d < 14:   return (5, f'fatigue:fresh({d}d)')
    if d < 46:   return (0, f'fatigue:ok({d}d)')
    if d < 91:   return (-5, f'fatigue:risk({d}d)')
    return (-10, f'fatigue:DEAD({d}d)')


# ═══════════════════════════════════════════════════════════════════════════
# ⑧ Cross-Platform — FB ↔ TikTok 同时跑 = 双验证
# ═══════════════════════════════════════════════════════════════════════════
# 本系统只抓 FB,但我们可以通过 brand_name 的 Andromeda hint 猜测:
#   广告格式 = video + vertical aspect + 标题含 "tiktok" / "fyp" → TikTok 回流
#   caption 含 "POV / stitch / duet / for you" → 原 TikTok 素材搬运
# 只要命中其中一种,+10
_TT_DNA_RE = re.compile(
    r'\b(tiktok|#fyp|for you page|pov |stitch this|duet|storytime)\b',
    re.IGNORECASE)

def cross_platform_score(ad_text: str, has_vertical_video: bool = False) -> tuple[int, str]:
    if not ad_text and not has_vertical_video:
        return (0, '')
    if ad_text and _TT_DNA_RE.search(ad_text):
        return (10, 'cross:TT-dna')
    if has_vertical_video:
        return (4, 'cross:vertical')
    return (0, '')


# ═══════════════════════════════════════════════════════════════════════════
# ⑨ Engagement Velocity — 评论/分享数 在首 48h 是 Andromeda 阈值
# ═══════════════════════════════════════════════════════════════════════════
# FB 不直接给 comment_count,但我们可以用 likes + ad_age 反推:
#   likes_per_day = likes / max(ad_age_days, 1)
#   >500/day  → VIRAL (+20)
#   100-500   → 强 (+12)
#   30-99     → 正常 (+5)
#   <30       → 冷启动 / 低互动 (0)
def engagement_velocity_score(likes: int, ad_age_days: int) -> tuple[int, str]:
    likes = int(likes or 0)
    age = max(int(ad_age_days or 1), 1)
    lpd = likes / age
    if lpd >= 500:  return (20, f'engage:VIRAL({lpd:.0f}/d)')
    if lpd >= 100:  return (12, f'engage:strong({lpd:.0f}/d)')
    if lpd >= 30:   return (5,  f'engage:normal({lpd:.0f}/d)')
    return (0, f'engage:cold({lpd:.0f}/d)')


# ═══════════════════════════════════════════════════════════════════════════
# ⑩ Reddit Validation — harvester 命中 ≥ 2 次 = 真实需求
# ═══════════════════════════════════════════════════════════════════════════
# reddit_hits: 该关键词在 reddit harvester 的累计命中次数
#   0   → 未验证 (0)
#   1   → 单次出现 (+3)
#   2-3 → 真实需求 (+10)
#   4+  → 强需求 (+15)
def reddit_validation_score(reddit_hits: int) -> tuple[int, str]:
    h = int(reddit_hits or 0)
    if h >= 4:  return (15, f'reddit:strong({h})')
    if h >= 2:  return (10, f'reddit:validated({h})')
    if h == 1:  return (3, f'reddit:hint({h})')
    return (0, '')


# ═══════════════════════════════════════════════════════════════════════════
# Aggregator — 一把梭,返回 dict
# ═══════════════════════════════════════════════════════════════════════════

def score_brand_v2(row: dict) -> dict:
    """输入 recommendation row + 上下文字段,返回 v2 分数包.

    期望字段 (缺失 → 0 分):
      ad_count_now:          当前广告数
      ad_count_14d_ago:      14 天前快照 (从 pipeline.db history 取)
      variant_total:         creative variant 数
      ad_age_days:           品牌域名最长 ad_age
      brand_count_in_niche:  同 niche 品牌数
      angle_diversity:       0-1 之间, 角度多样性
      lp_html_tags:          LP 探针抓到的 HTML/tags 片段
      country_codes:         活跃国家 ISO 列表
      days_since_latest:     距离最新 creative 天数
      ad_text:               标题+caption 合并
      has_vertical_video:    True = 竖版视频
      likes:                 总点赞
      reddit_hits:           reddit harvester 累计命中次数
    """
    breakdown = {}
    tags = []

    for key, fn, args in [
        ('velocity',   velocity_score,  (row.get('ad_count_now'), row.get('ad_count_14d_ago'))),
        ('diversity',  creative_diversity_score, (row.get('variant_total', 0),)),
        ('age',        age_curve_score, (row.get('ad_age_days', 0),)),
        ('cluster',    cluster_score,   (row.get('brand_count_in_niche', 1), row.get('angle_diversity', 0.5))),
        ('geo',        geo_expansion_score, (row.get('country_codes') or [],)),
        ('fatigue',    fatigue_score,   (row.get('days_since_latest', 0),)),
        ('engagement', engagement_velocity_score, (row.get('likes', 0), row.get('ad_age_days', 0))),
        ('reddit',     reddit_validation_score, (row.get('reddit_hits', 0),)),
    ]:
        score, tag = fn(*args)
        breakdown[key] = score
        if tag:
            tags.append(tag)

    # cross-platform (需要两参数, 单独调)
    cp_score, cp_tag = cross_platform_score(row.get('ad_text', ''), row.get('has_vertical_video', False))
    breakdown['cross_platform'] = cp_score
    if cp_tag:
        tags.append(cp_tag)

    # lp_fingerprint — 优先用 lp_prober 缓存, fallback 到原 tags 字符串
    lp_cache = row.get('lp_cache')
    if lp_cache:
        lp_bonus, lp_tags, lp_penalty, reddit_b = lp_fingerprint_from_cache(lp_cache)
        breakdown['lp_fingerprint'] = lp_bonus
        breakdown['dropship_penalty'] = lp_penalty
        breakdown['reddit_signals'] = reddit_b
        tags.extend(lp_tags)
        # price-band 从 LP 抽取的 price_hint 自动打分
        pb_score, pb_tag = price_band_score(lp_cache.get('price_hint'))
        breakdown['price_band'] = pb_score
        if pb_tag: tags.append(pb_tag)
    else:
        lp_score, lp_tags = lp_fingerprint_score(row.get('lp_html_tags', ''))
        breakdown['lp_fingerprint'] = lp_score
        breakdown['dropship_penalty'] = 0
        breakdown['reddit_signals'] = 0
        breakdown['price_band'] = 0
        tags.extend(lp_tags)

    total_v2 = sum(breakdown.values())
    return {
        'v2_total': total_v2,
        'v2_breakdown': breakdown,
        'v2_tags': tags,
        'v2_tier': _tier_of(total_v2),
    }


def _tier_of(total: int) -> str:
    """把 v2 总分分档."""
    if total >= 80:  return 'S'    # 立即跟投
    if total >= 55:  return 'A'    # 强候选
    if total >= 30:  return 'B'    # 观察
    if total >= 10:  return 'C'    # 弱
    if total >= -10: return 'D'    # 不碰
    return 'F'                     # 主动避开


# ═══════════════════════════════════════════════════════════════════════════
# Export for /admin/insights-2026-v2
# ═══════════════════════════════════════════════════════════════════════════

def export_v2() -> dict:
    return {
        'version': '2026-Q2-v2',
        'doctrine': [
            '速度 > 绝对值: ad_count 14d 增长斜率是真 signal',
            'Andromeda 奖励素材矩阵品牌 (6-10 variants 为甜蜜区)',
            '45-120 天才是爆品甜蜜区, >365 天是衰退',
            '同 niche 2-3 品牌角度分化 = EMERGING, >10 = SATURATED',
            'LP 技术栈指纹 = 真 D2C vs dropship 的快速识别器',
            'Geo 扩张阶梯: US → Tier-1 英语 → EU Tier-1 → 全球',
            'Creative 疲劳 >60 天无新素材 = 老 winner 将死',
            'FB + TikTok 双验证 = ROAS 2x 起步',
            '前 48h 评论/分享速度是 Andromeda 激活阈值',
            'Reddit Pro harvester 命中 ≥ 2 = 真实需求, 非炒作',
        ],
        'tiers': {
            'S': '≥80  立即跟投 · 建议全量复制创意',
            'A': '55-79 强候选 · 90 min MVP 验证',
            'B': '30-54 观察 · 加入自扩队列',
            'C': '10-29 弱信号 · 暂缓',
            'D': '-10-9 不碰',
            'F': '<-10 主动避开 (saturated/dead)',
        },
        'signal_max_points': {
            'velocity': 25,
            'diversity': 15,
            'age_curve': 20,
            'cluster': 15,
            'lp_fingerprint': 30,
            'geo_expansion': 25,
            'fatigue': 5,
            'cross_platform': 10,
            'engagement': 20,
            'reddit_validation': 15,
        },
        'total_cap': 180,
    }

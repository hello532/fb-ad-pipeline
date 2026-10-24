"""pipeline/fb_fitness.py — FB Ads 适配性评分引擎.

综合 amz123_picker (6 维) + playbook (战术) + insights_2026 (2026 信号)
为一个 0-100 的 FB Ads 适配分, 告诉你"这个产品能不能在 FB 上赢".

8 维模型:
  impulse_price  0-15  价格在 FB 冲动消费甜区?
  visual_demo    0-15  能做 UGC 演示视频?
  pain_emotion   0-15  痛点/情绪够强能 hook?
  audience_match 0-10  FB 受众精度高不高?
  policy_risk    0/-15 FB 广告政策合规风险
  creative_sig   0-10  投放阶段信号 (testing→scaling→cash_cow)
  funnel_fit     0-10  落地页+CTA 适合 FB 转化?
  market_gap     0-10  竞争格局蓝海/红海?

bonus: rising_niche +12, tariff-proof +10, polarity-hook +5
"""
from __future__ import annotations

import json
from typing import Any

from core.logger import get_logger

log = get_logger(__name__)


# ── 维度 1: impulse_price (0-15) ────────────────────────────────────────────

def _impulse_price(price_main: float | None) -> int:
    """FB 冲动消费甜区 $25-80."""
    if not price_main:
        return 5  # 无价格数据给中间分
    p = float(price_main)
    if 25 <= p <= 80:
        return 15
    if (15 <= p < 25) or (80 < p <= 150):
        return 10
    if 8 <= p < 15:
        return 5
    if p > 150:
        return 5
    return 3


# ── 维度 2: visual_demo (0-15) ──────────────────────────────────────────────

# 重用 amz123_picker 词汇表 (import 而非重写)
try:
    from .amz123_picker import (
        VISUAL_HIGH, VISUAL_MED_BEFORE_AFTER, VISUAL_LOW_UTIL, VISUAL_COMMODITY,
    )
    _HAS_AMZ123 = True
except ImportError:
    _HAS_AMZ123 = False
    import re as _re
    VISUAL_HIGH = _re.compile(r'\b(cream|patches?|tool|device|gadget|massager|curler|cover|spray|trimmer|shaver|razor|brush|comb|peeler|chopper|slicer|grater|opener|clipper)\b', _re.I)
    VISUAL_MED_BEFORE_AFTER = _re.compile(r'\b(supplement|vitamin|powder|tablet|capsule|tincture|gummies|protein)\b', _re.I)
    VISUAL_LOW_UTIL = _re.compile(r'\b(sheet|organizer|rack|mat|pad|bin|drawer|hanger|holder|hook|shelf)\b', _re.I)
    VISUAL_COMMODITY = _re.compile(r'\b(paper|soap|detergent|sponge|wipes?|tissue|trash\s+bag|bleach)\b', _re.I)


def _visual_demo(keyword: str, title: str = '', has_video: bool = False) -> int:
    """可演示性 — 能不能拍 UGC 视频卖?"""
    blob = f"{keyword} {title}"
    if VISUAL_HIGH.search(blob):
        base = 13
    elif VISUAL_MED_BEFORE_AFTER.search(blob):
        base = 9
    elif VISUAL_LOW_UTIL.search(blob):
        base = 6
    elif VISUAL_COMMODITY.search(blob):
        base = 2
    else:
        base = 5
    # 有视频素材加成
    if has_video:
        base = min(base + 2, 15)
    return base


# ── 维度 3: pain_emotion (0-15) ─────────────────────────────────────────────

try:
    from .amz123_picker import PAIN_STRONG, DESIRE_VANITY, WELLNESS_2026, HOBBY_LIFESTYLE
except ImportError:
    import re as _re
    PAIN_STRONG = _re.compile(r'\b(relief|treatment|removal|fix|repair|cover|hide|conceal|stop\s+\w+|cure|soothe|heal|reduce\s+\w+)\b', _re.I)
    DESIRE_VANITY = _re.compile(r'\b(enhancement|anti[-\s]?wrinkle|slimming|lifting|firming|whitening|brightening|pheromone|seductive|glow|luminous|youth|tighten|sculpt)\b', _re.I)
    WELLNESS_2026 = _re.compile(r'\b(collagen|peptide|magnesium|cortisol|lymphatic|nmn|creatine|taurine|colostrum|probiotic|electrolyte|adaptogen|nootropic|ashwagandha)\b', _re.I)
    HOBBY_LIFESTYLE = _re.compile(r'\b(yoga|gaming|pilates|hiking|camping|fishing|knitting|crochet|painting|drawing)\b', _re.I)


def _pain_emotion(keyword: str, title: str = '', ad_body: str = '') -> int:
    """情绪触发强度 — 痛点/欲望够不够让用户停下拇指?"""
    blob = f"{keyword} {title} {ad_body}"
    if PAIN_STRONG.search(blob):
        return 15
    if DESIRE_VANITY.search(blob):
        return 15
    if WELLNESS_2026.search(blob):
        return 12
    if HOBBY_LIFESTYLE.search(blob):
        return 8
    # 退而用 text_analysis 的 pain keywords
    return 3


# ── 维度 4: audience_match (0-10) ───────────────────────────────────────────

try:
    from .amz123_picker import (
        TGT_BEAUTY_WOMEN, TGT_HOME, TGT_PET, TGT_MEN_GROOMING, TGT_FITNESS,
    )
except ImportError:
    import re as _re
    TGT_BEAUTY_WOMEN = _re.compile(r'\b(serum|cream|mask|brow|lash|hair|nail|polish|lipstick|foundation|shapewear|leggings|bra|skincare|wrinkle|menopause|hormone)\b', _re.I)
    TGT_HOME = _re.compile(r'\b(kitchen|organizer|storage|hanger|drawer|laundry|cleaning|vacuum|curtain|rug|pillow|blanket|towel|cookware|grill|oven|fryer)\b', _re.I)
    TGT_PET = _re.compile(r'\b(dog|cat|pet|puppy|kitten|aquarium|bird|hamster|rabbit|reptile|leash|collar|harness|litter|treat\s+dog|treat\s+cat)\b', _re.I)
    TGT_MEN_GROOMING = _re.compile(r'\b(beard|mens?\s+(grooming|skincare|hair)|trimmer|aftershave|cologne|testosterone|prostate|men.*health)\b', _re.I)
    TGT_FITNESS = _re.compile(r'\b(workout|gym|fitness|protein|creatine|dumbbell|kettlebell|yoga\s+mat|resistance|hiking|outdoor|camping|cycling|running\s+shoe)\b', _re.I)


def _audience_match(keyword: str, title: str = '', platforms: dict | None = None) -> int:
    """FB 受众精度 — 能不能精准定位目标人群?"""
    blob = f"{keyword} {title}"
    if TGT_BEAUTY_WOMEN.search(blob):
        return 10
    if TGT_HOME.search(blob):
        return 8
    if TGT_PET.search(blob):
        return 8
    if TGT_MEN_GROOMING.search(blob):
        return 7
    if TGT_FITNESS.search(blob):
        return 7
    # 多平台投放信号 (FB+IG = 精准)
    if platforms and isinstance(platforms, dict):
        if 'facebook' in platforms and 'instagram' in platforms:
            return 6
    return 3


# ── 维度 5: policy_risk (0 / -5 / -10 / -15) ───────────────────────────────

try:
    from .amz123_picker import POLICY_MEDICAL, POLICY_INGESTIBLE, POLICY_BEFORE_AFTER
except ImportError:
    import re as _re
    POLICY_MEDICAL = _re.compile(r'\b(cure|treat(?:ment)?|heal|diabetes|arthritis|eczema|psoriasis|fungus|infection|blood\s+pressure|anxiety|depression|adhd|autism|cancer|pain\s+relief|pain[-\s]?relief|migraine|edema|varicose|neuropathy)\b', _re.I)
    POLICY_INGESTIBLE = _re.compile(r'\b(supplement|vitamin|capsule|tablet|pill|gumm(?:y|ies)|powder|drops?|tincture|tea|detox|fat\s+burner|keto|probiotic|collagen|nmn)\b', _re.I)
    POLICY_BEFORE_AFTER = _re.compile(r'\b(before\s+and\s+after|before/after|lose\s+\d+|weight\s+loss|slimming|anti[-\s]?wrinkle|whitening|hair\s+growth|regrow)\b', _re.I)

try:
    from .insights_2026 import is_dead_keyword
    _HAS_DEAD = True
except ImportError:
    _HAS_DEAD = False
    def is_dead_keyword(kw: str) -> bool:
        return False


def _policy_risk(keyword: str, title: str = '', ad_body: str = '') -> tuple[int, list[str]]:
    """FB 广告政策合规风险 — 返回 (扣分 0/-5/-10/-15, 风险标签)."""
    blob = f"{keyword} {title} {ad_body}"
    penalty = 0
    tags: list[str] = []

    if POLICY_MEDICAL.search(blob):
        penalty += 10
        tags.append('policy-risk-medical')
    if POLICY_INGESTIBLE.search(blob):
        penalty += 5
        tags.append('policy-risk-ingestible')
    if POLICY_BEFORE_AFTER.search(blob):
        penalty += 5
        tags.append('policy-risk-before-after')

    if _HAS_DEAD and is_dead_keyword(keyword):
        penalty += 15
        tags.append('saturated-PENALTY')

    penalty = min(penalty, 15)
    return -penalty, tags


# ── 维度 6: creative_signal (0-10) ──────────────────────────────────────────

def _creative_signal(ads_count: int, ad_age_days: int) -> int:
    """投放阶段信号 — 从广告龄/量推断阶段."""
    a = ads_count or 0
    d = ad_age_days or 0
    if d == 0:
        return 0
    # scaling_hard: 大量 + 短龄 = 刚找到 winner
    if a >= 20 and d <= 60:
        return 10
    # proven_scaling: 已验证稳步扩
    if a >= 10 and d <= 120:
        return 8
    # cash_cow: 长期稳定
    if a >= 5 and d >= 180:
        return 8
    # testing: 测品中
    if a <= 3 and d <= 30:
        return 4
    # long_tail
    if a <= 3 and d >= 90:
        return 2
    # 中间地带
    return 5


# ── 维度 7: funnel_fit (0-10) ───────────────────────────────────────────────

try:
    from .playbook import landing_analysis, cta_analysis, CTA_INTENT
except ImportError:
    landing_analysis = None
    cta_analysis = None

import re as _re
_SHOPIFY_PATH = _re.compile(r'/(products?|collections?)/', _re.I)


def _funnel_fit(landing_url: str, cta_type: str = '') -> int:
    """落地页 + CTA 是否适合 FB 转化."""
    score = 3  # baseline

    # Shopify PDP = 优化过的转化路径
    if landing_url and _SHOPIFY_PATH.search(landing_url):
        score += 4
    elif landing_url and '.myshopify.com' in landing_url.lower():
        score += 2

    # CTA 意图
    cta = (cta_type or '').upper()
    if 'SHOP' in cta:
        score += 3
    elif 'OFFER' in cta or 'GET' in cta:
        score += 2
    elif 'WATCH' in cta:
        score += 3  # VSL 漏斗
    elif 'LEARN' in cta:
        score += 1

    # 用 playbook 的 landing_analysis 补充 (如果可 import)
    if landing_analysis and landing_url:
        try:
            la = landing_analysis(landing_url)
            if la.get('funnel_type') == 'PDP 单品页':
                score = max(score, 9)
            elif la.get('funnel_type') == '漏斗子域':
                score += 1
            tld_trust = la.get('tld_trust', 5)
            if tld_trust >= 8:
                score += 1
        except Exception:
            pass

    return min(score, 10)


# ── 维度 8: market_gap (0-10) ───────────────────────────────────────────────

try:
    from .playbook import market_heat
except ImportError:
    market_heat = None


def _market_gap(brand_count: int | None = None, keyword: str = '') -> int:
    """竞争格局蓝海/红海."""
    if brand_count is None:
        return 5  # 无数据给中间分
    if market_heat:
        try:
            heat = market_heat(brand_count)
            hs = heat.get('heat_score', 3)
            # heat_score 5=蓝海, 3=健康, 2=拥挤, 1=红海 → 映射到 0-10
            return hs * 2
        except Exception:
            pass
    # fallback
    if brand_count <= 3:
        return 10
    if brand_count <= 7:
        return 8
    if brand_count <= 15:
        return 5
    if brand_count <= 30:
        return 3
    return 1


# ── Bonus 叠加 (insights_2026) ───────────────────────────────────────────────

try:
    from .insights_2026 import insight_score, _TARIFF_PROOF_RE, _NEG_HOOK_RE
    _HAS_INSIGHTS = True
except ImportError:
    _HAS_INSIGHTS = False

    def insight_score(*a, **kw) -> tuple[int, list[str]]:
        return (0, [])

    import re as _re
    _TARIFF_PROOF_RE = _re.compile(r'\b(ships from us|made in usa|usa made|small batch|eu warehouse|fast us shipping|ethically sourced|us based)\b', _re.I)
    _NEG_HOOK_RE = _re.compile(r"\b(stop|don'?t|mistake|wrong|failed|never|warning|hate|worst|broke)\b", _re.I)


def _bonus(ad_title: str, ad_caption: str, landing_page: str,
           brand_name: str, industry_name: str) -> tuple[int, list[str]]:
    """Rising niche + tariff-proof + polarity-hook bonus."""
    if _HAS_INSIGHTS:
        return insight_score(ad_title, ad_caption, landing_page,
                             brand_name, industry_name)
    # fallback: 只做 tariff-proof + polarity
    bonus = 0
    tags: list[str] = []
    blob = f"{ad_title} {ad_caption} {landing_page}".lower()
    if _TARIFF_PROOF_RE.search(blob):
        bonus += 10
        tags.append('tariff-proof')
    if _NEG_HOOK_RE.search(ad_title or ''):
        bonus += 5
        tags.append('polarity-hook')
    return (bonus, tags)


# ═══════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════

def fb_fitness_score(comp: dict, keyword: str) -> tuple[int, dict, list[str]]:
    """返回 (fb_fitness 0-100, breakdown_dict, tag_list).

    comp 是 _corrected.json 里的一条 top_competitor,
    keyword 是扫描关键词.
    """
    all_tags: list[str] = []

    # 提取常用字段
    cs = comp.get('commercial_signals') or {}
    ta = comp.get('text_analysis') or {}
    price = cs.get('price_main')
    kw_lower = (keyword or '').lower()
    slug = comp.get('product_slug') or ''
    body_text = ' '.join(
        (comp.get('bodies') or [])[:3]  # 广告正文
    )
    landing_url = comp.get('landing_url') or ''
    cta = comp.get('cta_type') or comp.get('top_cta') or ''
    ads_count = int(comp.get('ads', 0) or 0)
    ad_age_days = int(comp.get('ad_age_days', 0) or 0)
    platforms = comp.get('platforms')
    brand_count = comp.get('brand_count')  # 有些 corrected 有, 有些没有
    ad_title = comp.get('title') or ''
    brand_name = comp.get('brand') or ''

    # 1. impulse_price
    impulse = _impulse_price(price)
    if price and 25 <= float(price) <= 80:
        all_tags.append('impulse-sweet-spot')

    # 2. visual_demo
    visual = _visual_demo(kw_lower, slug,
                          has_video=bool(comp.get('video_url') or comp.get('has_video')))

    # 3. pain_emotion — 优先用 text_analysis 的 pain keywords
    pain_kws = ta.get('top_pain_keywords') or []
    pain_blob = f"{kw_lower} {slug} {' '.join(str(k) for k in pain_kws[:5])}"
    pain = _pain_emotion(pain_blob, '', body_text)
    if pain >= 12:
        all_tags.append('strong-pain-hook')

    # 4. audience_match
    audience = _audience_match(kw_lower, slug, platforms)

    # 5. policy_risk
    risk, risk_tags = _policy_risk(kw_lower, slug, body_text)
    all_tags.extend(risk_tags)

    # 6. creative_signal
    creative = _creative_signal(ads_count, ad_age_days)

    # 7. funnel_fit
    funnel = _funnel_fit(landing_url, cta)

    # 8. market_gap
    gap = _market_gap(brand_count, kw_lower)

    # 基础分
    base = impulse + visual + pain + audience + risk + creative + funnel + gap

    # bonus
    bonus, bonus_tags = _bonus(
        ad_title,
        body_text[:500],
        landing_url,
        brand_name,
        comp.get('industry_name') or '',
    )
    all_tags.extend(bonus_tags)

    total = max(0, min(base + bonus, 100))

    breakdown = {
        'impulse_price': impulse,
        'visual_demo': visual,
        'pain_emotion': pain,
        'audience_match': audience,
        'policy_risk': risk,
        'creative_signal': creative,
        'funnel_fit': funnel,
        'market_gap': gap,
        'bonus': bonus,
        'base': base,
    }

    return total, breakdown, all_tags


def fb_fitness_label(score: int) -> str:
    """FB 适配性标签."""
    if score >= 75:
        return "🔥 FB 强适配"
    if score >= 60:
        return "✅ FB 可投"
    if score >= 40:
        return "⚠️ FB 需优化"
    return "❌ FB 不推荐"

"""pipeline/playbook.py — 不为人知的竞品分析打法.

编码 10+ 年 FB ads + dropshipping 老兵的暗线知识.
每条规则都映射到一个可计算的信号 + 可操作的策略.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


# ──────────────────────────────────────────────────────────────────
# 跟风危险期 (copycat risk)
# ──────────────────────────────────────────────────────────────────
def copycat_risk(ad_age_days: int) -> tuple[str, str, int]:
    """返回 (risk_level, 策略, 评分 0-5).
    0-30d: 蓝海 (早期验证, 立即跟进)
    30-60d: 黄金带 (已验证, 跟风刚开始)
    60-120d: 红海警告 (跟风高峰期, 差异化才有机会)
    120-180d: 生存者偏差 (活下来的真winner, 值得深耕)
    180d+: 稳定现金流 (但创意可能已疲劳)
    """
    a = ad_age_days or 0
    if a == 0:
        return ("未知", "需采集更多数据", 0)
    if a <= 30:
        return ("🟢 蓝海", "立即测品跟进, 首批红利", 5)
    if a <= 60:
        return ("🟢 黄金带", "已验证+跟风刚起, 最佳入场", 5)
    if a <= 120:
        return ("🟡 跟风红海", "差异化角度/本地化才有机会", 3)
    if a <= 180:
        return ("🔵 生存者", "真winner, 深耕+扩品", 4)
    return ("🔵 稳定现金流", "可跟但需新素材 (原创意疲劳)", 3)


# ──────────────────────────────────────────────────────────────────
# 落地页类型识别 (funnel pattern)
# ──────────────────────────────────────────────────────────────────
FUNNEL_PATTERNS = [
    (re.compile(r'/products?/', re.I), "PDP 单品页", "直营品牌, 转化链路优化过"),
    (re.compile(r'/collections?/', re.I), "集合页", "多 SKU 店, 需要深漏斗"),
    (re.compile(r'^(offers?|try|funnel|promo|landing|deal)\.', re.I), "漏斗子域", "高客单 + 优惠券 funnel"),
    (re.compile(r'\.myshopify\.com', re.I), "未自定义域名", "新手店, 刚起步"),
    (re.compile(r'clickfunnels|leadpages|unbounce', re.I), "第三方 LP", "联盟或高转化测试"),
]

TLD_TRUST = {
    '.com': 10, '.co': 8, '.shop': 7, '.store': 6, '.io': 6,
    '.net': 7, '.org': 7, '.us': 8, '.co.uk': 9, '.com.au': 9, '.de': 8,
    '.ca': 8, '.fr': 7,
}


def landing_analysis(landing_url: str) -> dict[str, Any]:
    """落地页类型 + TLD 信任度 + 子域暗示."""
    if not landing_url:
        return {}
    out: dict[str, Any] = {}
    try:
        p = urlparse(landing_url)
        host = (p.hostname or '').lower()
        path = p.path or ''

        # TLD 信任度
        tld_score = 5
        for tld, s in sorted(TLD_TRUST.items(), key=lambda x: -len(x[0])):
            if host.endswith(tld):
                tld_score = s
                out['tld'] = tld
                break
        out['tld_trust'] = tld_score

        # 漏斗模式
        for pat, label, hint in FUNNEL_PATTERNS:
            if pat.search(host) or pat.search(path):
                out['funnel_type'] = label
                out['funnel_hint'] = hint
                break

        # 子域前缀 (暗示漏斗/多品牌矩阵)
        parts = host.split('.')
        if len(parts) >= 3 and parts[0] in ('shop', 'store', 'offers', 'offer',
                                             'try', 'get', 'my', 'new', 'deal', 'promo',
                                             'go', 'cart'):
            out['subdomain_prefix'] = parts[0]
            out['root_domain'] = '.'.join(parts[1:])
    except Exception:
        pass
    return out


# ──────────────────────────────────────────────────────────────────
# CTA 漏斗意图 (funnel intent)
# ──────────────────────────────────────────────────────────────────
CTA_INTENT = {
    'SHOP_NOW': ('transactional', "直接电商转化", 5),
    'BUY_NOW': ('transactional', "硬转化, 可能高单价自信", 5),
    'ORDER_NOW': ('transactional', "直接下单", 5),
    'GET_OFFER': ('coupon', "优惠券漏斗, 价格敏感受众", 3),
    'SIGN_UP': ('lead_gen', "邮件列表漏斗, 内容营销", 2),
    'SUBSCRIBE': ('subscription', "订阅模式, LTV 玩法", 4),
    'LEARN_MORE': ('educational', "教育型漏斗, 可能有 quiz/free video", 3),
    'DOWNLOAD': ('lead_magnet', "白皮书/PDF lead magnet", 2),
    'APPLY_NOW': ('high_touch', "高客单 + 咨询式销售", 4),
    'CONTACT_US': ('high_touch', "B2B 或定制服务", 2),
    'BOOK_NOW': ('service', "预约服务类", 3),
    'GET_QUOTE': ('high_touch', "高单价询价", 3),
    'WATCH_MORE': ('video_funnel', "VSL 视频漏斗, 高转化", 5),
}


def cta_analysis(cta: str) -> dict[str, Any]:
    c = (cta or '').upper().strip()
    if c in CTA_INTENT:
        intent, hint, score = CTA_INTENT[c]
        return {'intent': intent, 'hint': hint, 'cta_score': score}
    return {}


# ──────────────────────────────────────────────────────────────────
# 创意活跃度 (creative fatigue / scaling signal)
# ──────────────────────────────────────────────────────────────────
def creative_signal(ads_count: int, ad_age_days: int) -> dict[str, Any]:
    """用广告数量和广告龄推断投放阶段."""
    a = ads_count or 0
    d = ad_age_days or 0
    out: dict[str, Any] = {}
    if d == 0:
        return {}
    # 活跃度 = 每个广告平均存活天数的倒数 × ads 数
    intensity = a / max(d / 7, 1)  # 每周平均活跃广告数
    out['weekly_ad_velocity'] = round(intensity, 2)
    if a >= 20 and d <= 60:
        out['stage'] = "scaling_hard"
        out['stage_hint'] = "规模化阶段, 刚找到 winner 全力扩量"
    elif a >= 10 and d <= 120:
        out['stage'] = "proven_scaling"
        out['stage_hint'] = "已验证, 稳步扩量中"
    elif a >= 5 and d >= 180:
        out['stage'] = "cash_cow"
        out['stage_hint'] = "印钞机, 持续有效"
    elif a <= 3 and d <= 30:
        out['stage'] = "testing"
        out['stage_hint'] = "测品阶段, 未必成功"
    elif a <= 3 and d >= 90:
        out['stage'] = "long_tail"
        out['stage_hint'] = "长尾运营, 不足为惧"
    return out


# ──────────────────────────────────────────────────────────────────
# 品牌命名风格 (naming archetype)
# ──────────────────────────────────────────────────────────────────
DROPSHIP_NAME_HINTS = re.compile(r'^[A-Z][a-z]{3,6}(ia|eo|ix|ox|oma|ara|yx|ynth|ura)$', re.I)
FOUNDER_NAME_HINTS = re.compile(r'\b(Dr\.?|MD|Ph\.?D|CEO|Founder)\b', re.I)


def brand_archetype(brand_name: str) -> dict[str, Any]:
    b = (brand_name or '').strip()
    if not b:
        return {}
    out: dict[str, Any] = {}
    if FOUNDER_NAME_HINTS.search(b):
        out['archetype'] = "背书型"
        out['archetype_hint'] = "个人品牌背书, 信任溢价"
    elif DROPSHIP_NAME_HINTS.match(b.split()[0] if ' ' in b else b):
        out['archetype'] = "现代 dropship"
        out['archetype_hint'] = "近年套路命名, 可能是中国白牌军"
    elif len(b.split()) == 1 and 4 <= len(b) <= 10:
        out['archetype'] = "Word-mark"
        out['archetype_hint'] = "专业品牌调性"
    elif any(w in b.lower() for w in ('shop', 'store', 'mart', 'online')):
        out['archetype'] = "泛店"
        out['archetype_hint'] = "通用网店, 没有品牌力"
    return out


# ──────────────────────────────────────────────────────────────────
# 平台组合战术意图
# ──────────────────────────────────────────────────────────────────
def platform_strategy(platforms) -> dict[str, Any]:
    if not platforms:
        return {}
    if isinstance(platforms, dict):
        plats = set(k.lower() for k in platforms.keys())
    else:
        plats = set(str(p).lower() for p in platforms)
    out: dict[str, Any] = {}
    has_fb = any('facebook' in p for p in plats)
    has_ig = any('instagram' in p for p in plats)
    has_msg = any('messenger' in p for p in plats)
    has_an = any('audience' in p for p in plats)
    if has_fb and has_ig and has_msg:
        out['strategy'] = "全漏斗"
        out['strategy_hint'] = "FB+IG+Messenger 做 chatbot/客服闭环"
    elif has_fb and has_ig and has_an:
        out['strategy'] = "规模化扩量"
        out['strategy_hint'] = "开 Audience Network 吃量, 已找到winner"
    elif has_fb and has_ig:
        out['strategy'] = "双平台标配"
        out['strategy_hint'] = "Meta 标准打法"
    elif has_ig and not has_fb:
        out['strategy'] = "IG only"
        out['strategy_hint'] = "偏品牌调性, 年轻受众"
    elif has_fb and not has_ig:
        out['strategy'] = "FB only"
        out['strategy_hint'] = "中老年受众或测试期"
    out['platform_count'] = len(plats)
    return out


# ──────────────────────────────────────────────────────────────────
# 市场饱和度 (competition saturation per keyword)
# ──────────────────────────────────────────────────────────────────
def market_heat(brand_count: int) -> dict[str, Any]:
    b = brand_count or 0
    if b == 0:
        return {}
    if b <= 3:
        return {'heat': "蓝海", 'heat_hint': f"{b}个品牌, 极度蓝海或小众", 'heat_score': 5}
    if b <= 7:
        return {'heat': "黄金带", 'heat_hint': f"{b}个品牌, 最佳入场 (已验证未红海)", 'heat_score': 5}
    if b <= 15:
        return {'heat': "健康竞争", 'heat_hint': f"{b}个品牌, 需差异化", 'heat_score': 3}
    if b <= 30:
        return {'heat': "拥挤", 'heat_hint': f"{b}个品牌, 硬刚 CPA 高", 'heat_score': 2}
    return {'heat': "红海", 'heat_hint': f"{b}个品牌, 不建议硬刚", 'heat_score': 1}


# ──────────────────────────────────────────────────────────────────
# 主入口: 对一条 recommendation 做全面战术分析
# ──────────────────────────────────────────────────────────────────
def analyze(rec: dict) -> dict[str, Any]:
    """返回 playbook 字段 (展开到 rec 上)."""
    out: dict[str, Any] = {}

    # 跟风风险
    risk, strategy, risk_score = copycat_risk(rec.get('ad_age_days', 0) or 0)
    out['copycat_risk'] = risk
    out['copycat_strategy'] = strategy
    out['copycat_score'] = risk_score

    # 落地页 & TLD
    out.update(landing_analysis(rec.get('landing_url', '')))

    # CTA 意图
    out.update(cta_analysis(rec.get('top_cta', '')))

    # 创意活跃度
    out.update(creative_signal(rec.get('ads_count', 0) or 0,
                                rec.get('ad_age_days', 0) or 0))

    # 品牌命名
    out.update(brand_archetype(rec.get('brand_name', '')))

    # 平台策略 — rec 里没有 platforms, 跳过 (可后续从 corrected 补)

    # 综合战术分 0-10 (结合上述所有)
    tactical = 0
    tactical += risk_score  # 0-5
    tactical += out.get('tld_trust', 5) // 2  # 0-5
    tactical += out.get('cta_score', 0)  # 0-5
    if out.get('stage') in ('scaling_hard', 'proven_scaling', 'cash_cow'):
        tactical += 3
    tactical = min(tactical, 10)
    out['tactical_score'] = tactical

    return out

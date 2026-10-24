"""pipeline/insights.py — 30 年选品师经验编码.

将美国顶级 dropshipper 的启发式规则编成可计算的 insights.
输出: (expert_score 0-10, insight_tags list[str], verdict str).

核心原则:
1. 解决痛点 > 功能展示
2. 价格感知价值差 3-5x
3. 广告龄 30-180d = 已验证未饱和 (最佳入场期)
4. 粉丝 5K-50K = 新兴品牌有成长空间 (> 100K = 红海)
5. 多平台投放 = 健康漏斗
6. VIDEO creative = 转化冠军
7. 直 Shopify PDP URL = 优化过的转化路径
8. 美国常青品类: 背痛 / 睡眠 / 护肤 / 宠物 / 家用健身
9. 季节性: Q4 礼品 / Q1 新年决心 / Q2 户外 / Q3 BTS
10. CTA "Shop Now" = 直接转化; "Get Offer" = 优惠券漏斗
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any


EVERGREEN_CATEGORIES = (
    'back pain', 'neck pain', 'sciatica', 'arthritis', 'sleep', 'snoring',
    'posture', 'massage', 'skincare', 'anti aging', 'hair loss',
    'pet', 'dog', 'cat', 'fitness', 'weight loss'
)
SEASONAL_HINTS = {
    (11, 12): 'Q4 礼品季 (节日高峰, 立即入场)',
    (1, 2): 'Q1 新年决心 (健身/护肤最佳)',
    (3, 4, 5): 'Q2 春夏户外 (运动/旅行)',
    (6, 7): '夏季高峰 (户外/美容)',
    (8, 9): 'BTS 返校 (家居/文具)',
}


def _age_verdict(age: int) -> tuple[int, str]:
    """广告龄洞见: 评分(0-3) + 文字."""
    if age <= 0:
        return 0, ""
    if age < 14:
        return 1, "太新·未验证"
    if age < 30:
        return 2, "验证中·观察"
    if age <= 180:
        return 3, "甜蜜期·入场最佳"
    if age <= 365:
        return 3, "长寿·真winner"
    return 2, "超老·抢滩太晚"


def _price_tier(price: float | None) -> tuple[int, str]:
    if not price:
        return 0, ""
    p = float(price)
    if 19 <= p <= 39:
        return 2, f"${p} 冲动消费区"
    if 40 <= p <= 79:
        return 3, f"${p} 主力客单带"
    if 80 <= p <= 149:
        return 3, f"${p} 高客单·需要funnel"
    if p >= 150:
        return 2, f"${p} 超高客单·小众"
    return 1, f"${p} 低价薄利"


def _brand_size(likes: int) -> tuple[int, str]:
    if likes <= 0:
        return 0, ""
    if likes < 1000:
        return 1, "品牌很新·高风险"
    if 5_000 <= likes <= 50_000:
        return 3, "新兴品牌·成长空间"
    if 50_000 < likes <= 200_000:
        return 2, "成熟品牌·仍有空间"
    if likes > 500_000:
        return 1, "红海·抢滩已晚"
    return 2, ""


def _platform_coverage(platforms: dict | list | None) -> tuple[int, str]:
    if not platforms:
        return 0, ""
    if isinstance(platforms, dict):
        plats = set(platforms.keys())
    else:
        plats = set(platforms)
    n = len(plats)
    if n >= 3:
        return 3, f"{n}平台·健康漏斗"
    if n == 2:
        return 2, "双平台"
    return 1, "单平台·实验中"


def _cta_verdict(cta: str) -> tuple[int, str]:
    cta = (cta or '').upper()
    if 'SHOP' in cta:
        return 2, "Shop Now·直接转化"
    if 'OFFER' in cta or 'GET' in cta:
        return 2, "优惠券漏斗"
    if 'LEARN' in cta:
        return 1, "Learn More·教育型"
    return 0, ""


def _seasonal() -> str:
    m = datetime.now().month
    for months, hint in SEASONAL_HINTS.items():
        if m in months:
            return hint
    return ""


def _evergreen(keyword: str, top_product: str) -> bool:
    txt = (keyword + ' ' + (top_product or '')).lower()
    return any(e in txt for e in EVERGREEN_CATEGORIES)


def compute_insights(rec: dict) -> dict[str, Any]:
    """返回 {expert_score, tags, verdict}.

    expert_score 0-10: 综合选品师判断.
    tags: 显示在 UI 的洞见标签.
    verdict: 一句话判断.
    """
    tags: list[str] = []
    score = 0

    age_s, age_t = _age_verdict(rec.get('ad_age_days', 0) or 0)
    score += age_s
    if age_t:
        tags.append(age_t)

    price_s, price_t = _price_tier(rec.get('price_main'))
    score += price_s
    if price_t:
        tags.append(price_t)

    # 可能拿不到 likes, 用 orbit_score 兜底代表品牌成熟度
    orbit = rec.get('orbit_score', 0) or 0
    if orbit >= 70:
        tags.append(f"轨道{orbit}·跨词验证")
        score += 2
    elif orbit >= 40:
        tags.append("成长期品牌")
        score += 1

    if rec.get('is_shopify_shape'):
        tags.append("Shopify PDP")
        score += 1

    if rec.get('has_urgency'):
        tags.append("稀缺营销")
        score += 1
    if rec.get('has_guarantee'):
        tags.append("风险消除")
        score += 1
    if rec.get('has_authority'):
        tags.append("权威背书")
        score += 1

    if rec.get('review_count') and rec['review_count'] >= 1000:
        tags.append(f"{rec['review_count']//1000}K+评论·社会证明")
        score += 1
    if rec.get('star_rating') and rec['star_rating'] >= 4.5:
        tags.append(f"{rec['star_rating']}★好评")

    # 常青品类加分
    if _evergreen(rec.get('keyword', ''), rec.get('top_product', '')):
        tags.append("常青品类")
        score += 1

    # FB fitness 维度
    fb = rec.get('fb_fitness', 0) or 0
    if fb >= 75:
        tags.append(f"FB强适配({fb})")
        score += 2
    elif fb >= 60:
        tags.append("FB中等适配")
        score += 1

    # FB policy 风险标签
    try:
        fb_tags_list = json.loads(rec.get('fb_tags') or '[]')
    except Exception:
        fb_tags_list = []
    if 'policy-risk-medical' in fb_tags_list:
        tags.append("FB医疗政策风险")
        score -= 1
    if 'saturated-PENALTY' in fb_tags_list:
        tags.append("饱和市场惩罚")
        score -= 2

    # 季节性提示
    season = _seasonal()
    if season:
        tags.append(season)

    # 判断
    score = min(score, 10)
    if score >= 8:
        verdict = "强烈推荐·立即测品"
    elif score >= 6:
        verdict = "推荐·加入候选池"
    elif score >= 4:
        verdict = "观察·需补充验证"
    else:
        verdict = "弱·跳过"

    return {
        'expert_score': score,
        'tags': tags,
        'verdict': verdict,
    }

"""pipeline/classifier.py — 自动类目识别 + 类目健康度.

核心: 把每个品牌/产品自动归入某个类目, 然后用类目基准衡量它的相对表现.
- 价格是否在类目甜蜜带?
- 广告龄是否匹配类目常见周期?
- 饱和度是否健康?
- 季节匹配度如何?

这样不用手工为每个产品写描述 — 类目规则自动套用.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .categories import load_categories

# 类目 → 健康基准 (可不写, 有默认 fallback)
CATEGORY_BENCHMARKS: dict[str, dict[str, Any]] = {
    'massage_wellness': {
        'price_sweet': (39, 129),       # 典型成交价区间
        'price_premium': (130, 299),    # 高客单带
        'age_proven': 60,                # 超过 60 天算验证过
        'saturation_safe': 15,          # 这个类目竞品 < 15 家算健康
        'season_peak': [1, 2, 11, 12],  # 新年决心 + 送礼季
        'advice': '刚需 + 高复购, Q4 送礼季是黄金窗口. 角度: 办公族 / 运动恢复 / 老年关节 3 条腿分开打.',
    },
    'beauty_devices': {
        'price_sweet': (79, 199),
        'price_premium': (200, 499),
        'age_proven': 45,
        'saturation_safe': 20,
        'season_peak': [1, 2, 11, 12],
        'advice': '女性主导, VSL (视频销售信) 转化最强. 必备 before/after + 临床数据背书. 节前(圣诞/母亲节)最猛.',
    },
    'home_fitness_premium': {
        'price_sweet': (149, 499),
        'price_premium': (500, 1500),
        'age_proven': 90,
        'saturation_safe': 12,
        'season_peak': [1, 2, 3],       # 新年决心
        'advice': '大件 → 需深度漏斗 (VSL + 分期付款). Q1 新年决心月最强. 强调"在家用"痛点.',
    },
    'sleep_recovery': {
        'price_sweet': (39, 99),
        'price_premium': (100, 249),
        'age_proven': 90,
        'saturation_safe': 15,
        'season_peak': [10, 11, 12, 1],
        'advice': '冬季睡眠障碍高峰, 强调"医生推荐/临床"+ 30 天试用. 主力中老年受众.',
    },
    'outdoor_premium': {
        'price_sweet': (199, 599),
        'price_premium': (600, 2000),
        'age_proven': 120,
        'saturation_safe': 10,
        'season_peak': [4, 5, 6, 7, 8],
        'advice': '季节性强 — Q2/Q3 是主场. 男性主导. 需要场景化视频 (露营/远足/EV 生活).',
    },
    'home_smart': {
        'price_sweet': (99, 299),
        'price_premium': (300, 699),
        'age_proven': 60,
        'saturation_safe': 18,
        'season_peak': [11, 12, 1],      # 礼品季 + Prime Day
        'advice': '便利性 + 时间节省是核心卖点. 需对比 Walmart/Target 展示差异化.',
    },
    'hair_loss': {
        'price_sweet': (99, 249),
        'price_premium': (250, 499),
        'age_proven': 180,               # 这类常青性强
        'saturation_safe': 8,
        'season_peak': list(range(1, 13)),  # 全年稳
        'advice': '男性羞耻痛点, FB 上常青品类 (10 年 winner). 必须前后对比 + 医生/临床背书.',
    },
    'ergonomic_office': {
        'price_sweet': (149, 399),
        'price_premium': (400, 999),
        'age_proven': 90,
        'saturation_safe': 12,
        'season_peak': [1, 9],           # 新年 + 返校/返工
        'advice': 'B2C + B2B 双通. 强调"背痛/颈痛" + 员工福利角度.',
    },
    'kitchen_pro': {
        'price_sweet': (79, 199),
        'price_premium': (200, 399),
        'age_proven': 60,
        'saturation_safe': 20,
        'season_peak': [10, 11, 12],
        'advice': '礼品季主场. 食物演示视频 (ASMR) 爆款. 主力女性受众 25-55.',
    },
    'pet_premium': {
        'price_sweet': (49, 129),
        'price_premium': (130, 299),
        'age_proven': 90,
        'saturation_safe': 18,
        'season_peak': [11, 12],
        'advice': '宠物主情感钩子最强 — "为爱犬做最好的". 可爱动物视频天然转化.',
    },
    'audio_video': {
        'price_sweet': (79, 249),
        'price_premium': (250, 599),
        'age_proven': 60,
        'saturation_safe': 20,
        'season_peak': [11, 12],
        'advice': '礼品季 + 男性受众. 强调声音体验 + 便携.',
    },
    'baby_premium': {
        'price_sweet': (79, 199),
        'price_premium': (200, 499),
        'age_proven': 120,
        'saturation_safe': 12,
        'season_peak': list(range(1, 13)),
        'advice': '新手妈妈焦虑 = 购买力. 全年常青. 必须强调安全/FDA/医生推荐.',
    },
    'default': {
        'price_sweet': (29, 99),
        'price_premium': (100, 299),
        'age_proven': 60,
        'saturation_safe': 15,
        'season_peak': [11, 12],
        'advice': '通用品类: 先看产品功能独特性, 再做卖点.',
    },
}


def _build_keyword_to_category() -> dict[str, str]:
    """从 categories.json 的 seeds 建立关键词 → 类目映射."""
    out = {}
    cfg = load_categories()
    for cat in cfg.get('categories', []):
        cid = cat.get('id', '')
        for seed in cat.get('seeds', []):
            out[seed.lower().strip()] = cid
    return out


_KW_MAP: dict[str, str] = {}


def _get_kw_map() -> dict[str, str]:
    global _KW_MAP
    if not _KW_MAP:
        _KW_MAP = _build_keyword_to_category()
    return _KW_MAP


def classify(rec: dict) -> dict[str, Any]:
    """返回 {category_id, category_name, confidence, ...benchmarks}."""
    kw_map = _get_kw_map()
    keyword = (rec.get('keyword') or '').lower()
    product = (rec.get('top_product') or '').lower()
    pain = (rec.get('top_pain') or '').lower()
    hay = f"{keyword} {product} {pain}"

    # 1) 精确匹配
    cat_id = kw_map.get(keyword.strip())
    confidence = 1.0 if cat_id else 0.0

    # 2) 模糊 — seed 中任一词出现在 hay
    if not cat_id:
        best = None
        best_hits = 0
        for seed, cid in kw_map.items():
            hits = sum(1 for w in seed.split() if len(w) > 3 and w in hay)
            if hits > best_hits:
                best_hits = hits
                best = cid
        if best and best_hits >= 1:
            cat_id = best
            confidence = min(1.0, best_hits / 3)

    # 3) 关键词直接匹配类目关键字
    if not cat_id:
        if any(w in hay for w in ('massage', 'massager', 'fascia', 'tens', 'percussion', 'muscle', 'sciatica', 'back pain', 'neck', 'shoulder')):
            cat_id, confidence = 'massage_wellness', 0.6
        elif any(w in hay for w in ('skin', 'facial', 'beauty', 'anti aging', 'hair removal', 'led face', 'red light')):
            cat_id, confidence = 'beauty_devices', 0.6
        elif any(w in hay for w in ('treadmill', 'dumbbell', 'rowing', 'pilates', 'vibration plate')):
            cat_id, confidence = 'home_fitness_premium', 0.6
        elif any(w in hay for w in ('pillow', 'mattress', 'sleep', 'posture', 'snoring', 'blanket')):
            cat_id, confidence = 'sleep_recovery', 0.6
        elif any(w in hay for w in ('pet', 'dog', 'cat', 'litter')):
            cat_id, confidence = 'pet_premium', 0.6
        elif any(w in hay for w in ('hair growth', 'hair loss', 'scalp', 'derma roller')):
            cat_id, confidence = 'hair_loss', 0.7

    if not cat_id:
        cat_id = 'default'
    bench = CATEGORY_BENCHMARKS.get(cat_id, CATEGORY_BENCHMARKS['default'])
    cat_name = _category_name(cat_id) if cat_id != 'default' else '未分类(自动)'

    # 类目健康度判断
    price = rec.get('price_main')
    age = rec.get('ad_age_days', 0) or 0

    price_fit = ''
    price_hint = ''
    if price:
        sweet_lo, sweet_hi = bench['price_sweet']
        prem_lo, prem_hi = bench['price_premium']
        if sweet_lo <= price <= sweet_hi:
            price_fit = 'sweet'
            price_hint = f'在类目甜蜜带 ${sweet_lo}-{sweet_hi}'
        elif prem_lo <= price <= prem_hi:
            price_fit = 'premium'
            price_hint = f'高客单带 ${prem_lo}-{prem_hi}'
        elif price < sweet_lo:
            price_fit = 'low'
            price_hint = f'低于类目甜蜜带 (${sweet_lo}+), 利润薄'
        else:
            price_fit = 'high'
            price_hint = f'超出类目常见区间 (>${prem_hi}), 小众'

    age_fit = ''
    if age >= bench['age_proven'] * 2:
        age_fit = 'mature'
    elif age >= bench['age_proven']:
        age_fit = 'proven'
    elif age >= 14:
        age_fit = 'testing'
    elif age > 0:
        age_fit = 'new'

    # 季节匹配
    now_m = datetime.now().month
    season_peak = bench.get('season_peak', [])
    in_season = now_m in season_peak

    return {
        'category_id': cat_id or 'default',
        'category_name': cat_name,
        'category_confidence': round(confidence, 2),
        'category_advice': bench['advice'],
        'category_price_sweet': bench['price_sweet'],
        'category_price_premium': bench['price_premium'],
        'price_fit': price_fit,
        'price_fit_hint': price_hint,
        'age_fit': age_fit,
        'in_season': in_season,
        'saturation_safe_count': bench['saturation_safe'],
    }


def _category_name(cid: str) -> str:
    for cat in load_categories().get('categories', []):
        if cat.get('id') == cid:
            return cat.get('name', cid)
    return cid or '未分类'

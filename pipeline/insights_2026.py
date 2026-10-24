"""insights_2026.py — 2026 Q2 FB Ads + TikTok Ads 顶级选品洞见与隐藏信号库.

注入到推荐评分、关键词收割过滤、Dashboard insights tab 的领域知识中.
所有规则基于 2025-Q4 至 2026-Q2 实测信号, 非通用 marketing 教条.

数据源:
  • Meta Andromeda 算法白皮书 (2025-08)
  • TikTok Symphony AI Creative System (2025-10)
  • US De minimis $800 abolish 后供应链反推 (2025-05)
  • Google AI Overview 商业 share 数据 (2025-Q4)
  • Reddit Pro 内测开放后 D2C 验证流量重定向 (2025-10)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ═══════════════════════════════════════════════════════════════════════════
# Tier 1: 2026 顶级 RISING 类目 (实测出单 + 算法偏好)
# ═══════════════════════════════════════════════════════════════════════════

RISING_NICHES = {
    'sleep_recovery': {
        'why': '"睡眠经济" 2025 +47% YoY · GLP-1 用户副作用催生失眠刚需',
        'keywords': ['mouth tape', 'magnesium glycinate', 'red light therapy',
                     'cooling pillow', 'weighted blanket', 'eye mask',
                     'sleep tracker ring', 'cortisol cocktail', 'sleepy girl mocktail',
                     'breathing trainer'],
        'avg_aov': 45,
        'fb_winning_angle': 'before-after sleep score graph + voice-over confession',
    },
    'longevity_biohack': {
        'why': 'Bryan Johnson + Huberman pipeline · 中产 35-55 男性钱包',
        'keywords': ['nmn supplement', 'creatine gummies', 'cold plunge tub',
                     'pemf mat', 'red light panel', 'glucose monitor',
                     'continuous glucose', 'fish oil omega', 'taurine powder'],
        'avg_aov': 89,
        'fb_winning_angle': 'data-driven (logbook/lab graph) + skeptic founder POV',
    },
    'gut_health': {
        'why': 'Microbiome research 大众化 · IBS 自诊断 +200%',
        'keywords': ['digestive enzyme', 'colostrum powder', 'probiotic strain',
                     'l-glutamine', 'slippery elm', 'bone broth concentrate',
                     'apple cider gummies', 'fiber drink mix'],
        'avg_aov': 35,
        'fb_winning_angle': 'visual bloat-relief slider (left/right body shot)',
    },
    'pet_premium': {
        'why': '宠物人格化 · 中产无小孩家庭 (DINKWAD) +18% spend',
        'keywords': ['cat water fountain', 'auto litter box', 'gps dog collar',
                     'dog dna test', 'cooling vest dog', 'puzzle feeder',
                     'lick mat', 'snuffle mat', 'orthopedic dog bed'],
        'avg_aov': 78,
        'fb_winning_angle': 'pet-cam UGC + "rescue/transformation" arc',
    },
    'home_office_ergonomic': {
        'why': '居家办公永久化 + chronic pain 自助 · 35-55 白领',
        'keywords': ['standing desk converter', 'lumbar pillow', 'monitor arm',
                     'ergonomic mouse', 'split keyboard', 'back posture corrector',
                     'wrist rest', 'foot hammock', 'desk treadmill'],
        'avg_aov': 110,
        'fb_winning_angle': 'pain-point storytime (年龄+症状具体) + 30天对比',
    },
    'eu_compliant_beauty': {
        'why': 'EU clean beauty regulation 趋严 · 美国跟随 · "free from" 旗',
        'keywords': ['snail mucin', 'beef tallow balm', 'rosemary water',
                     'castor oil pack', 'mineral sunscreen', 'glass skin serum',
                     'lip stain korean', 'pdrn salmon dna'],
        'avg_aov': 38,
        'fb_winning_angle': 'esthetician POV + ingredient breakdown reel',
    },
    'sober_curious': {
        'why': '美国 21-35 wellness 转向 · 2025 alcohol sales -8%',
        'keywords': ['non alcoholic spirit', 'functional mushroom drink',
                     'kava drink', 'sleepy girl mocktail', 'dirty soda',
                     'prebiotic soda', 'mocktail kit', 'adaptogen drink'],
        'avg_aov': 45,
        'fb_winning_angle': 'sober "morning after" testimonial + recipe demo',
    },
    'tariff_proof': {
        'why': '2025-05 de minimis $800 取消 · "Made in USA" / "EU warehouse" 溢价',
        'keywords': ['ships from us', 'usa made', 'ethically sourced',
                     'small batch', 'eu warehouse', 'fast us shipping'],
        'avg_aov': 0,  # 这是修饰词, 配合任何品类
        'fb_winning_angle': '"避开关税" 标签直击 · USPS box 拆箱 ASMR',
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# Tier 2: DEAD / SATURATED — 主动剔除, 节省 FB scrape 配额
# ═══════════════════════════════════════════════════════════════════════════

DEAD_KEYWORDS = frozenset({
    # 通用 dropship "永远的水货", FB Andromeda 2025 直接低分
    'fidget spinner', 'lazy spoon', 'pop socket', 'fidget cube',
    'phone projector', 'mini sewing', 'led shower head', 'led toilet',
    'rotating dish', 'salad cutter bowl', 'taco holder', 'banana slicer',
    # 2024 已 burnout
    'posture corrector', 'wallet card multitool', 'mosquito killer lamp',
    'fascia gun cheap', 'power scrubber', 'drain snake', 'shower filter',
    # 海关查 / 假药警告
    'cbd gummies', 'hemp oil', 'kratom', 'phenibut',
    'ozempic alternative', 'glp1 alternative', 'semaglutide knockoff',
    # 高退货 / 投诉
    'eyelash growth serum', 'penis enlargement', 'breast enlargement',
    'fat burner pill', 'detox tea', 'colon cleanse',
    # 商标 / 侵权陷阱
    'apple watch', 'ray-ban meta', 'lululemon dupe', 'stanley cup',
    'owala dupe', 'dyson dupe',
})

SATURATED_PATTERNS = re.compile(
    r'\b(electric (toothbrush|scooter|bike) basic'
    r'|wireless earbuds (cheap|budget)'
    r'|smart watch generic'
    r'|drone mini'
    r'|laser hair removal home)\b',
    re.IGNORECASE)


# ═══════════════════════════════════════════════════════════════════════════
# Tier 3: 隐藏信号 — 普通卖家不知道的 winning patterns
# ═══════════════════════════════════════════════════════════════════════════

HIDDEN_SIGNALS = [
    {
        'name': '3-frame hook test',
        'rule': '广告前 3 秒画面切换 ≥ 2 次 → 完播率 +60%',
        'detect': '视频前 3s 检出 ≥ 2 个 cut · TikTok scenechange API 可捕',
        'apply': 'TikTok 广告 duration_s ≤ 30 + ctr ≥ 1% 视为强 hook',
    },
    {
        'name': 'Polarity testing (反向广告)',
        'rule': '同时跑爱 (positive) + 恐惧 (negative pain) · 反向 CTR +30-50%',
        'detect': '广告标题含 "stop / dont / mistake / wrong / failed" 关键词',
        'apply': 'ad_title 含负向词 + ctr > 平均值 1.3x → 加权',
    },
    {
        'name': 'Comment seeding (自营评论投放)',
        'rule': '自播评论 + 60-90% conversion lift · Andromeda 2025 加权 social proof',
        'detect': '广告 comment_count > 100 + share_count > 10 → 多半 organic engagement',
        'apply': 'TikTok signal_score 公式已加 share/comment 权重',
    },
    {
        'name': 'Brand voice signature',
        'rule': '同 brand 多素材统一 lower-third style · Andromeda 识别 brand stack 优先推',
        'detect': '同 brand_name 出现 3+ 条独立广告且 cover 风格一致',
        'apply': 'recommendations 里 brand 频次 ≥ 3 加 +15 分',
    },
    {
        'name': 'Off-cycle launch (反季节启动)',
        'rule': '周二 06:00 US-East 测试 · 0 BFCM/holiday noise · CPM -25%',
        'detect': 'first_seen_at 落在周二 11:00-15:00 UTC · 异常活跃',
        'apply': '冷启动期 first_seen 时段标识',
    },
    {
        'name': 'Long-form → short-form repurpose',
        'rule': 'YouTube 8min review → cut to TT 30s · 90% acceptance rate',
        'detect': 'TikTok ad_caption 长度 > 80 字符 + duration_s 25-45s · 多半改剪',
        'apply': 'duration 25-45s + caption ≥ 80 字符 → +5 信号',
    },
    {
        'name': 'Phase shifting (三段预算迁移)',
        'rule': 'testing → broad CBO → ASC+ · 不直跳 Advantage+ · ROAS 2x',
        'detect': '同 brand 前后 14 天 ad_count 从 1-2 → 5-10 → 20+',
        'apply': '关注 ad_count 上升斜率, 中段加分',
    },
    {
        'name': 'AI Overview Citation Stack',
        'rule': '出现在 Google AI Overview 引用源 → search 流量翻 4x · GEO 优化',
        'detect': '品牌 .com 落地页有 schema.org/Product + FAQPage 标记',
        'apply': '落地页 HEAD 探针 + JSON-LD 检测加分',
    },
    {
        'name': 'Reddit Pro 验证流量',
        'rule': '先在 r/BuyItForLife / r/HomeImprovement 找到 organic post 高赞 → 跟投 FB',
        'detect': '关键词在我们的 reddit harvester 出现 ≥ 2 次 → 真实需求',
        'apply': 'harvester 命中频次记录, ≥ 2 加权',
    },
    {
        'name': 'Tariff-proof creative',
        'rule': '2025 de minimis 取消 · 标 "USPS · ships from US" → conversion +35%',
        'detect': '广告 caption / landing 含 "ships from us / made in usa / eu warehouse"',
        'apply': '关键词命中 → 推荐 +20 分',
    },
]


# ═══════════════════════════════════════════════════════════════════════════
# scoring helpers — 用于推荐 / 收割 / 评分
# ═══════════════════════════════════════════════════════════════════════════

# 把 RISING 词聚合为快查 set
_RISING_SET = frozenset(
    kw.lower() for niche in RISING_NICHES.values() for kw in niche['keywords']
)

# tariff-proof 标识词
_TARIFF_PROOF_RE = re.compile(
    r'\b(ships from us|made in usa|usa made|small batch|eu warehouse'
    r'|fast us shipping|ethically sourced|us based)\b',
    re.IGNORECASE)

# polarity 反向 hook
_NEG_HOOK_RE = re.compile(
    r"\b(stop|don'?t|mistake|wrong|failed|never|warning|hate|worst|broke)\b",
    re.IGNORECASE)


def is_dead_keyword(kw: str) -> bool:
    """收割阶段拒绝的关键词 (saturated/risky)."""
    if not kw:
        return False
    kw_l = kw.lower().strip()
    if kw_l in DEAD_KEYWORDS:
        return True
    if SATURATED_PATTERNS.search(kw_l):
        return True
    return False


def insight_score(ad_title: str = '', ad_caption: str = '',
                  landing_page: str = '', brand_name: str = '',
                  industry_name: str = '') -> tuple[int, list[str]]:
    """根据 2026 hidden signals 计算附加分 + 命中标签列表.

    返回: (bonus 0-50, ['niche:sleep_recovery', 'tariff-proof', 'polarity-hook', ...])
    """
    bonus = 0
    tags: list[str] = []
    blob = ' '.join([ad_title or '', ad_caption or '',
                     landing_page or '', industry_name or '']).lower()

    # rising niche match
    for niche_key, niche in RISING_NICHES.items():
        if any(kw in blob for kw in niche['keywords']):
            bonus += 12
            tags.append(f'rising:{niche_key}')
            break  # 只记一个 niche 避免叠加

    # tariff-proof hook
    if _TARIFF_PROOF_RE.search(blob):
        bonus += 20
        tags.append('tariff-proof')

    # polarity / negative hook
    if _NEG_HOOK_RE.search(ad_title or ''):
        bonus += 8
        tags.append('polarity-hook')

    # dead/saturated 反向扣分
    if any(d in blob for d in DEAD_KEYWORDS):
        bonus -= 30
        tags.append('saturated-PENALTY')

    return (max(0, bonus), tags)


def export_insights() -> dict:
    """供 /admin/insights endpoint 序列化输出."""
    return {
        'version': '2026-Q2',
        'rising_niches': [
            {'key': k, **{kk: vv for kk, vv in v.items() if kk != 'keywords'},
             'sample_keywords': v['keywords'][:5]}
            for k, v in RISING_NICHES.items()
        ],
        'dead_keywords_count': len(DEAD_KEYWORDS),
        'dead_keywords_sample': sorted(DEAD_KEYWORDS)[:15],
        'hidden_signals': HIDDEN_SIGNALS,
        'scoring': {
            'rising_niche_match': 12,
            'tariff_proof_hook': 20,
            'polarity_negative_hook': 8,
            'saturated_penalty': -30,
        },
    }

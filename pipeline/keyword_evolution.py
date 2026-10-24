"""
pipeline/keyword_evolution.py — 关键词智能自动进化 (V2: 5 层兔子洞扩展)

兔子洞 5 层推演:
  L1 种子词 (seed)         → 初始高分词, 如 "knee pain"
  L2 属性层 (attribute)    → 附加程度/状态修饰, 如 "chronic knee pain", "severe knee pain"
  L3 场景层 (scene)        → 发生语境, 如 "knee pain when walking", "knee pain after running"
  L4 人群层 (demographic)  → 受众画像, 如 "knee pain seniors", "knee pain athletes"
  L5 痛点根因 (condition)  → 深层病因/并发, 如 "knee arthritis", "meniscus tear"

四个能力 (仍保留):
  harvest  — 从高分推荐中学习新关键词 (seed 候选)
  expand   — 对每个 seed 做 5 层兔子洞扩展 (替代原朴素 cartesian)
  retire   — 淘汰陈旧低产关键词
  cluster  — 品类自动发现

外部种子抓取:
  fetch_external_suggestions — Amazon 下拉 + Google autocomplete 反向挖长尾
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.parse
import urllib.error
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from core.logger import get_logger
log = get_logger(__name__)


# ── 基础工具 ─────────────────────────────────────────────────────────────────

_STOPWORDS = {
    'the','a','an','for','and','or','of','in','to','with','on','at','by',
    'from','up','out','off','over','under','this','that','these','those',
    'is','are','was','were','be','been','being','have','has','had','do',
    'does','did','will','would','should','could','may','might','must',
    'your','my','our','their','his','her','its','all','new','best','top',
    'one','two','three','you','we','i','me','us','them','they','he','she',
    'it','more','most','some','any','each','every','no','not','can','also',
}


def _tokenize(text: str) -> list[str]:
    if not text: return []
    text = text.lower()
    text = re.sub(r'[^a-z0-9\s-]', ' ', text)
    tokens = text.split()
    return [t.strip('-') for t in tokens if len(t) >= 3 and t not in _STOPWORDS]


def _ngrams(tokens: list[str], n_min: int = 1, n_max: int = 3) -> list[str]:
    out = []
    for n in range(n_min, n_max + 1):
        for i in range(len(tokens) - n + 1):
            gram = ' '.join(tokens[i:i+n])
            if len(gram) >= 4:
                out.append(gram)
    return out


# ── 兔子洞 10 层词典 (V3: 5 基础 + 5 个 2026 新维度) ────────────────────────

# L2 属性层: 程度/频次/病程修饰
ATTRIBUTE_MODIFIERS = [
    'chronic', 'acute', 'severe', 'mild', 'sharp', 'dull', 'throbbing',
    'persistent', 'sudden', 'constant', 'intermittent', 'bilateral',
    'radiating', 'worsening', 'lingering',
]

# L3 场景层: 发生语境 (动作 / 时间 / 状态)
SCENE_PATTERNS = [
    'when walking', 'when bending', 'when sitting', 'when standing',
    'when lying down', 'when sleeping', 'when running', 'when driving',
    'when climbing stairs', 'when exercising', 'after workout',
    'after running', 'after sitting', 'after sleeping', 'at night',
    'in the morning', 'during pregnancy', 'at work', 'at home',
    'while typing', 'while walking', 'in cold weather',
]

# L4 人群层: 受众画像 (年龄 / 职业 / 状态)
DEMOGRAPHICS = [
    'seniors', 'elderly', 'women', 'men', 'runners', 'athletes',
    'desk workers', 'office workers', 'nurses', 'drivers', 'pregnant women',
    'new mothers', 'teens', 'teenagers', 'golfers', 'gamers',
    'remote workers', 'for women over 40', 'for women over 50',
    'for men over 40', 'post-surgery', 'post-partum',
]

# L5 痛点根因层: 具体并发症/条件
BODY_TO_CONDITIONS = {
    'knee':     ['arthritis', 'meniscus tear', 'ACL injury', 'runners knee',
                 'patellar tendinitis', 'bursitis', 'baker cyst', 'IT band syndrome'],
    'back':     ['sciatica', 'herniated disc', 'spinal stenosis', 'scoliosis',
                 'muscle spasm', 'degenerative disc', 'pinched nerve', 'si joint pain'],
    'neck':     ['herniated disc', 'cervical spondylosis', 'whiplash',
                 'tension headache', 'pinched nerve', 'text neck', 'stiff neck'],
    'shoulder': ['rotator cuff', 'frozen shoulder', 'impingement', 'bursitis',
                 'labral tear', 'AC joint sprain'],
    'foot':     ['plantar fasciitis', 'neuropathy', 'flat feet', 'bunion',
                 'heel spur', 'morton neuroma', 'hammer toe'],
    'hand':     ['carpal tunnel', 'arthritis', 'trigger finger', 'Dupuytren',
                 'tendinitis', 'ganglion cyst'],
    'wrist':    ['carpal tunnel', 'tendinitis', 'RSI', 'ganglion cyst',
                 'DeQuervain', 'sprain'],
    'hip':      ['bursitis', 'arthritis', 'labral tear', 'IT band syndrome',
                 'piriformis', 'tendinitis'],
    'leg':      ['restless leg', 'cramps', 'varicose veins', 'DVT',
                 'shin splints', 'neuropathy'],
    'elbow':    ['tennis elbow', 'golfer elbow', 'bursitis', 'tendinitis'],
    'jaw':      ['TMJ', 'bruxism', 'lockjaw'],
    'eye':      ['eye strain', 'dry eye', 'blepharitis'],
    'ankle':    ['sprain', 'Achilles tendinitis', 'instability'],
}
BODY_PARTS_TOKEN = set(BODY_TO_CONDITIONS.keys())

# ── 2026 新增维度 ─────────────────────────────────────────────────────────────

# L6 语音/问句层: 对话式搜索 (Siri/Alexa/ChatGPT era, Gen Z 提问式搜索)
VOICE_TEMPLATES = [
    'how to relieve {s}', 'how to fix {s}', 'how to stop {s}',
    'how do i treat {s}', 'why do i have {s}', 'what causes {s}',
    'what is the best product for {s}', 'best treatment for {s}',
    'can {s} be cured', 'should i see a doctor for {s}',
    'when to worry about {s}', 'is {s} serious',
]

# L7 短视频钩子层: TikTok/Reels/Shorts 2026 流量词
TIKTOK_HOOKS = [
    'tiktok made me buy {s} fix', 'pov: you have {s}',
    '{s} hack', '{s} secret', 'the reason i have {s}',
    'if you have {s} watch this', '{s} that actually works',
    'game changer for {s}', 'life changing {s} fix',
    '{s} nobody talks about', 'underrated {s} remedy',
]

# L8 情绪/身份层: 2026 趋势 — 从功能转向身份 / 情绪 / 社群认同
IDENTITY_MODIFIERS = [
    'healing from {s}', '{s} recovery journey', 'self care for {s}',
    'feel better with {s}', 'confidence after {s}', 'living with {s}',
    '{s} girlie', '{s} warrior', 'overcoming {s}',
    'mindful relief from {s}',
]

# L9 2026 健康新概念: 神经系统/迷走神经/冷热疗/昼夜节律/长寿生活方式
WELLNESS_2026 = [
    'vagus nerve', 'nervous system regulation', 'somatic therapy',
    'lymph drainage', 'fascia release', 'cold plunge',
    'red light therapy', 'grounding mat', 'earthing',
    'circadian rhythm', 'zone 2 cardio', 'mitochondria',
    'autophagy fasting', 'longevity protocol', 'blue zone',
    'breathwork', 'sound healing', 'infrared sauna',
    'glp-1 alternative', 'gut brain axis', 'cortisol reset',
    'magnesium glycinate', 'creatine for women', 'microdose',
]

# L10 清洁/成分/伦理层: 2026 消费者对原料透明度的高要求
CLEAN_MODIFIERS = [
    'clean', 'non-toxic', 'fragrance-free', 'paraben-free',
    'sulfate-free', 'plant-based', 'reef safe', 'biodegradable',
    'eco-friendly', 'sustainable', 'cruelty-free', 'vegan',
    'refillable', 'zero waste', 'small batch', 'dermatologist-tested',
    'pcos-friendly', 'hormone-safe', 'pregnancy-safe',
]

# L11 结果/时限承诺: 2026 消费者要快见效
TIMEBOX_PATTERNS = [
    '{s} in 7 days', '{s} in 30 days', 'overnight {s} relief',
    'instant {s}', 'quick {s} fix', '{s} before 40',
    'fast {s} solution', '{s} results in weeks',
]


def rabbit_hole_expand(seed: str, layers: tuple = None) -> dict:
    """
    兔子洞 10 层扩展 (V3, 2026 增强版).
    默认展开全部层. layers=('L2','L3','L4','L5') 则退回基础 5 层.
    """
    seed = (seed or '').strip().lower()
    if not seed:
        return {}
    if layers is None:
        layers = ('L2','L3','L4','L5','L6','L7','L8','L9','L10','L11')

    tokens = _tokenize(seed)
    body = next((t for t in tokens if t in BODY_PARTS_TOKEN), None)

    out: dict[str, list[str]] = {}

    # ── 基础 5 层 ──────────
    if 'L2' in layers:
        out['L2'] = [f"{m} {seed}" for m in ATTRIBUTE_MODIFIERS]
    if 'L3' in layers:
        out['L3'] = [f"{seed} {sc}" for sc in SCENE_PATTERNS]
    if 'L4' in layers:
        out['L4'] = [f"{seed} {dg}" for dg in DEMOGRAPHICS[:15]] \
                  + [f"{dg} {seed}" for dg in ['seniors','women','men','athletes']]
    if 'L5' in layers:
        if body and body in BODY_TO_CONDITIONS:
            out['L5'] = list(BODY_TO_CONDITIONS[body])
        else:
            out['L5'] = [f'chronic {seed}', f'{seed} relief', f'{seed} treatment',
                         f'{seed} symptoms', f'{seed} causes']

    # ── 2026 新增 5 层 ─────
    if 'L6' in layers:      # 语音/问句
        out['L6'] = [t.replace('{s}', seed) for t in VOICE_TEMPLATES]
    if 'L7' in layers:      # 短视频钩子
        out['L7'] = [t.replace('{s}', seed) for t in TIKTOK_HOOKS]
    if 'L8' in layers:      # 情绪/身份
        out['L8'] = [t.replace('{s}', seed) for t in IDENTITY_MODIFIERS]
    if 'L9' in layers:      # 2026 健康新概念 (种子独立, 非模板)
        out['L9'] = list(WELLNESS_2026)
    if 'L10' in layers:     # 清洁/成分 — 只对有产品词根的 seed 生效
        if any(w in tokens for w in ('cream','serum','gel','balm','oil','spray',
                                       'supplement','lotion','powder','drops','pills')):
            out['L10'] = [f"{m} {seed}" for m in CLEAN_MODIFIERS[:15]]
        else:
            out['L10'] = []
    if 'L11' in layers:     # 时限承诺
        out['L11'] = [t.replace('{s}', seed) for t in TIMEBOX_PATTERNS]

    return {k: v for k, v in out.items() if v}  # 去掉空层


def _flatten_expansion(expanded: dict, max_per_layer: int = 20) -> list[dict]:
    """把分层扩展展平为可入库的 item 列表, 每层标注来源."""
    items = []
    for layer, kws in expanded.items():
        for kw in kws[:max_per_layer]:
            items.append({
                'keyword': kw.lower().strip(),
                'kw_type': _layer_to_type(layer),
                'subtype': f'rh-{layer.lower()}',
                'source_kw': layer,
            })
    return items


def _layer_to_type(layer: str) -> str:
    return {
        'L2': 'pain',    'L3': 'scene',   'L4': 'scene',  'L5': 'pain',
        'L6': 'scene',   'L7': 'scene',   'L8': 'scene',  'L9': 'product',
        'L10': 'product','L11': 'scene',
    }.get(layer, 'product')


# ── 1. HARVEST: 从高分推荐中学习新关键词 ─────────────────────────────────────

def productivity_tokens(min_score: int = 80, lookback_days: int = 60) -> dict:
    """
    分析高分推荐里哪些 token 是"产能因子".
    返回 {token: weight}, weight 表示该 token 出现在 score>=80 推荐的加权次数.
    这是兔子洞智能选种的基石: 能识别"真正产出爆品的词根".
    """
    from .persistence import get_persistence
    db = get_persistence()
    counter: Counter = Counter()
    try:
        with db._conn() as conn:
            rows = conn.execute("""
                SELECT keyword, top_pain, top_scene, top_product, score
                FROM recommendations
                WHERE score >= ? AND updated_at >= datetime('now','-' || ? || ' days')
            """, (min_score, lookback_days)).fetchall()
        for r in rows:
            w = int((r['score'] or 0) / 10)     # score=100 → w=10; 80 → 8
            for field in ('keyword','top_pain','top_scene','top_product'):
                for tok in _tokenize(r[field] or ''):
                    if len(tok) >= 4:
                        counter[tok] += w
    except Exception as e:
        log.warning(f"productivity_tokens 查询失败: {e}")
    return dict(counter)


def productivity_score(kw: str, prod_tokens: dict) -> int:
    """给一个候选词打 0-100 预测产能分. 基于与历史爆品的 token 重合度."""
    if not prod_tokens:
        return 50
    toks = _tokenize(kw)
    if not toks:
        return 0
    # 垃圾词过滤: 文件扩展名 / URL / 极端重复
    if any(t.endswith(ext) or f'.{ext}' in kw.lower() for ext in ['xlsx','xls','csv','txt','json','pdf','html','png','jpg']):
        return 0
    if 'http' in kw.lower() or '://' in kw or kw.count('-') > 3:
        return 0
    # 同 token 重复惩罚 (如 "pain pain")
    if len(toks) >= 2 and len(set(toks)) < len(toks):
        return 0
    # 去重后求权重和
    unique = set(toks)
    hit = sum(prod_tokens.get(t, 0) for t in unique)
    max_w = max(prod_tokens.values()) if prod_tokens else 1
    base = min(100, int(hit * 50 / max(max_w, 1)))
    n = len(unique)
    if n == 1: base -= 10
    elif n > 5: base -= 15
    return max(0, min(100, base))


def harvest_new_keywords(min_score: int = 70, lookback_days: int = 30) -> list[dict]:
    """扫描近 N 天 score>=阈值 的推荐, 抽取尚未入库的关键词."""
    from .persistence import get_persistence
    db = get_persistence()

    with db._conn() as conn:
        existing = {r[0].lower().strip() for r in conn.execute(
            "SELECT keyword FROM learned_keywords"
        ) if r and r[0]}

        recs = conn.execute(
            "SELECT domain, brand_name, product_slug, top_pain, top_scene, top_product, score, updated_at "
            "FROM recommendations WHERE score >= ? AND updated_at >= datetime('now','-' || ? || ' days')",
            (min_score, lookback_days),
        ).fetchall()

    candidates: Counter = Counter()
    type_hint: dict[str, str] = {}
    sources: dict[str, set] = defaultdict(set)

    for r in recs:
        rec = dict(r)
        w = max(1, int(rec['score'] / 20))

        for gram in _ngrams(_tokenize(rec.get('top_pain') or ''), 1, 3):
            if gram not in existing:
                candidates[gram] += w
                type_hint.setdefault(gram, 'pain')
                sources[gram].add(rec.get('domain') or '')

        for gram in _ngrams(_tokenize(rec.get('top_scene') or ''), 1, 3):
            if gram not in existing:
                candidates[gram] += w
                type_hint.setdefault(gram, 'scene')
                sources[gram].add(rec.get('domain') or '')

        for gram in _ngrams(_tokenize(rec.get('top_product') or ''), 1, 3):
            if gram not in existing:
                candidates[gram] += w
                type_hint.setdefault(gram, 'product')
                sources[gram].add(rec.get('domain') or '')

        slug = rec.get('product_slug') or ''
        if slug:
            for gram in _ngrams(_tokenize(slug.replace('-', ' ')), 2, 3):
                if gram not in existing:
                    candidates[gram] += w // 2 or 1
                    type_hint.setdefault(gram, 'product')
                    sources[gram].add(rec.get('domain') or '')

    harvested = []
    for kw, weight in candidates.most_common(300):
        src_cnt = len(sources[kw])
        if weight >= 5 and src_cnt >= 2:
            harvested.append({
                'keyword': kw,
                'kw_type': type_hint.get(kw, 'product'),
                'weight':  weight,
                'source_brands': src_cnt,
                'subtype': 'auto-harvest',
            })
        if len(harvested) >= 100:
            break
    return harvested


def smart_seed_pool(pool_size: int = 30) -> list[dict]:
    """
    智能种子池: 从历史 score>=85 推荐反向溯源出最具产能的干净种子.
    这解决了原 expand 用"随机 top_pain"当种子导致质量不稳的问题.
    """
    from .persistence import get_persistence
    db = get_persistence()
    seeds = []
    seen = set()
    prod_tokens = productivity_tokens(min_score=80, lookback_days=90)
    try:
        with db._conn() as conn:
            rows = conn.execute("""
                SELECT top_pain, top_scene, top_product, score, ads_count
                FROM recommendations
                WHERE score >= 80 AND updated_at >= datetime('now','-60 days')
                ORDER BY score DESC, ads_count DESC
                LIMIT 200
            """).fetchall()
        for r in rows:
            for field in ('top_pain', 'top_scene', 'top_product'):
                for phrase in _clean_seeds(r[field]):
                    if phrase in seen: continue
                    seen.add(phrase)
                    seeds.append({
                        'keyword': phrase,
                        'kw_type': {'top_pain':'pain','top_scene':'scene','top_product':'product'}[field],
                        'prod_score': productivity_score(phrase, prod_tokens),
                        'rec_score': r['score'],
                    })
    except Exception as e:
        log.warning(f"smart_seed_pool 失败: {e}")
    # 按 prod_score × rec_score 双重排序, 取 TOP N
    seeds.sort(key=lambda s: (s['prod_score'] * s['rec_score'], s['rec_score']), reverse=True)
    return seeds[:pool_size]


# ── 2. EXPAND: 5 层兔子洞扩展 (升级版) ──────────────────────────────────────

def _clean_seeds(raw: str) -> list[str]:
    """把一条 top_pain/top_scene 字段 (可能含逗号/分号/&) 切成干净短语列表."""
    if not raw:
        return []
    # 按 逗号/分号/斜杠/& 切分
    parts = re.split(r'[,;/&]|\band\b', raw.lower())
    out = []
    for p in parts:
        p = p.strip(' -,.')
        # 去掉过短/过长/含数字的
        if 4 <= len(p) <= 30 and not re.search(r'\d', p):
            # 去掉冗余前缀词
            p = re.sub(r'^(for |with |without |of |the |a |an )+', '', p).strip()
            if len(p) >= 4:
                out.append(p)
    return out


def expand_variations(seeds: list[dict] | None = None,
                      max_seeds: int = 40,
                      max_per_layer: int = 25) -> list[dict]:
    """
    智能兔子洞扩展 V4:
      1. 用 smart_seed_pool() 从高分 (≥80) 推荐溯源干净种子 (替代原随机选)
      2. 每个 seed 做 10 层兔子洞扩展
      3. 用 productivity_score 给每个变体打产能预测分, 只保留 TOP N
      4. 高产能种子对应的变体优先入库

    这个设计让"rabbit hole 真正赚钱": 从赢家反向找更多赢家, 而不是盲扩.
    """
    from .persistence import get_persistence
    db = get_persistence()

    with db._conn() as conn:
        existing = {r[0].lower().strip() for r in conn.execute(
            "SELECT keyword FROM learned_keywords"
        ) if r and r[0]}

    # 产能 token 表: 识别哪些 token 真的能带出爆品
    prod_tokens = productivity_tokens(min_score=80, lookback_days=90)

    # 种子池: 优先用本轮 harvest, 补齐用 smart_seed_pool
    pool: list[dict] = []
    pool_set: set = set()

    if seeds:
        for s in seeds:
            if s.get('kw_type') in ('pain','scene','product'):
                kw = s['keyword'].strip().lower()
                if kw and kw not in pool_set and len(kw) <= 30:
                    pool_set.add(kw)
                    prod = productivity_score(kw, prod_tokens)
                    pool.append({
                        'keyword': kw,
                        'weight': s.get('weight', 10) + prod // 2,
                        'prod': prod,
                    })

    # 补齐: smart_seed_pool 按 productivity × rec_score 排序
    if len(pool) < max_seeds:
        smart = smart_seed_pool(pool_size=max_seeds * 2)
        for s in smart:
            if s['keyword'] in pool_set:
                continue
            pool_set.add(s['keyword'])
            pool.append({
                'keyword': s['keyword'],
                'weight': s['prod_score'] + s['rec_score'] // 2,
                'prod': s['prod_score'],
            })
            if len(pool) >= max_seeds * 2:
                break

    # 按 weight (产能 + rec 分加权) 排序 TOP N
    pool.sort(key=lambda s: s['weight'], reverse=True)
    pool = pool[:max_seeds]

    variants = []
    seen = set(existing)
    used_seeds = []
    for seed_item in pool:
        seed = seed_item['keyword']
        seed_prod = seed_item.get('prod', 50)
        used_seeds.append(f"{seed}({seed_prod})")
        expanded = rabbit_hole_expand(seed)
        for item in _flatten_expansion(expanded, max_per_layer=max_per_layer):
            kw = item['keyword']
            if kw in seen or len(kw) < 4 or len(kw) > 60:
                continue
            # 每个变体再用 productivity 打分, 低于 15 的丢弃
            vp = productivity_score(kw, prod_tokens)
            if vp < 15 and seed_prod < 40:
                continue   # 种子本身没产能, 变体也不保留
            item['source_kw'] = f"{seed}/{item['source_kw']}"
            item['_prod'] = vp
            variants.append(item)
            seen.add(kw)

    # 按产能分排序入库, 淘汰低分
    variants.sort(key=lambda v: v.get('_prod', 0), reverse=True)

    log.info(f"智能兔子洞: {len(used_seeds)} 种子 (avg_prod={sum(s['prod'] for s in pool)//max(1,len(pool))}) "
             f"→ {len(variants)} 变体 (avg_prod={sum(v.get('_prod',0) for v in variants)//max(1,len(variants))})")
    return variants


# ── 3. RETIRE: 淘汰低产关键词 ───────────────────────────────────────────────

def retire_stale_keywords(min_age_days: int = 30, min_productivity: float = 45.0,
                          dry_run: bool = True) -> list[dict]:
    from .persistence import get_persistence
    db = get_persistence()

    candidates = []
    with db._conn() as conn:
        rows = conn.execute("""
            SELECT s.keyword,
                   MIN(s.scan_date) AS first_scan,
                   COUNT(DISTINCT s.scan_date) AS scan_count,
                   COALESCE(AVG(r.score), 0) AS avg_score,
                   COUNT(r.domain) AS rec_count
            FROM scans s
            LEFT JOIN recommendations r ON r.keyword = s.keyword
            WHERE s.scan_date <= date('now', '-' || ? || ' days')
            GROUP BY s.keyword
            HAVING avg_score < ? OR rec_count = 0
            ORDER BY avg_score ASC, rec_count ASC
            LIMIT 200
        """, (min_age_days, min_productivity)).fetchall()

        for r in rows:
            d = dict(r)
            d['productivity'] = round(d['avg_score'], 1)
            candidates.append(d)

        if not dry_run and candidates:
            kws = [c['keyword'] for c in candidates]
            conn.executemany(
                "UPDATE learned_keywords SET subtype='retired-low-roi' "
                "WHERE LOWER(keyword) = LOWER(?) AND subtype != 'retired-low-roi'",
                [(k,) for k in kws],
            )
            conn.commit()
            log.info(f"标记淘汰 {len(kws)} 个低产关键词")
    return candidates


# ── 4. CLUSTER: 品类自动发现 ────────────────────────────────────────────────

def suggest_new_categories(min_cluster_size: int = 15) -> list[dict]:
    from .persistence import get_persistence
    from .categories import load_categories

    db = get_persistence()
    try:
        cfg = load_categories() or {}
        existing_kws = set()
        for cat in cfg.get('categories', []):
            for sk in cat.get('seed_keywords', []):
                existing_kws.add(sk.lower().strip())
    except Exception:
        existing_kws = set()

    with db._conn() as conn:
        rows = conn.execute(
            "SELECT keyword, kw_type FROM learned_keywords "
            "WHERE kw_type IN ('pain','scene','product') "
            "AND (subtype IS NULL OR subtype NOT LIKE 'retired%')"
        ).fetchall()

    token_to_kws: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        kw = r[0]
        kw_lower = kw.lower()
        if kw_lower in existing_kws:
            continue
        for tok in _tokenize(kw):
            if len(tok) >= 4:
                token_to_kws[tok].append(kw)

    suggestions = []
    for tok, kws in sorted(token_to_kws.items(), key=lambda x: -len(x[1])):
        if len(kws) >= min_cluster_size and tok not in existing_kws:
            seeds = sorted(set(kws), key=len)[:10]
            suggestions.append({
                'cluster_token': tok,
                'size': len(kws),
                'seed_keywords': seeds,
                'proposed_category': f"auto-{tok}",
            })
        if len(suggestions) >= 20:
            break
    return suggestions


# ── 5. 写回 learned_keywords ────────────────────────────────────────────────

def ingest_harvested(items: list[dict]) -> int:
    if not items:
        return 0
    from .persistence import get_persistence
    db = get_persistence()
    today = datetime.now().strftime('%Y-%m-%d')
    added = 0
    with db._conn() as conn:
        for it in items:
            try:
                before = conn.total_changes
                conn.execute(
                    "INSERT OR IGNORE INTO learned_keywords "
                    "(keyword, kw_type, subtype, source_kw, learned_date) "
                    "VALUES (?,?,?,?,?)",
                    (it['keyword'], it['kw_type'],
                     it.get('subtype', 'auto'),
                     it.get('source_kw', it.get('subtype', 'auto')),
                     today),
                )
                if conn.total_changes > before:
                    added += 1
            except Exception:
                continue
        conn.commit()
    return added


# ── 6. 编排器 ─────────────────────────────────────────────────────────────────

def evolve(dry_run: bool = False, write_queue: bool = True,
           max_seeds: int = 30, max_per_layer: int = 25) -> dict:
    """一次完整进化: harvest + 5 层兔子洞扩展 + retire + cluster."""
    report = {
        'run_at': datetime.now().isoformat(timespec='seconds'),
        'dry_run': dry_run,
        'rabbit_hole_layers': [
            'L2-属性','L3-场景','L4-人群','L5-根因',
            'L6-问句(Voice/GenZ)','L7-短视频钩子(TikTok)','L8-情绪身份',
            'L9-2026健康(迷走/冷热/长寿)','L10-清洁成分','L11-时限承诺',
        ],
    }

    harvested = harvest_new_keywords(min_score=70, lookback_days=30)
    report['harvested'] = {'count': len(harvested), 'sample': harvested[:10]}

    # V2: 5 层兔子洞扩展 (替代原 cartesian)
    expanded = expand_variations(seeds=harvested, max_seeds=max_seeds,
                                 max_per_layer=max_per_layer)
    # 按 layer 统计
    by_layer = Counter(it['source_kw'].split('/')[-1] for it in expanded)
    report['expanded'] = {
        'count': len(expanded),
        'by_layer': dict(by_layer),
        'sample': expanded[:12],
    }

    if not dry_run:
        added_h = ingest_harvested(harvested)
        added_e = ingest_harvested(expanded)
        report['ingested'] = {'harvest_added': added_h, 'expand_added': added_e}
    else:
        report['ingested'] = {'dry_run': True}

    retired = retire_stale_keywords(min_age_days=30, min_productivity=45.0, dry_run=dry_run)
    report['retired'] = {'count': len(retired), 'sample': retired[:10]}

    clusters = suggest_new_categories(min_cluster_size=15)
    report['clusters'] = {'count': len(clusters), 'suggestions': clusters}

    if write_queue and not dry_run and (harvested or expanded):
        from .paths import RESULTS_DIR
        queue_file = RESULTS_DIR / f"scan_queue_evolution_{datetime.now().strftime('%Y-%m-%d')}.txt"
        new_kws = [it['keyword'] for it in (harvested + expanded)]
        with open(queue_file, 'w', encoding='utf-8') as f:
            f.write(f"# === 关键词进化自动生成 {report['run_at']} ===\n")
            f.write(f"# harvest={len(harvested)} expand(5层兔子洞)={len(expanded)}\n\n")
            for kw in new_kws:
                f.write(f"{kw}|待翻译|auto|evolution\n")
        report['queue_file'] = str(queue_file)

    return report


# ── 7. 外部种子抓取: 6 平台多源融合 ─────────────────────────────────────────

_UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
       'AppleWebKit/537.36 (KHTML, like Gecko) '
       'Chrome/126.0.0.0 Safari/537.36')


def _http_json(url: str, timeout: int = 6, headers: dict | None = None):
    h = {
        'User-Agent': _UA,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    if headers: h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw.decode('utf-8', errors='replace'))


def fetch_amazon_suggestions(prefix: str) -> list[str]:
    try:
        url = (f"https://completion.amazon.com/api/2017/suggestions"
               f"?mid=ATVPDKIKX0DER&alias=aps&prefix={urllib.parse.quote(prefix)}")
        data = _http_json(url)
        return [s.get('value', '').strip().lower()
                for s in (data.get('suggestions') or []) if s.get('value')]
    except Exception as e:
        log.warning(f"Amazon 失败 ({prefix}): {e}")
        return []


def fetch_google_suggestions(prefix: str) -> list[str]:
    try:
        url = (f"https://suggestqueries.google.com/complete/search"
               f"?client=firefox&q={urllib.parse.quote(prefix)}")
        data = _http_json(url)
        if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
            return [s.strip().lower() for s in data[1] if s]
        return []
    except Exception as e:
        log.warning(f"Google 失败 ({prefix}): {e}")
        return []


def fetch_bing_suggestions(prefix: str) -> list[str]:
    """Bing autosuggest (OpenSearch format)."""
    try:
        url = (f"https://api.bing.com/osjson.aspx?query={urllib.parse.quote(prefix)}")
        data = _http_json(url)
        if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
            return [s.strip().lower() for s in data[1] if s]
        return []
    except Exception as e:
        log.warning(f"Bing 失败 ({prefix}): {e}")
        return []


def fetch_duckduckgo_suggestions(prefix: str) -> list[str]:
    """DuckDuckGo autocomplete."""
    try:
        url = (f"https://duckduckgo.com/ac/?q={urllib.parse.quote(prefix)}&type=list")
        data = _http_json(url)
        # 格式 1: [query, [list...]];  格式 2: [{'phrase': '...'}, ...]
        if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
            return [s.strip().lower() for s in data[1] if s]
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return [d.get('phrase', '').strip().lower() for d in data if d.get('phrase')]
        return []
    except Exception as e:
        log.warning(f"DuckDuckGo 失败 ({prefix}): {e}")
        return []


def fetch_youtube_suggestions(prefix: str) -> list[str]:
    """YouTube search suggestion (走 Google 的 ds=yt)."""
    try:
        url = (f"https://suggestqueries.google.com/complete/search"
               f"?client=youtube&ds=yt&q={urllib.parse.quote(prefix)}")
        req = urllib.request.Request(url, headers={'User-Agent': _UA})
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
        # YouTube 返回 JSONP-style, 形如 window.google.ac.h([...])
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        if not m: return []
        data = json.loads(m.group(0))
        if len(data) >= 2 and isinstance(data[1], list):
            return [item[0].strip().lower() for item in data[1]
                    if isinstance(item, list) and item and isinstance(item[0], str)]
        return []
    except Exception as e:
        log.warning(f"YouTube 失败 ({prefix}): {e}")
        return []


def fetch_wikipedia_suggestions(prefix: str) -> list[str]:
    """Wikipedia OpenSearch API. 对医学/条件词特别有效."""
    try:
        url = (f"https://en.wikipedia.org/w/api.php?action=opensearch"
               f"&search={urllib.parse.quote(prefix)}&limit=15&namespace=0&format=json")
        data = _http_json(url)
        # 格式 [query, [titles], [descriptions], [urls]]
        if isinstance(data, list) and len(data) >= 2 and isinstance(data[1], list):
            return [s.strip().lower() for s in data[1] if s and len(s) <= 80]
        return []
    except Exception as e:
        log.warning(f"Wikipedia 失败 ({prefix}): {e}")
        return []


# 源注册表: 按优先级排, 失败一个不影响其他
PLATFORM_FETCHERS = [
    ('amazon', fetch_amazon_suggestions),
    ('google', fetch_google_suggestions),
    ('bing', fetch_bing_suggestions),
    ('ddg', fetch_duckduckgo_suggestions),
    ('youtube', fetch_youtube_suggestions),
    ('wikipedia', fetch_wikipedia_suggestions),
]

TREND_2026_TERMS = {
    'vagus': 34, 'nerve': 18, 'nervous': 26, 'somatic': 30, 'cortisol': 32,
    'lymph': 28, 'drainage': 18, 'fascia': 28, 'red light': 32, 'infrared': 24,
    'cold plunge': 30, 'circadian': 26, 'mitochondria': 24, 'longevity': 30,
    'glp-1': 36, 'creatine': 26, 'magnesium': 22, 'glycinate': 18,
    'women over': 20, 'menopause': 28, 'perimenopause': 30, 'pcos': 28,
    'non toxic': 20, 'nontoxic': 20, 'clean': 12, 'hormone': 22,
    'grounding': 24, 'earthing': 22, 'sleep': 16, 'anxiety': 18,
    'tiktok': 16, 'hack': 12, 'actually works': 20, 'before and after': 18,
}

COMMERCIAL_INTENT_TERMS = {
    'best': 16, 'near me': 10, 'for women': 12, 'for men': 10, 'kit': 14,
    'device': 18, 'machine': 18, 'massager': 18, 'brace': 16, 'sleeve': 14,
    'pillow': 14, 'mat': 14, 'patch': 16, 'cream': 16, 'serum': 16,
    'supplement': 18, 'drops': 16, 'relief': 18, 'treatment': 14,
    'alternative': 18, 'reviews': 12, 'amazon': 10,
}


def trend_score_2026(kw: str, sources: list[str] | None = None) -> int:
    s = (kw or '').lower()
    score = 0
    for term, weight in TREND_2026_TERMS.items():
        if term in s:
            score += weight
    for term, weight in COMMERCIAL_INTENT_TERMS.items():
        if term in s:
            score += weight
    toks = re.findall(r'[a-z0-9+-]+', s)
    if 2 <= len(toks) <= 5:
        score += 10
    elif len(toks) == 1:
        score -= 8
    elif len(toks) > 7:
        score -= 12
    if sources:
        src = set(sources)
        score += min(30, len(src) * 8)
        if 'amazon' in src:
            score += 8
        if 'youtube' in src:
            score += 6
        if 'google' in src:
            score += 6
    return max(0, min(100, score))


# ── 自动分类: 从关键词内容推断 kw_type ───────────────────────────────────────

_PAIN_HINTS = {'pain','ache','sore','soreness','stiffness','tension','cramp',
               'spasm','inflammation','injury','arthritis','sciatica','fatigue',
               'migraine','headache','burn','itch','numbness','tingle','bruise',
               'tear','strain','sprain','tendinitis','bursitis'}
_SCENE_HINTS = {'when','after','during','before','while','at night','in the morning',
                'at home','at work','for sleep','for travel'}
_VOICE_HINTS = {'how to','why does','what causes','what is','should i',
                'can i','best way','how do'}
_PRODUCT_HINTS = {'massager','cream','gel','balm','oil','spray','serum',
                  'brace','sleeve','support','roller','stretcher','compression',
                  'wrap','cushion','pillow','mat','strap','belt','tape',
                  'supplement','pill','capsule','powder','drops','patch',
                  'machine','device','tool','stick','ball','kit'}


def _classify_keyword(kw: str) -> tuple[str, str]:
    """
    根据关键词内容推断 (kw_type, subtype).
    返回 kw_type ∈ {pain, scene, product, brand}.
    """
    s = kw.lower()
    tokens = set(re.findall(r'[a-z]+', s))
    # 1. 问句式 → 归 scene (语境/意图)
    if any(s.startswith(v) or (' ' + v + ' ') in (' ' + s + ' ') for v in _VOICE_HINTS):
        return ('scene', 'voice-query')
    # 2. 带场景词 → scene
    if any(h in s for h in _SCENE_HINTS):
        return ('scene', 'scene-context')
    # 3. 带痛点/病症词 → pain
    if tokens & _PAIN_HINTS:
        return ('pain', 'pain-symptom')
    # 4. 带产品载体词 → product
    if tokens & _PRODUCT_HINTS:
        return ('product', 'product-item')
    # 5. 兜底
    return ('product', 'generic')


def fetch_external_suggestions(seed: str, alphabet: bool = False) -> dict:
    """
    多源融合外部搜索建议. 6 平台并联, 任一失败不影响其他.
    返回结构:
      {
        seed, alphabet_mode,
        per_platform: { amazon: [...], google: [...], bing: [...], ... },
        combined: [...],           # 所有去重后
        kw_sources: { kw: [platforms...] },  # 反查每个词的来源
        classified: { kw: {kw_type, subtype} },  # 自动分类
        new_count, already_count, already: [...],
      }
    """
    seed = (seed or '').strip().lower()
    if not seed or len(seed) < 2:
        return {'error': 'seed too short'}

    per_platform: dict[str, set] = {name: set() for name, _ in PLATFORM_FETCHERS}
    prefixes = [seed]
    if alphabet:
        prefixes.extend(f"{seed} {ch}" for ch in 'abcdefghijklmnopqrstuvwxyz')

    jobs = []
    with ThreadPoolExecutor(max_workers=18) as ex:
        for prefix in prefixes:
            for name, fetcher in PLATFORM_FETCHERS:
                jobs.append((name, ex.submit(fetcher, prefix)))
        for name, fut in jobs:
            try:
                per_platform[name].update(fut.result(timeout=8))
            except Exception as e:
                log.warning(f"{name} suggestion job failed: {e}")

    # 过滤 + 反向索引
    def _ok(kw: str) -> bool:
        return 4 <= len(kw) <= 80 and kw != seed

    kw_sources: dict[str, list[str]] = defaultdict(list)
    for name, s in per_platform.items():
        for kw in s:
            if _ok(kw):
                kw_sources[kw].append(name)

    combined = sorted(kw_sources.keys())

    # 自动分类
    classified = {kw: {'kw_type': t, 'subtype': st}
                  for kw, (t, st) in ((k, _classify_keyword(k)) for k in combined)}

    # DB 去重标记
    already = set()
    try:
        from .persistence import get_persistence
        with get_persistence()._conn() as conn:
            for kw in combined:
                r = conn.execute("SELECT 1 FROM learned_keywords WHERE LOWER(keyword)=? LIMIT 1", (kw,)).fetchone()
                if r:
                    already.add(kw)
    except Exception:
        pass

    scored = [
        {'keyword': k, 'trend_score': trend_score_2026(k, kw_sources[k]), 'sources': kw_sources[k], **classified[k]}
        for k in combined
    ]
    scored.sort(key=lambda x: (-x['trend_score'], -len(x['sources']), x['keyword']))
    combined_ranked = [x['keyword'] for x in scored]

    return {
        'seed': seed,
        'alphabet_mode': alphabet,
        'per_platform': {name: sorted(s) for name, s in per_platform.items()},
        'platform_counts': {name: len(s) for name, s in per_platform.items()},
        'combined': combined_ranked,
        'kw_sources': dict(kw_sources),
        'classified': classified,
        'scored': scored,
        'top_2026': scored[:50],
        'new_count': len(combined) - len(already),
        'already_count': len(already),
        'already': sorted(already),
    }


def ingest_suggestions(keywords: list[str], source: str = 'multi-platform') -> int:
    """自动分类 + 入库. 每个词根据内容自动打 pain/scene/product 标签."""
    if not keywords:
        return 0
    items = []
    for k in keywords:
        k = (k or '').strip().lower()
        if 4 <= len(k) <= 80:
            kw_type, subtype = _classify_keyword(k)
            items.append({
                'keyword': k,
                'kw_type': kw_type,
                'subtype': f'suggest-{subtype}',
                'source_kw': source,
            })
    return ingest_harvested(items)

"""pipeline/strategies.py — 三种实战选品策略, 都输出 scan_queue_*.txt.

策略 1: 爆款反查 (Lookalike)
    从 recommendations 中挑 score≥80 且 ad_age≥90 的爆款,
    拿它们的 top_pain / top_scene 去模糊匹配 brands 表,
    找出覆盖同痛点但 orbit_score < 30 的小品牌(还没做爆的替代品).

策略 2: 痛点空白 (Pain Gap)
    聚合所有 _corrected.json 里的 top_pain_keywords,
    找 出现次数 ≥ 10 但对应品牌数 < 3 的"高需求+少供给"痛点,
    这些痛点词本身就是下一批值得扫的关键词.

策略 3: 季节提前期 (Seasonal Pre-wave)
    拿当前月份 +2 月 → 查该未来月份历史上的爆款类目 →
    从 categories.json 抽对应种子关键词入队.
    用内置月份-类目映射表实现(categories.json 暂无 seasonal 字段).

每种策略输出到独立 scan_queue_*.txt, 和 rabbit_hole 互补.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from core.logger import get_logger

from .paths import NEW_DIR, SCAN_FILE, LEARNED_FILE, RESULTS_DIR
from .persistence import get_persistence
from .categories import load_categories

try:
    from .insights_2026 import is_dead_keyword
    _HAS_DEAD = True
except ImportError:
    _HAS_DEAD = False
    def is_dead_keyword(kw: str) -> bool:
        return False

log = get_logger(__name__)

# 输出文件 (与 scan_queue_rabbit_hole.txt 同目录)
LOOKALIKE_FILE = RESULTS_DIR / "scan_queue_lookalike.txt"
PAINGAP_FILE   = RESULTS_DIR / "scan_queue_paingap.txt"
SEASONAL_FILE  = RESULTS_DIR / "scan_queue_seasonal.txt"


# ── 已知关键词 (用于去重) ──────────────────────────────────────────────────

def _already_known() -> set[str]:
    known: set[str] = set()
    for f in (SCAN_FILE, LEARNED_FILE):
        if not f.exists():
            continue
        try:
            for line in f.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                kw = line.split('|')[0].strip().lower()
                if kw:
                    known.add(kw)
        except Exception:
            continue
    return known


def _write_queue(path: Path, items: list[tuple[str, str]], header: str) -> int:
    """items: [(keyword, reason), ...]"""
    if not items:
        # 空结果也写空文件便于 UI 检测时间戳
        path.write_text(f"# {header}\n# {datetime.now().isoformat(timespec='seconds')}\n# (本轮无候选)\n",
                        encoding='utf-8')
        return 0
    lines = [f"# {header}", f"# 生成: {datetime.now().isoformat(timespec='seconds')}",
             f"# 条目: {len(items)}", ""]
    for kw, reason in items:
        lines.append(f"{kw} | {reason}")
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return len(items)


# ── 策略 1: 爆款反查 ──────────────────────────────────────────────────────
#
# 2.0 逻辑: 找爆款的 source keyword, 反查同一关键词下出现过但 orbit 低的品牌.
# 这些品牌是"被爆款关键词扫到但自己没火"的候选 — 值得深入研究做跟风盘.

def generate_lookalike_queue(
    min_winner_score: int = 80,
    min_winner_age: int = 90,
    max_candidate_orbit: int = 40,
    limit: int = 50,
) -> int:
    db = get_persistence()
    with db._conn() as conn:
        # 1. 爆款的 source keyword (去重)
        winner_keywords = [
            r['keyword'].strip().lower()
            for r in conn.execute(
                """SELECT DISTINCT keyword FROM recommendations
                   WHERE score >= ? AND ad_age_days >= ? AND keyword IS NOT NULL
                   ORDER BY score DESC LIMIT 200""",
                (min_winner_score, min_winner_age),
            )
            if r['keyword']
        ]
        if not winner_keywords:
            return _write_queue(LOOKALIKE_FILE, [], "爆款反查: 无可用爆款")

        # 2. 同一关键词下 orbit_score 低的品牌 (被爆款扫到但没火)
        #    且 fb_fitness >= 40 (排除 FB 不适合的品)
        placeholders = ','.join('?' * len(winner_keywords))
        low_brands = conn.execute(
            f"""SELECT b.domain, b.canonical_name, MAX(b.orbit_score) AS orbit,
                       GROUP_CONCAT(DISTINCT bk.keyword) AS kws,
                       COUNT(DISTINCT bk.keyword) AS kw_count
                FROM v_brands b
                JOIN v_brand_keywords bk ON bk.domain = b.domain
                LEFT JOIN recommendations r ON r.domain = b.domain
                WHERE LOWER(bk.keyword) IN ({placeholders})
                GROUP BY b.domain
                HAVING MAX(b.orbit_score) < ? AND MAX(b.orbit_score) > 0
                   AND COALESCE(MAX(r.fb_fitness), 0) >= 40
                ORDER BY kw_count DESC, orbit DESC LIMIT ?""",
                (*winner_keywords, max_candidate_orbit, limit * 2),
        ).fetchall()

    known = _already_known()
    items: list[tuple[str, str]] = []
    for r in low_brands:
        name = (r['canonical_name'] or r['domain'] or '').strip()
        if not name:
            continue
        # 用 domain 而不是品牌名(更唯一) + 去掉 www. / .com 太通用
        lookup = name.lower()
        if lookup in known:
            continue
        kws_preview = (r['kws'] or '').split(',')[:2]
        kws_str = ' + '.join(kws_preview)
        fb_note = f", FB≥40" if r['orbit'] else ""
        items.append((
            name,
            f"orbit={r['orbit']}{fb_note}, 被爆款关键词扫到 {r['kw_count']} 次 ({kws_str})",
        ))
        if len(items) >= limit:
            break

    n = _write_queue(LOOKALIKE_FILE, items, "爆款反查 — 爆款关键词下低 orbit 候选品牌")
    log.info(f"策略 [爆款反查]: 爆款关键词 {len(winner_keywords)}, 候选低 orbit 品牌 {len(low_brands)}, 入队 {n}")
    return n


# ── 策略 2: 痛点空白 ──────────────────────────────────────────────────────
#
# 2.0 逻辑: 聚合 per-competitor top_pain_keywords, 用 landing domain 去重 brand.
# 目标: 找"高频提及 (≥N次) 但品牌覆盖 (≤M个独立 domain) 少"的痛点 —
#       说明市场需求大但供给少, 是蓝海切入点.
# 不过滤 known, 因为痛点词本身就是候选关键词 (需要我们再扫).

def generate_paingap_queue(
    min_appearances: int = 30,
    max_brands: int = 15,
    limit: int = 50,
) -> int:
    pain_freq: Counter = Counter()
    pain_domains: defaultdict[str, set] = defaultdict(set)

    from .domain import canonical_domain, extract_domain_from_url

    files = [f for f in RESULTS_DIR.glob('*_corrected.json') if '汇总' not in f.name]
    for jf in files:
        try:
            data = json.loads(jf.read_text(encoding='utf-8'))
        except Exception:
            continue

        for comp in data.get('top_competitors') or []:
            if not isinstance(comp, dict):
                continue
            # 用 landing_url / landing → canonical domain 作为 brand 唯一标识
            landing = comp.get('landing_url') or comp.get('landing') or ''
            dom = (extract_domain_from_url(landing) or '').lower().strip() \
                  if landing else ''
            if not dom:
                # 退而用 brand 名
                dom = (comp.get('brand') or '').lower().strip()
            if not dom:
                continue

            ta = comp.get('text_analysis') or {}
            for kw in ta.get('top_pain_keywords') or []:
                kw = (kw or '').lower().strip()
                if len(kw) < 4 or kw in ('pain', 'relief'):
                    continue
                pain_freq[kw] += 1
                pain_domains[kw].add(dom)

    # 排序: 频次高 且 品牌少 → 蓝海分数 = freq / (brand_count+1)
    ranked = sorted(
        [(kw, freq, len(pain_domains[kw])) for kw, freq in pain_freq.items()],
        key=lambda x: -(x[1] / (x[2] + 1))
    )

    items: list[tuple[str, str]] = []
    for kw, freq, bc in ranked:
        if freq < min_appearances or bc > max_brands:
            continue
        if _HAS_DEAD and is_dead_keyword(kw):
            continue
        gap_score = round(freq / (bc + 1), 1)
        items.append((kw, f"{freq}次提及 / 仅{bc}个品牌 / 蓝海分={gap_score}"))
        if len(items) >= limit:
            break

    n = _write_queue(PAINGAP_FILE, items,
                     f"痛点空白 — ≥{min_appearances}次提及且品牌≤{max_brands} (按蓝海分排序)")
    log.info(f"策略 [痛点空白]: 总痛点 {len(pain_freq)}, 满足条件 {n}")
    return n


# ── 策略 3: 季节提前期 ──────────────────────────────────────────────────────

# 简化的月份→关键词映射 (欧美市场消费节奏)
# key = 投放月份, value = 在该月需要已经开始测品/上架的品类关键词
SEASONAL_MAP: dict[int, list[str]] = {
    1:  ['home gym equipment', 'meal prep containers', 'fitness tracker', 'resistance bands set', 'weight loss supplement'],
    2:  ['valentine gift for him', 'valentine gift for her', 'couples games', 'heart shaped jewelry'],
    3:  ['spring cleaning tools', 'garden planter', 'allergy relief', 'outdoor pet gear'],
    4:  ['easter basket', 'mothers day gift', 'spring dress', 'grill accessories', 'backyard games'],
    5:  ['summer camping gear', 'pool float adult', 'beach cooler', 'wedding guest dress', 'fathers day gift'],
    6:  ['4th of july decor', 'boat accessories', 'summer skincare spf', 'portable fan', 'bbq smoker'],
    7:  ['back to school supplies', 'laptop stand ergonomic', 'kids backpack', 'dorm room decor'],
    8:  ['halloween costume', 'halloween decor outdoor', 'cozy sweater', 'fall candle'],
    9:  ['thanksgiving tableware', 'turkey fryer', 'warm blanket electric', 'gaming chair'],
    10: ['christmas lights', 'christmas tree artificial', 'holiday gift set', 'advent calendar', 'ugly christmas sweater'],
    11: ['new year fitness', 'planner 2027', 'diet meal kit', 'kitchen organizer', 'air fryer xl'],
    12: ['valentines day pre-sale', 'heart shaped lamp', 'couples pajamas', 'diet tracker'],
}


def generate_seasonal_queue(
    lead_months: int = 2,
    limit: int = 30,
) -> int:
    """当前月份 + lead_months → 抽该月对应季节关键词入队."""
    now = datetime.now()
    target_month = ((now.month - 1 + lead_months) % 12) + 1
    target_name = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][target_month-1]
    seeds = SEASONAL_MAP.get(target_month, [])

    known = _already_known()
    items: list[tuple[str, str]] = []
    for kw in seeds:
        if kw.lower() in known:
            continue
        if _HAS_DEAD and is_dead_keyword(kw):
            continue
        items.append((kw, f"{lead_months}月后({target_name})季节性爆品, 提前 test"))
        if len(items) >= limit:
            break

    n = _write_queue(
        SEASONAL_FILE,
        items,
        f"季节提前期 — 目标月 {target_name} (当前 {now.strftime('%b')} + {lead_months}月)",
    )
    log.info(f"策略 [季节提前期]: 目标月 {target_name}, 种子 {len(seeds)}, 新入队 {n}")
    return n


# ── 合并入主扫描队列 ──────────────────────────────────────────────────────

def ingest_strategy_queues_into_scan() -> int:
    """把三个策略队列的新关键词 (去重后) 追加进 facebook-ad-keywords-scan.txt."""
    known = _already_known()
    new_lines: list[str] = []
    sources = [
        (LOOKALIKE_FILE, 'lookalike'),
        (PAINGAP_FILE, 'paingap'),
        (SEASONAL_FILE, 'seasonal'),
    ]
    for path, tag in sources:
        if not path.exists():
            continue
        for line in path.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            kw = line.split('|')[0].strip()
            if not kw or kw.lower() in known:
                continue
            known.add(kw.lower())
            new_lines.append(f"{kw}  # {tag}")

    if not new_lines:
        return 0

    today = datetime.now().strftime('%Y-%m-%d')
    with open(SCAN_FILE, 'a', encoding='utf-8') as f:
        f.write(f"\n# === 策略入队 {today} ({len(new_lines)} 个) ===\n")
        for ln in new_lines:
            f.write(ln + '\n')
    log.info(f"策略: 三种方法共入队 {len(new_lines)} 个新关键词")
    return len(new_lines)


def run_all_strategies() -> dict:
    """跑三种策略 + 合并 + 给 recommendations 表打标签."""
    r = {
        'lookalike': generate_lookalike_queue(),
        'paingap': generate_paingap_queue(),
        'seasonal': generate_seasonal_queue(),
    }
    r['ingested'] = ingest_strategy_queues_into_scan()
    tagged = tag_recommendations_by_strategy()
    r['tagged'] = tagged
    from .notify import banner
    banner(
        "🎯 策略选品完成",
        f"爆款反查 {r['lookalike']} + 痛点空白 {r['paingap']} + 季节提前 {r['seasonal']}",
        f"入主队列 {r['ingested']} 个 · 推荐表打标签 {sum(tagged.values())} 行",
        highlight=bool(r['ingested']) or bool(sum(tagged.values())),
    )
    return r


def tag_recommendations_by_strategy() -> dict[str, int]:
    """把三种策略的候选回写到 recommendations.strategy_tags 列.

    匹配逻辑:
    - lookalike: 候选品牌名/域名 → 精确匹配 recommendations.domain / brand_name
    - paingap:   痛点词 → recommendations.top_pain LIKE '%pain_word%'
    - seasonal:  季节关键词 → recommendations.keyword 精确匹配
    """
    db = get_persistence()
    counts = {'lookalike': 0, 'paingap': 0, 'seasonal': 0}

    # 读取三个 queue 文件
    def _read_keywords(path: Path) -> list[str]:
        if not path.exists(): return []
        out = []
        for ln in path.read_text(encoding='utf-8').splitlines():
            ln = ln.strip()
            if not ln or ln.startswith('#'):
                continue
            out.append(ln.split('|')[0].strip())
        return out

    lookalike_items = _read_keywords(LOOKALIKE_FILE)
    paingap_items   = _read_keywords(PAINGAP_FILE)
    seasonal_items  = _read_keywords(SEASONAL_FILE)

    with db._conn() as conn:
        # 保证列存在
        try:
            conn.execute("ALTER TABLE recommendations ADD COLUMN strategy_tags TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE recommendations ADD COLUMN fb_fitness INTEGER")
        except Exception:
            pass
        # 清空旧 tag (每次跑是全量刷新)
        conn.execute("UPDATE recommendations SET strategy_tags = NULL")

        # lookalike: 精确匹配 domain / brand_name (LOWER)
        for kw in lookalike_items:
            low = kw.lower()
            cur = conn.execute(
                """UPDATE recommendations
                   SET strategy_tags = COALESCE(strategy_tags || ',', '') || 'lookalike'
                   WHERE (LOWER(domain) = ? OR LOWER(brand_name) = ?)
                     AND (strategy_tags IS NULL OR strategy_tags NOT LIKE '%lookalike%')""",
                (low, low),
            )
            counts['lookalike'] += cur.rowcount

        # paingap: top_pain LIKE (痛点词是名词短语, 可能出现在 top_pain 里)
        for kw in paingap_items:
            cur = conn.execute(
                """UPDATE recommendations
                   SET strategy_tags = COALESCE(strategy_tags || ',', '') || 'paingap'
                   WHERE (LOWER(top_pain) LIKE ? OR LOWER(top_scene) LIKE ?)
                     AND (strategy_tags IS NULL OR strategy_tags NOT LIKE '%paingap%')""",
                (f"%{kw.lower()}%", f"%{kw.lower()}%"),
            )
            counts['paingap'] += cur.rowcount

        # seasonal: keyword LIKE (季节词是短语, 用 LIKE 方便宽松匹配)
        for kw in seasonal_items:
            cur = conn.execute(
                """UPDATE recommendations
                   SET strategy_tags = COALESCE(strategy_tags || ',', '') || 'seasonal'
                   WHERE LOWER(keyword) LIKE ?
                     AND (strategy_tags IS NULL OR strategy_tags NOT LIKE '%seasonal%')""",
                (f"%{kw.lower()}%",),
            )
            counts['seasonal'] += cur.rowcount

        # 回写 fb_fitness 列 (从 recommendations 表内已有的 fb_fitness 复制到 strategy_tag 候选行)
        # 此列在 rebuild_recommendations 时已填充, 这里只确保 ALTER TABLE 过
        # (fb_fitness 列已在上面的 ALTER TABLE 里确保存在)

    log.info(f"策略 tag 回写: {counts}")
    return counts

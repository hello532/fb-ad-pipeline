"""pipeline/categories.py — 类目定义 + 种子关键词注入."""
from __future__ import annotations

import json
import copy
import random
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.logger import get_logger

from .paths import NEW_DIR, SCAN_FILE, LEARNED_FILE

log = get_logger(__name__)

CATEGORIES_FILE = NEW_DIR / "categories.json"
_CATEGORIES_CACHE_LOCK = threading.RLock()
_CATEGORIES_CACHE_MTIME_NS: int | None = None
_CATEGORIES_CACHE_DATA: dict | None = None


def _default_categories() -> dict:
    return {"regions": {"primary": "US", "secondary": [], "rotation_weight_primary": 1.0},
            "high_ticket_threshold_usd": 50, "categories": []}


def load_categories() -> dict:
    global _CATEGORIES_CACHE_MTIME_NS, _CATEGORIES_CACHE_DATA
    if not CATEGORIES_FILE.exists():
        return _default_categories()
    with _CATEGORIES_CACHE_LOCK:
        mtime_ns = CATEGORIES_FILE.stat().st_mtime_ns
        if _CATEGORIES_CACHE_DATA is not None and _CATEGORIES_CACHE_MTIME_NS == mtime_ns:
            return copy.deepcopy(_CATEGORIES_CACHE_DATA)
        data = json.loads(CATEGORIES_FILE.read_text(encoding='utf-8'))
        _CATEGORIES_CACHE_MTIME_NS = mtime_ns
        _CATEGORIES_CACHE_DATA = data
        return copy.deepcopy(data)


def get_region_code() -> str:
    """按权重随机选一个国家, 主区最高, 次区中等, 高净值 tier2 低权重探测."""
    import random
    cfg = load_categories()
    reg = cfg.get('regions', {})
    primary = reg.get('primary', 'US')
    secondary = reg.get('secondary', [])
    tier2 = reg.get('tier2_wealthy', [])
    wp = reg.get('rotation_weight_primary', 0.65)
    ws = reg.get('rotation_weight_secondary', 0.25)
    r = random.random()
    if r < wp or not secondary:
        return primary
    if r < wp + ws or not tier2:
        return random.choice(secondary)
    return random.choice(tier2)


def get_all_regions() -> list[str]:
    """所有可投放地区 (primary + secondary + tier2)."""
    cfg = load_categories().get('regions', {})
    out = [cfg.get('primary', 'US')]
    out.extend(cfg.get('secondary', []))
    out.extend(cfg.get('tier2_wealthy', []))
    return out


def get_region_metadata() -> dict:
    """每个地区的人均 GDP / FB 渗透率 / 电商成熟度 + 中文说明."""
    return load_categories().get('regions', {}).get('region_metadata', {})


def get_high_ticket_threshold() -> float:
    return float(load_categories().get('high_ticket_threshold_usd', 50))


def ingest_category_seeds() -> int:
    """把 categories.json 里的种子关键词注入 scan_queue,
    跳过已在 scan/learned 的, 优先级高的先入队.
    """
    cfg = load_categories()
    cats = sorted(cfg.get('categories', []), key=lambda c: c.get('priority', 5))

    # 已知集合
    known: set[str] = set()
    for f in (SCAN_FILE, LEARNED_FILE):
        if f.exists():
            for line in f.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                kw = line.lstrip('#').strip().split('|')[0].strip().lower()
                if kw:
                    known.add(kw)

    new_entries = []
    for cat in cats:
        cat_id = cat.get('id', '')
        for seed in cat.get('seeds', []):
            slug = seed.lower().strip()
            if slug and slug not in known:
                new_entries.append((seed, cat_id, cat.get('priority', 5)))
                known.add(slug)

    if not new_entries:
        return 0

    today = datetime.now().strftime('%Y-%m-%d')
    with open(SCAN_FILE, 'a', encoding='utf-8') as f:
        f.write(f"\n# === 类目种子入队 {today} ({len(new_entries)} 个) ===\n")
        # 按 priority 排序
        new_entries.sort(key=lambda x: x[2])
        for seed, cat_id, prio in new_entries:
            f.write(f"{seed}|待翻译|product|seed-{cat_id}-p{prio}\n")

    log.info(f"类目种子注入: {len(new_entries)} 个关键词")
    return len(new_entries)


def category_stats() -> list[dict]:
    """各类目当前扫描覆盖率 (SQLite scans 表 vs seeds)."""
    from .persistence import get_persistence
    cfg = load_categories()
    db = get_persistence()
    out = []
    with db._conn() as conn:
        for cat in cfg.get('categories', []):
            seeds = cat.get('seeds', [])
            if not seeds:
                continue
            placeholders = ','.join('?' * len(seeds))
            scanned = conn.execute(
                f"SELECT COUNT(DISTINCT keyword) FROM scans "
                f"WHERE LOWER(keyword) IN ({placeholders})",
                [s.lower() for s in seeds],
            ).fetchone()[0]
            out.append({
                'id': cat['id'], 'name': cat.get('name', cat['id']),
                'priority': cat.get('priority', 5),
                'seeds_total': len(seeds),
                'scanned': scanned,
                'coverage_pct': round(100 * scanned / len(seeds), 1) if seeds else 0,
            })
    return sorted(out, key=lambda x: (-x['priority'], -x['coverage_pct']))

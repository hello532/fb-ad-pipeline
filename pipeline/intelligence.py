"""intelligence.py — 跨品牌情报库 (v8: 流式增量 + SQLite 优先).

v8 变化:
- 不再在内存中蓄积 intel['brands'] 大字典
- 每个 _corrected.json 解析后立即调用 persistence.upsert_brand_incremental()
- orbit_score 从 SQL 聚合重算, 不依赖 Python 字典
- JSON 文件只在需要时从 SQLite 导出 (export_brands_json)
"""
import json
from datetime import datetime

from .paths import RESULTS_DIR, INTEL_FILE
from .domain import canonical_domain, extract_domain_from_url, resolve_domain

from core.logger import get_logger
log = get_logger(__name__)


def update_brand_intelligence() -> dict:
    """v8: 逐文件流式处理, 增量写入 SQLite, 不蓄积全量字典."""
    from .persistence import get_persistence
    db = get_persistence()

    corrected_files = sorted(RESULTS_DIR.glob('*_corrected.json'))
    ok_files = 0
    total_comps = 0

    for jf in corrected_files:
        if '汇总' in jf.name:
            continue
        try:
            data = json.loads(jf.read_text(encoding='utf-8'))
        except Exception:
            continue

        ok_files += 1
        kw = data.get('keyword', '')
        for comp in data.get('top_competitors', []):
            if not isinstance(comp, dict):
                continue
            try:
                db.upsert_brand_incremental(comp, kw)
                total_comps += 1
            except Exception as e:
                log.debug(f"upsert_brand_incremental failed: {e}")

    # 重算 orbit_score (SQL 聚合)
    db.recalc_orbit_scores()

    # 导出 JSON (兼容旧接口)
    intel = db.export_brands_json()
    with open(INTEL_FILE, 'wb') as f:
        f.write(json.dumps(intel, indent=2, ensure_ascii=False).encode('utf-8', errors='replace'))

    log.info(f"  ✅ 品牌情报: {len(intel['brands'])} 个品牌, "
             f"{len(intel['orbit_brands'])} 个轨道品牌 "
             f"(扫 {ok_files} 文件, {total_comps} 竞品)")

    return intel


def show_orbits():
    """v8: 从 SQLite 读轨道品牌 (不需要 JSON 文件)."""
    from .persistence import get_persistence
    db = get_persistence()
    orbits = db.list_orbit_brands_from_sql(min_appearance=3, limit=50)

    if not orbits:
        log.warning("暂无轨道品牌（需出现在3+次扫描中）")
        return

    log.info(f"\n{'='*80}")
    log.info(f"  轨道品牌 — 出现在多个关键词扫描中的品牌 ({len(orbits)} 个)")
    log.info(f"{'='*80}")
    for i, ob in enumerate(orbits, 1):
        kws = ob.get('keywords', '')
        if isinstance(kws, str):
            kws = kws.split(',')[:5]
        elif isinstance(kws, list):
            kws = kws[:5]
        else:
            kws = []
        log.info(f"\n  {i:2d}. {ob['name']:<25} ({ob['domain']})")
        log.info(f"      轨道评分: {ob['score']}/100  |  出现 {ob['scans']} 次扫描")
        log.info(f"      关键词: {', '.join(kws)}")
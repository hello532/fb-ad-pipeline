"""scan_instructions.py — extracted from fb-scan-pipeline-v7.py:1416-1472"""
import os
import re
import time as _time
from datetime import datetime
from pathlib import Path
from typing import List

from .paths import SCAN_FILE, RESULTS_DIR
from .js_script import EXTRACT_JS_V7

from core.logger import get_logger
log = get_logger(__name__)

# ── 扫描指引 ─────────────────────────────────────────────────────────────────

def generate_scan_instructions(keyword: str) -> str:
    encoded = keyword.replace(' ', '+')
    url = f"https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=US&q={encoded}&search_type=keyword_unordered"
    return f"""
=== 扫描指引: {keyword} ===

1. 打开链接（登录 Facebook 后访问）:
   {url}

2. 滚动页面加载至少 20-30 条广告

3. 打开开发者工具 (F12) → Console

4. 粘贴以下 JS v7 代码并回车:

{EXTRACT_JS_V7}

5. 复制输出的 JSON

6. 保存到: {RESULTS_DIR}/{datetime.now().strftime('%Y-%m-%d')}_{keyword.replace(' ', '-')}.json

7. 运行: python3 {Path(__file__).name} --process-raw
"""

# ── 队列加载器 ───────────────────────────────────────────────────────────────

# 若 N 天内同关键词已扫过, 本次入队时自动跳过 (节省预算, 避免重复)
DEDUP_DAYS = int(os.getenv('FB_DEDUP_DAYS', '7'))
# 去重后若队列偏小或跳过多, 从 learned_keywords 回填
BACKFILL_LIMIT = int(os.getenv('FB_BACKFILL_LIMIT', '300'))


def _normalize_kw(s: str) -> str:
    """精准去重的关键: 统一小写 + 去首尾空白 + 合并内部多空格 + 去首尾标点."""
    if not s: return ''
    s = s.lower().strip()
    s = re.sub(r'\s+', ' ', s)
    s = s.strip('-,.;:|/!?"\'`()[]{}')
    return s


def _all_scanned_set(days: int) -> set:
    """查询 scans 表返回归一化后的已扫集合.
    days>0 只取近 N 天; days=0 不去重; days<0 全历史永久去重."""
    if days == 0:
        return set()
    try:
        from .persistence import get_persistence
        db = get_persistence()
        with db._conn() as conn:
            if days > 0:
                rows = conn.execute(
                    "SELECT DISTINCT keyword FROM scans "
                    "WHERE scan_date >= date('now', '-' || ? || ' days')",
                    (days,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT DISTINCT keyword FROM scans").fetchall()
        return {_normalize_kw(r[0]) for r in rows if r and r[0]}
    except Exception as e:
        log.warning(f"DEDUP 查询失败: {e}")
        return set()


def _fetch_fresh_from_learned(limit: int, exclude: set) -> list[dict]:
    """
    智能回填: learned_keywords 未扫词, 用 productivity_score 排序 (兔子洞真智能).
    v9.4: 改用 Python-side 过滤 (避免 LEFT JOIN 24s 卡死), 全量取 learned 再内存去重.
    """
    try:
        from .persistence import get_persistence
        db = get_persistence()

        # 拿产能 token 表 (失败时降级 None, _score=固定 50)
        prod_tokens = {}
        try:
            from .keyword_evolution import productivity_tokens, productivity_score
            prod_tokens = productivity_tokens(min_score=80, lookback_days=90)
        except Exception:
            productivity_score = None  # type: ignore

        # v9.4: 先全量拉 learned_keywords (0.03s), 再 Python 内存过滤, 避免 LEFT JOIN 24s 卡死
        with db._conn() as conn:
            rows = conn.execute("""
                SELECT keyword, kw_type, subtype, source_kw, learned_date
                FROM learned_keywords
                WHERE (subtype IS NULL OR subtype NOT LIKE 'retired%')
                  AND LENGTH(keyword) BETWEEN 4 AND 80
                ORDER BY learned_date DESC
            """).fetchall()

        # 构建已扫集合 (0.01s)
        scanned_norms = _all_scanned_set(7)

        # 对每个候选用 productivity 再排一次, 只保留 TOP
        scored = []
        for r in rows:
            kw = (r[0] or '').strip()
            norm = _normalize_kw(kw)
            if not kw or norm in exclude or norm in scanned_norms or _is_junk_kw(kw):
                continue
            subtype = r[2] or ''
            source_kw = r[3] or ''
            # 优先级排序 (与原 SQL CASE 一致)
            if source_kw.startswith('manual:'):
                prio = 1
            elif 'suggest' in source_kw or 'amazon' in source_kw or 'google' in source_kw or 'multi-platform' in source_kw:
                prio = 2
            elif subtype == 'auto-harvest':
                prio = 3
            elif subtype in ('rh-l6','rh-l7','rh-l8','rh-l9','rh-l10','rh-l11'):
                prio = 4
            elif subtype.startswith('rh-l'):
                prio = 5
            else:
                prio = 6
            prod = 50
            if prod_tokens:
                try:
                    from .keyword_evolution import productivity_score as _ps
                    prod = _ps(kw, prod_tokens)
                except Exception:
                    pass
            scored.append({
                'keyword': kw, 'translation': '',
                'type': r[1] or 'auto',
                '_priority': prio,
                '_prod': prod,
                '_norm': norm,
            })
        # 按 priority ASC → prod DESC 排序 (高优先级 + 高产能排最前)
        scored.sort(key=lambda x: (x['_priority'], -x['_prod']))
        fresh = []
        for it in scored:
            fresh.append(it)
            exclude.add(it['_norm'])
            if len(fresh) >= limit:
                break
        return fresh
    except Exception as e:
        log.warning(f"BACKFILL 查询失败: {e}")
        return []


def _is_junk_kw(kw: str) -> bool:
    """识别文件名/URL/重复词根等垃圾词, 禁止入队."""
    k = kw.lower().strip()
    if 'http' in k or '://' in k: return True
    for ext in ('.xlsx','.xls','.csv','.txt','.json','.pdf','.html','.png','.jpg','.bak'):
        if k.endswith(ext) or f'{ext} ' in k: return True
    # 同 token 重复 (如 'pain pain')
    toks = k.split()
    if len(toks) >= 2 and len(set(toks)) < len(toks): return True
    return False


def _parse_queue_file(path: Path, priority: int, seen_norm: set, dedup_norm: set) -> tuple[list[dict], int]:
    """读单个队列文件, 返回 (items, skipped_dup)."""
    items = []
    skipped = 0
    if not path.exists():
        return items, skipped
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '已扫描' in line:
                continue
            parts = line.split('|')
            kw = parts[0].strip()
            norm = _normalize_kw(kw)
            if not kw or norm in seen_norm or _is_junk_kw(kw):
                continue
            if norm in dedup_norm:
                skipped += 1
                continue
            seen_norm.add(norm)
            items.append({
                'keyword':     kw,
                'translation': parts[1].strip() if len(parts) > 1 else '',
                'type':        parts[2].strip() if len(parts) > 2 else '',
                '_priority':   priority,
            })
    return items, skipped


# ── 轻量缓存: 60 秒内重复调用直接返回, 避免 /queue 被轮询时刷日志 ─────────────
_cache = {'ts': 0.0, 'items': None}
_CACHE_TTL = 600.0   # 秒 (v9.2: 60→600, 避免运维 tab 每次冷启动 4s)

# 是否加载历史 scan_queue_*.txt 文件 (默认关闭, 只从 DB 取未扫词, 零 skip 冗余)
INCLUDE_LEGACY_FILES = os.getenv('FB_INCLUDE_LEGACY', '0') == '1'


def _cleanup_legacy_queue_files(days: int = 14) -> int:
    """清理 14 天前的老队列文件 (除 manual), 避免文件夹堆积."""
    cutoff = _time.time() - days * 86400
    removed = 0
    for f in RESULTS_DIR.glob('scan_queue_*.txt'):
        if f.name.startswith('scan_queue_manual_'):
            continue   # 手动导入永不删
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        log.info(f"清理过期队列文件: {removed} 个 (>{days}天)")
    return removed


def scan_queue_loader() -> List[dict]:
    """
    重构的智能队列加载器 (完全基于 DB, 零 skip 冗余):
      P1: scan_queue_manual_*.txt        — 用户手动导入, 最高优先
      P2: learned_keywords WHERE 未扫    — 按产能排序直接取, 不 skip 冗余

    对比旧版:
      × 旧: 读 10+ 个 scan_queue_*.txt 文件, 其中一半都已扫过, 每次刷日志
      √ 新: SQL 一次直接查未扫词, 无文件解析, 无 skip 日志噪音
    """
    now = _time.time()
    if _cache['items'] is not None and (now - _cache['ts']) < _CACHE_TTL:
        return _cache['items']

    seen_norm = set()
    dedup_norm = _all_scanned_set(DEDUP_DAYS)
    queued_items: list[dict] = []
    total_skipped = 0

    # P1: 仅加载用户手动导入 (最近 7 天, 太老的过期)
    cutoff_ts = now - 7 * 86400
    manual_files = sorted(RESULTS_DIR.glob('scan_queue_manual_*.txt'), reverse=True)
    for f in manual_files:
        try:
            if f.stat().st_mtime < cutoff_ts:
                continue
        except Exception:
            continue
        it, sk = _parse_queue_file(f, priority=1, seen_norm=seen_norm, dedup_norm=dedup_norm)
        queued_items.extend(it); total_skipped += sk

    # P1.5 (可选, 默认关闭): 加载其他 legacy queue 文件, 仅用于兼容场景
    if INCLUDE_LEGACY_FILES:
        for f in sorted(RESULTS_DIR.glob('scan_queue_*.txt'), reverse=True):
            if f.name.startswith('scan_queue_manual_'):
                continue
            it, sk = _parse_queue_file(f, priority=4, seen_norm=seen_norm, dedup_norm=dedup_norm)
            queued_items.extend(it); total_skipped += sk
        it, sk = _parse_queue_file(SCAN_FILE, priority=5, seen_norm=seen_norm, dedup_norm=dedup_norm)
        queued_items.extend(it); total_skipped += sk

    # P2: 从 learned_keywords 拉未扫词 (SQL JOIN 直接排除已扫, 无 skip)
    # 每次拉 BACKFILL_LIMIT × 2 作为候选池, 产能排序后取前 N
    fresh_target = max(BACKFILL_LIMIT, 400) - len(queued_items)
    if fresh_target > 0:
        fresh = _fetch_fresh_from_learned(
            limit=fresh_target,
            exclude=set(seen_norm) | set(dedup_norm),
        )
        if fresh:
            queued_items.extend(fresh)

    queued_items.sort(key=lambda x: x.get('_priority', 9))

    # 日志精简: 只在变化时 log, skip_dup 是手动文件里的才报
    p1_cnt = sum(1 for i in queued_items if i.get('_priority')==1)
    p3_cnt = sum(1 for i in queued_items if i.get('_priority')==3)
    log.info(f"队列加载: {len(queued_items)} 个 (P1手动={p1_cnt} P2智能={p3_cnt})"
             + (f" · 手动文件 skip={total_skipped}" if total_skipped else ""))

    _cache['ts'] = now
    _cache['items'] = queued_items
    return queued_items


# 启动时清理一次老文件 (不影响主流程失败也无所谓)
try:
    _cleanup_legacy_queue_files(days=14)
except Exception:
    pass

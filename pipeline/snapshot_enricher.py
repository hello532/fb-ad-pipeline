"""pipeline/snapshot_enricher.py — 详情页 enrich pipeline (v9.5).

对 GO_FOLLOW / score>=70 的 brand 的 active 广告, 访问 ad_snapshot_url 拿真值:
- video_duration_sec (真时长, 不是兜底)
- 完整 body (而非 link_description fallback)
- 真 publisher_platforms (FB/IG/Messenger/Threads/AN)
- cards 完整 (carousel 每张卡的 title/body/cta/link)
- aaa_info (advertiser identifiers)
- 部分 EU/political 广告还能拿 spend/regional_distribution

每个 ad ~3-5s. 用同一套 graphql_sniffer + resource blocker, 跑详情页 URL.

API:
    enrich_one_ad(ad_archive_id, country='US', headless=True) -> dict | None
    enrich_brand(domain, max_ads=10) -> {ok, fail, fields_filled}

挂在 ProcessPool 工作队列, 主流程不阻塞.
"""
from __future__ import annotations

import json
import random
import time
from typing import Optional

from core.logger import get_logger

from .scraper import (USER_AGENTS, VIEWPORTS, TIMEZONES, ANONYMOUS_MODE,
                       _attach_resource_blocker, _human_mouse_move,
                       _load_state, _can_scrape_now, _save_state,
                       FB_BLOCK_RESOURCES)
from .js_script import EXTRACT_JS_V7
from .graphql_sniffer import install_sniffer, merge_dom_with_graphql

log = get_logger(__name__)


SNAPSHOT_URL_FMT = 'https://www.facebook.com/ads/library/?id={aid}'


def enrich_one_ad(ad_archive_id: str,
                  country: str = 'US',
                  headless: bool = True,
                  timeout_sec: int = 25) -> Optional[dict]:
    """访问单广告详情页, 返回富 ad dict (None 表示失败).

    流程跟 scrape_keyword 同构:
    1. 启动 anonymous browser + resource blocker
    2. 安装 graphql sniffer (会捕获 AdLibraryAdDetailsV2Query 的真值)
    3. goto detail URL
    4. 短停 + 单次轻滚 (触发 lazy-load)
    5. evaluate EXTRACT_JS (DOM regex 兜底)
    6. merge graphql + dom → 返回
    """
    aid = (ad_archive_id or '').strip()
    if not aid or not aid.isdigit():
        return None

    # 检查熔断 — 跟主 scrape 共用 state file
    s = _load_state()
    can_scrape, why = _can_scrape_now(s)
    if not can_scrape and 'cooldown' in why.lower():
        log.info(f"snapshot_enrich: skipped {aid} ({why})")
        return None

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    url = SNAPSHOT_URL_FMT.format(aid=aid)
    ua = random.choice(USER_AGENTS)
    viewport = random.choice(VIEWPORTS)
    tz = random.choice(TIMEZONES)

    payload = None
    graphql_ads_by_id: dict = {}
    graphql_meta = {'graphql_count': 0, 'ad_responses': 0, 'unique_ads': 0, 'errors': 0}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        ctx = browser.new_context(
            viewport=viewport, user_agent=ua,
            locale="en-US", timezone_id=tz,
        )
        _attach_resource_blocker(ctx)
        page = ctx.new_page()
        try:
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        except Exception:
            pass

        sniffer_state = install_sniffer(page)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_sec * 1000)
        except PWTimeout:
            log.info(f"snapshot_enrich timeout: {aid}")
            ctx.close(); browser.close()
            return None

        # 详情页关键: 等 GraphQL detail query 触发, 它在 onLoad 后就 fire
        time.sleep(random.uniform(0.6, 1.2))
        try:
            page.wait_for_load_state('networkidle', timeout=5000)
        except Exception:
            pass

        # 轻滚一下让 lazy-loaded 部分 (cards/regional 块) 加载
        try:
            page.evaluate("window.scrollBy(0, 600)")
        except Exception:
            pass
        time.sleep(random.uniform(0.4, 0.8))

        # DOM 兜底提取
        try:
            payload = page.evaluate(EXTRACT_JS_V7)
        except Exception as e:
            err = str(e)
            if 'destroyed' in err.lower() or 'navigation' in err.lower():
                try:
                    time.sleep(0.8)
                    page.wait_for_load_state('domcontentloaded', timeout=3000)
                    payload = page.evaluate(EXTRACT_JS_V7)
                except Exception:
                    payload = None
            else:
                payload = None

        # 鼠标轻动后关闭
        try:
            _human_mouse_move(page, random.randint(400, 900), random.randint(200, 400), steps=4)
        except Exception:
            pass

        graphql_ads_by_id = dict(sniffer_state.get('ads') or {})
        graphql_meta = {
            'graphql_count': sniffer_state.get('graphql_count', 0),
            'ad_responses': sniffer_state.get('ad_responses', 0),
            'unique_ads': len(graphql_ads_by_id),
            'errors': sniffer_state.get('errors', 0),
        }
        ctx.close()
        browser.close()

    # parse DOM
    data: dict = {}
    if payload:
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    results = data.get('results') if isinstance(data, dict) else []
    if not isinstance(results, list):
        results = []

    # merge with graphql
    if graphql_ads_by_id:
        results = merge_dom_with_graphql(results, graphql_ads_by_id)

    # 找匹配 aid 的 ad
    target = None
    for r in results:
        if isinstance(r, dict) and (r.get('ad_archive_id') or '') == aid:
            target = r
            break
    if not target and results:
        # 详情页通常只 1 条, 直接拿第一条
        target = results[0]

    if not target:
        log.debug(f"snapshot_enrich: no ad data for {aid} "
                  f"(graphql_count={graphql_meta['graphql_count']})")
        return None

    target['_enrich_source'] = 'snapshot+graphql' if graphql_ads_by_id else 'snapshot+dom'
    target['_graphql_meta'] = graphql_meta
    return target


# ── DB upsert helpers ──────────────────────────────────────────────────────

def _ensure_enrich_columns(conn) -> None:
    """加 enriched_at + enrich_source 列 (idempotent)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ad_archive)").fetchall()}
    if 'enriched_at' not in cols:
        conn.execute("ALTER TABLE ad_archive ADD COLUMN enriched_at TEXT")
    if 'enrich_source' not in cols:
        conn.execute("ALTER TABLE ad_archive ADD COLUMN enrich_source TEXT")
    conn.commit()


def upsert_enriched(conn, aid: str, enriched: dict) -> dict:
    """把 enriched ad 字段 merge 进 ad_archive 的对应行. 只填空字段, 不覆盖.
    返回 {'updated': bool, 'fields_filled': [...] }
    """
    if not aid or not enriched:
        return {'updated': False, 'fields_filled': []}

    from datetime import datetime
    now_iso = datetime.now().isoformat(timespec='seconds')

    # 拉当前行 (named-column query, 自己 zip 成 dict — 不依赖 conn.row_factory)
    columns = ['body', 'cards', 'cards_count', 'video_duration_sec', 'video_url',
               'publisher_platforms', 'ig_handle', 'ig_followers',
               'regional_distribution', 'demographic_distribution',
               'eu_total_reach', 'spend_lower', 'spend_upper',
               'imp_lower', 'imp_upper', 'currency', 'target_ages', 'target_genders',
               'target_locations', 'languages', 'byline', 'page_likes']
    raw = conn.execute(
        f"SELECT {', '.join(columns)} FROM ad_archive WHERE ad_archive_id=?",
        (aid,)
    ).fetchone()
    if not raw:
        return {'updated': False, 'fields_filled': []}
    cur = dict(zip(columns, raw))

    def _empty(v):
        return v is None or v == '' or v == 0 or v == '[]'

    filled: list = []
    sets: list = []
    params: list = []

    def _set(col, new_val):
        sets.append(f"{col}=COALESCE(?, {col})")
        params.append(new_val)
        filled.append(col)

    # body — enriched 真值优先 (只在原 body 短于 enriched 时替换)
    e_body = (enriched.get('body') or '').strip()
    cur_body = (cur.get('body') or '').strip()
    if e_body and len(e_body) > len(cur_body):
        _set('body', e_body[:5000])

    # video_duration_sec
    e_dur = enriched.get('video_duration_sec')
    if e_dur and not cur.get('video_duration_sec'):
        try:
            sets.append("video_duration_sec=?"); params.append(float(e_dur)); filled.append('video_duration_sec')
        except Exception:
            pass

    # cards
    e_cards = enriched.get('cards') or []
    if isinstance(e_cards, list) and len(e_cards) > 0:
        # 真 cards (非 inferred) 强覆盖
        if not all(c.get('_inferred') for c in e_cards if isinstance(c, dict)):
            _set('cards', json.dumps(e_cards, ensure_ascii=False)[:50000])
            sets.append("cards_count=?"); params.append(len(e_cards)); filled.append('cards_count')

    # publisher_platforms — 真值 (graphql 来源) 强覆盖兜底
    e_plats = enriched.get('publisher_platforms') or []
    if isinstance(e_plats, list) and e_plats and enriched.get('_enrich_source', '').startswith('snapshot+graphql'):
        _set('publisher_platforms', json.dumps(e_plats, ensure_ascii=False))

    # ig_handle / ig_followers — 真值优先
    e_ig = (enriched.get('ig_handle') or '').strip()
    if e_ig and (not cur.get('ig_handle') or len(e_ig) > 0):
        _set('ig_handle', e_ig)
    e_igf = enriched.get('ig_followers') or 0
    try:
        e_igf = int(e_igf)
    except Exception:
        e_igf = 0
    if e_igf > 0 and (cur.get('ig_followers') or 0) == 0:
        sets.append("ig_followers=?"); params.append(e_igf); filled.append('ig_followers')

    # regional / demographic — 只有 EU/political ad 才有
    rd = enriched.get('regional_distribution') or []
    if rd:
        _set('regional_distribution', json.dumps(rd, ensure_ascii=False)[:50000])
    dd = enriched.get('demographic_distribution') or []
    if dd:
        _set('demographic_distribution', json.dumps(dd, ensure_ascii=False)[:50000])

    eu_reach = enriched.get('eu_total_reach')
    if eu_reach:
        try:
            sets.append("eu_total_reach=?"); params.append(int(eu_reach)); filled.append('eu_total_reach')
        except Exception:
            pass

    # spend / impressions
    sp = enriched.get('spend') if isinstance(enriched.get('spend'), dict) else {}
    if sp.get('lower') and not cur.get('spend_lower'):
        sets.append("spend_lower=?"); params.append(sp['lower']); filled.append('spend_lower')
    if sp.get('upper') and not cur.get('spend_upper'):
        sets.append("spend_upper=?"); params.append(sp['upper']); filled.append('spend_upper')

    imp = enriched.get('impressions') if isinstance(enriched.get('impressions'), dict) else {}
    if imp.get('lower') and not cur.get('imp_lower'):
        sets.append("imp_lower=?"); params.append(imp['lower']); filled.append('imp_lower')
    if imp.get('upper') and not cur.get('imp_upper'):
        sets.append("imp_upper=?"); params.append(imp['upper']); filled.append('imp_upper')

    # 总是更新 enriched_at + enrich_source
    sets.append("enriched_at=?"); params.append(now_iso)
    sets.append("enrich_source=?"); params.append(enriched.get('_enrich_source', 'snapshot'))
    sets.append("updated_at=?"); params.append(now_iso)

    if not sets:
        return {'updated': False, 'fields_filled': []}

    params.append(aid)
    conn.execute(f"UPDATE ad_archive SET {', '.join(sets)} WHERE ad_archive_id=?", params)
    return {'updated': True, 'fields_filled': sorted(set(filled))}


# ── Batch enrich ───────────────────────────────────────────────────────────

def enrich_brand_active_ads(domain: str,
                              limit: int = 10,
                              min_score: int = 60,
                              skip_already_enriched: bool = True,
                              delay_sec: tuple = (3.0, 6.0)) -> dict:
    """对单个品牌的 active 广告做详情页 enrich.

    - 拉 ad_archive 中该 domain 的 is_active=1 active_days DESC 排序
    - 跳过最近 7 天已 enrich 过的
    - 每条 ad ~4s + delay_sec 间隔
    - 返回 {ok, fail, total, fields_filled_total}
    """
    from .persistence import get_persistence
    db = get_persistence()
    # BUGFIX 2026-05-05: hold cm in local var (was leaked __enter__()).
    cm = db._conn()
    conn = cm.__enter__()
    try:
        _ensure_enrich_columns(conn)

        sql = ("SELECT ad_archive_id FROM ad_archive "
               "WHERE LOWER(domain)=LOWER(?) AND is_active=1 ")
        if skip_already_enriched:
            sql += " AND (enriched_at IS NULL OR enriched_at < datetime('now', '-7 days')) "
        sql += " ORDER BY active_days DESC LIMIT ?"
        rows = [dict(r) for r in conn.execute(sql, (domain, limit)).fetchall()]

        ok = fail = 0
        fields_total: dict = {}
        for r in rows:
            aid = r['ad_archive_id']
            try:
                enriched = enrich_one_ad(aid)
                if not enriched:
                    fail += 1
                    continue
                res = upsert_enriched(conn, aid, enriched)
                conn.commit()
                if res.get('updated'):
                    ok += 1
                    for f in res.get('fields_filled', []):
                        fields_total[f] = fields_total.get(f, 0) + 1
                else:
                    fail += 1
            except Exception as e:
                log.warning(f"enrich {aid} failed: {e}")
                fail += 1
            # delay
            time.sleep(random.uniform(*delay_sec))

        return {
            'domain': domain,
            'attempted': len(rows),
            'ok': ok,
            'fail': fail,
            'fields_filled': fields_total,
        }
    finally:
        cm.__exit__(None, None, None)


def enrich_top_brands(min_score: int = 70,
                       max_brands: int = 20,
                       max_ads_per_brand: int = 5,
                       days: int = 30) -> dict:
    """全局 enrich orchestrator — 按老兵 score DESC 取 top brands, 每个 enrich 前 N active ads.

    一次跑完 60-90 个 ads 大约 5-10 min. 推荐每天跑 1-2 次.
    """
    from .veteran_signals import veteran_flags
    flags = veteran_flags(days=days, limit=max_brands * 2, only_active=True,
                           verdict='GO_FOLLOW')
    brands = [b for b in flags.get('brands', []) if b.get('score', 0) >= min_score]
    brands = brands[:max_brands]

    log.info(f"enrich_top_brands: {len(brands)} brands eligible "
             f"(score >= {min_score}, days={days})")

    summary = {
        'started_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'brands_total': len(brands),
        'ads_ok': 0,
        'ads_fail': 0,
        'fields_filled': {},
        'per_brand': [],
    }
    for b in brands:
        domain = b['domain']
        log.info(f"  enriching {domain} (score={b.get('score')})")
        r = enrich_brand_active_ads(domain, limit=max_ads_per_brand)
        summary['ads_ok'] += r['ok']
        summary['ads_fail'] += r['fail']
        for f, n in r.get('fields_filled', {}).items():
            summary['fields_filled'][f] = summary['fields_filled'].get(f, 0) + n
        summary['per_brand'].append({
            'domain': domain, 'score': b.get('score'),
            'attempted': r['attempted'], 'ok': r['ok'], 'fail': r['fail'],
        })
    summary['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    return summary

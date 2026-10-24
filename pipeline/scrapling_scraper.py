"""pipeline/scrapling_scraper.py — Scrapling 三层抓取引擎 (v2).

三层组合实现全自动快速抓取, 完全不需要 API / TOKEN:

  Layer 0 — Fetcher 快筛 (2s/词):
    curl_cffi + anti-detect TLS 指纹 → 静态 HTML
    零命中 → 直接跳过, 不触发更重的层
    有命中 → 进入 Layer 1

  Layer 1 — StealthyFetcher 深抓 (5-7s/词):
    Patchright (魔改 Chromium) → JS 渲染 + 滚动加载
    拿到 95% 字段: page_name, spend, cta, link_url, body,
    video_hd_url, original_image_url, title, display_format 等
    仅缺: ad_snapshot_url, publisher_platforms, instagram_actor_name (低价值)
    → 对大多数场景已足够, 直接输出 JSON, 不需要 Playwright

  Layer 2 — Playwright (15s/词, 仅 fallback):
    仅当 StealthyFetcher 失败或需要特定缺失字段时才回退
    拿到 100% 字段 + GraphQL sniffer 数据

env 控制:
  FB_SCRAPLING_MODE=full   三层全开 (默认)
  FB_SCRAPLING_MODE=quick  仅 Layer 0 快筛 + Playwright
  FB_SCRAPLING_MODE=off    完全关闭, 纯 Playwright
  FB_MAX_PAGES             翻页上限 (默认 1=仅首页, 5=约 125-150 条)
"""
from __future__ import annotations

import json
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.logger import get_logger

from .paths import RESULTS_DIR
from .commerce_filter import filter_commerce_ads
from .proxy_pool import get_pool, classify_error, proxy_for_playwright, ERROR_PROXY_CONNECTION, ROTATE_PER_SCRAPE
from .runtime import proxy_label

log = get_logger(__name__)

SCRAPLING_MODE = os.getenv("FB_SCRAPLING_MODE", "full").lower()
MAX_PAGES = int(os.getenv("FB_MAX_PAGES", "1"))
L1_INITIAL_WAIT_MS = max(250, int(os.getenv("FB_L1_INITIAL_WAIT_MS", "750")))
L1_SCROLL_WAIT_MIN_MS = max(200, int(os.getenv("FB_L1_SCROLL_WAIT_MIN_MS", "450")))
L1_SCROLL_WAIT_JITTER_MS = max(0, int(os.getenv("FB_L1_SCROLL_WAIT_JITTER_MS", "250")))
L1_NAV_TIMEOUT_MS = max(8_000, int(os.getenv("FB_L1_NAV_TIMEOUT_MS", "20_000")))
L0_DIRECT_SAVE_MIN_ADS = max(0, int(os.getenv("FB_L0_DIRECT_SAVE_MIN_ADS", "20")))
L15_FIRST_RENDER_MIN_SEC = max(0.1, float(os.getenv("FB_L15_FIRST_RENDER_MIN_SEC", "0.35")))
L15_FIRST_RENDER_MAX_SEC = max(L15_FIRST_RENDER_MIN_SEC, float(os.getenv("FB_L15_FIRST_RENDER_MAX_SEC", "0.9")))

# v10: thread-local browser pool — keep patchright + browser alive across keywords
# inside one worker thread. Saves ~2s/keyword launch overhead. context is still
# rebuilt per keyword (fresh fingerprint, fresh cookies). Browser retires after
# BROWSER_MAX_USES keywords to refresh fingerprint cohort.
BROWSER_POOL_ENABLED = os.getenv("FB_BROWSER_POOL", "1") != "0"
BROWSER_MAX_USES = int(os.getenv("FB_BROWSER_MAX_USES", "1"))

import threading as _threading
_THREAD_LOCAL = _threading.local()


def _retire_thread_browser() -> None:
    """Tear down this thread's cached browser + patchright instance, if any."""
    state = getattr(_THREAD_LOCAL, "patchright_state", None)
    if not state:
        return
    try:
        state["browser"].close()
    except Exception:
        pass
    try:
        state["pw_instance"].stop()
    except Exception:
        pass
    _THREAD_LOCAL.patchright_state = None


def _get_thread_browser(headless: bool) -> Optional[dict]:
    """Return {pw_instance, browser, uses, headless} for this thread, lazy-init.

    Returns None if patchright is not installed or if headless flag changes
    (we retire and let caller take cold path)."""
    if not BROWSER_POOL_ENABLED:
        return None
    if ROTATE_PER_SCRAPE:
        _retire_thread_browser()
    state = getattr(_THREAD_LOCAL, "patchright_state", None)
    if state:
        # Headless toggle invalidates cache — retire and rebuild.
        if state["headless"] != headless:
            _retire_thread_browser()
            state = None
    if state and state["uses"] >= BROWSER_MAX_USES:
        _retire_thread_browser()
        state = None
    if state:
        return state
    try:
        from patchright.sync_api import sync_playwright as sync_patchright
    except ImportError:
        return None
    try:
        pw = sync_patchright().start()
        launch_kwargs = dict(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        pool = get_pool()
        proxy_url = pool.acquire()
        pw_proxy = proxy_for_playwright(proxy_url)
        if pw_proxy:
            launch_kwargs["proxy"] = pw_proxy
        browser = pw.chromium.launch(**launch_kwargs)
    except Exception as e:
        log.warning(f"  browser pool 启动失败, 回退冷路径: {e}")
        return None
    state = {"pw_instance": pw, "browser": browser, "uses": 0, "headless": headless}
    _THREAD_LOCAL.patchright_state = state
    return state


def _build_url(keyword: str, country: str = "US") -> str:
    return (
        f"https://www.facebook.com/ads/library/"
        f"?active_status=active&ad_type=all&search_type=keyword_unordered"
        f"&country={country}&q={keyword.replace(' ', '+')}"
    )


def _wait_for_first_render_signal(sniffer_state: dict) -> None:
    deadline = time.perf_counter() + L15_FIRST_RENDER_MAX_SEC
    time.sleep(L15_FIRST_RENDER_MIN_SEC)
    while time.perf_counter() < deadline:
        if (sniffer_state.get("graphql_count") or sniffer_state.get("ad_responses")
                or sniffer_state.get("ads")):
            return
        time.sleep(0.1)


def _decode_body(body) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    return body or ""


def _is_challenge(body: str) -> bool:
    lower = body[:5000].lower()
    return any(m in lower for m in (
        "temporarily restricted", "unusual activity", "too many requests",
        "rate limit", "unusual_activity", "suspicious",
    ))


# ── 正则提取器 (Fetcher / StealthyFetcher 共用) ────────────────────────

def _parse_static_ads(body: str) -> list[dict]:
    """从 FB Ad Library HTML 中提取广告预渲染数据.

    FB 的 SPA 把 GraphQL 响应 JSON 嵌入 <script> 标签,
    用 ad_archive_id 为锚点, 向前后扩展提取字段.
    """
    results: list[dict] = []
    seen_ids: set[str] = set()

    for m in re.finditer(r'"ad_archive_id"\s*:\s*"(\d+)"', body):
        aid = m.group(1)
        if aid in seen_ids or len(aid) < 8:
            continue
        seen_ids.add(aid)

        st = max(0, m.start() - 12000)
        en = min(len(body), m.end() + 60000)
        c = body[st:en]

        # v10: second-pass search in wider region for high-value fields
        # that may fall outside the 72KB context window
        wide_st = max(0, m.start() - 50000)
        wide_en = min(len(body), m.end() + 150000)
        w = body[wide_st:wide_en]

        def g(k: str) -> str:
            x = re.search(rf'"{re.escape(k)}"\s*:\s*"((?:[^"\\]|\\.)*)"', c)
            if not x:
                # v10: fallback to wider region
                x = re.search(rf'"{re.escape(k)}"\s*:\s*"((?:[^"\\]|\\.)*)"', w)
            if not x:
                return ""
            v = x.group(1)
            try:
                v = re.sub(r'\\u([0-9a-fA-F]{4})', lambda q: chr(int(q.group(1), 16)), v)
            except Exception:
                pass
            return v[:2000]

        def gi(k: str) -> Optional[int]:
            x = re.search(rf'"{re.escape(k)}"\s*:\s*(\d+)', c)
            if not x:
                x = re.search(rf'"{re.escape(k)}"\s*:\s*(\d+)', w)
            return int(x.group(1)) if x else None

        def ga(k: str) -> list[str]:
            x = re.search(rf'"{re.escape(k)}"\s*:\s*\[([^\]]*?)\]', c)
            if not x:
                x = re.search(rf'"{re.escape(k)}"\s*:\s*\[([^\]]*?)\]', w)
            if not x:
                return []
            return [v.strip().strip('"') for v in x.group(1).split(",") if v.strip().strip('"')]

        def go(k: str) -> Optional[dict]:
            x = re.search(
                rf'"{re.escape(k)}"\s*:\s*\{{[^}}]*?"lower_bound"\s*:\s*"(\d+)"[^}}]*?"upper_bound"\s*:\s*"(\d+)"',
                c,
            )
            return {"lower": int(x.group(1)), "upper": int(x.group(2))} if x else None

        def gDateIso(k: str) -> str:
            iv = gi(k)
            if iv and iv > 1_000_000:
                try:
                    from datetime import datetime as _dt
                    return _dt.utcfromtimestamp(iv).strftime("%Y-%m-%d")
                except Exception:
                    pass
            return g(k)

        # 提取所有 image/video URLs (carousel)
        def gAllUrls(key_pattern: str) -> list[str]:
            re_url = re.compile(rf'"({key_pattern})"\s*:\s*"(https?:[^"]+)"')
            urls: list[str] = []
            seen: set[str] = set()
            for um in re_url.finditer(c):
                u = um.group(2)
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
            return urls[:10]

        # 平衡花括号匹配 — 子对象
        def gObjBalanced(k: str) -> str:
            sm = re.search(rf'"{re.escape(k)}"\s*:\s*[\{{\[]', c)
            if not sm:
                return ""
            start = sm.start() + len(sm.group(0)) - 1
            open_ch = c[start]
            close_ch = '}' if open_ch == '{' else ']'
            depth = 0
            in_str = False
            esc = False
            for i in range(start, min(len(c), start + 12000)):
                ch = c[i]
                if esc:
                    esc = False
                    continue
                if ch == '\\':
                    esc = True
                    continue
                if ch == '"':
                    in_str = not in_str
                    continue
                if in_str:
                    continue
                if ch == open_ch:
                    depth += 1
                elif ch == close_ch:
                    depth -= 1
                    if depth == 0:
                        return c[start:i + 1]
            return ""

        results.append({
            "ad_archive_id": aid,
            "page_name": g("page_name") or "Unknown Brand",
            "page_id": g("page_id") or "",
            "page_profile_uri": g("page_profile_uri") or "",
            "page_profile_picture_url": g("page_profile_picture_url") or "",
            "page_like_count": gi("page_like_count") or 0,
            "page_categories": ga("page_categories"),
            "currency": g("currency") or "",
            "spend": go("spend"),
            "impressions": go("impressions"),
            "start_date": gi("start_date") or 0,
            "start_date_iso": gDateIso("start_date"),
            "end_date": gi("end_date") or gi("ad_delivery_stop_time") or 0,
            "is_active": '"is_active":true' in c or '"ad_delivery_status":"active"' in c,
            "cta_type": g("cta_type") or "",
            "cta_text": g("cta_text") or "",
            "link_url": g("link_url") or "",
            "link_description": g("link_description") or "",
            "link_caption": g("link_caption") or g("caption") or "",
            "title": g("title") or "",
            "body": g("body")[:3000],
            "byline": g("byline") or "",
            "ad_snapshot_url": g("ad_snapshot_url") or g("snapshot_url") or "",
            "collation_count": gi("collation_count") or 1,
            "display_format": g("display_format") or "",
            "archive_type": g("archive_type") or "",
            "ig_handle": g("instagram_actor_name") or g("ig_handle") or g("ig_username") or "",
            "ig_followers": gi("instagram_followers") or gi("ig_followers") or 0,
            "image_url": g("original_image_url") or g("resized_image_url") or "",
            "video_url": g("video_hd_url") or g("video_sd_url") or "",
            "video_duration_sec": gi("video_duration_sec"),
            "carousel_images": gAllUrls("original_image_url|resized_image_url"),
            "carousel_videos": gAllUrls("video_hd_url|video_sd_url"),
            "publisher_platforms": ga("publisher_platforms") or ga("publisher_platform"),
            "target_ages": g("target_ages") or "",
            "target_genders": g("target_genders") or "",
            "target_locations": ga("target_locations"),
            "languages": ga("languages"),
            "funding_entity": g("funding_entity") or "",
            "disclaimer_label": g("disclaimer_label") or g("funding_entity") or "",
            "regional_distribution_raw": gObjBalanced("regional_distribution"),
            "demographic_distribution_raw": gObjBalanced("age_country_gender_reach_breakdown") or gObjBalanced("demographic_distribution"),
            "eu_total_reach": gi("eu_total_reach"),
            "eu_political_ad": '"is_political_ad":true' in c or '"eu_political_ad":true' in c,
            "_source": "scrapling",
        })

    return results


# ── Layer 0: Fetcher 快筛 ──────────────────────────────────────────────────

def quick_scan(keyword: str, country: str = "US") -> Optional[dict]:
    """Fetcher (curl_cffi) 快筛: 2s/词, 判断有无结果."""
    try:
        from scrapling import Fetcher
    except ImportError:
        return None

    url = _build_url(keyword, country)
    t0 = time.perf_counter()

    pool = get_pool()
    resp = None
    used_proxy = None
    last_error = None
    for proxy_url in pool.acquire_sequence():
        try:
            fetch_kwargs = {'url': url}
            if proxy_url:
                fetch_kwargs['proxy'] = proxy_url
            resp = Fetcher.get(**fetch_kwargs)
            used_proxy = proxy_url
            pool.report_success(proxy_url)
            break
        except Exception as e:
            last_error = e
            err_type = classify_error(e)
            pool.report_failure(proxy_url, err_type)
            if err_type == ERROR_PROXY_CONNECTION and proxy_url is not None:
                log.warning(f"  L0 Fetcher 代理失败 ({proxy_label(proxy_url)}), 切换: {e}")
                pool.report_switch(proxy_url, None)
                continue
            log.warning(f"  L0 Fetcher 请求失败 ({proxy_label(proxy_url)}): {e}")
            return None
    if resp is None:
        log.warning(f"  L0 Fetcher 全部线路失败: {last_error}")
        return None

    elapsed = time.perf_counter() - t0
    status = getattr(resp, "status", 0)
    body = _decode_body(resp.body)

    if status != 200:
        log.warning(f"  L0 Fetcher HTTP {status}")
        return None

    if _is_challenge(body):
        log.warning(f"  L0 Fetcher 风控检测")
        return {"keyword": keyword, "count": 0, "ads": [], "elapsed_sec": elapsed,
                "source": "L0-fetcher", "challenge": True}

    ads = _parse_static_ads(body)
    log.info(f"  L0 Fetcher [{status}] {keyword}: {len(ads)} ads, {elapsed:.1f}s via {proxy_label(used_proxy)}")
    return {"keyword": keyword, "count": len(ads), "ads": ads,
            "elapsed_sec": round(elapsed, 1), "source": "L0-fetcher"}


# ── Layer 1: StealthyFetcher 深抓 ──────────────────────────────────────────

def stealthy_scan(
    keyword: str,
    country: str = "US",
    headless: bool = True,
    max_scrolls: int = 3,
    max_pages: int = 1,
) -> Optional[dict]:
    """StealthyFetcher (Patchright Chromium) 深抓: 5-7s/词.

    自带反检测 + JS 渲染 + 滚动加载, 拿到 95% 字段.
    max_pages=1 (默认): 仅首页, 与旧行为一致.
    max_pages>1: 建议用 _patchright_paginate() 替代 (更可靠翻页).
    """
    try:
        from scrapling import StealthyFetcher
    except ImportError:
        log.warning("scrapling[all] 未安装 (缺 Patchright), 跳过 StealthyFetcher")
        return None

    url = _build_url(keyword, country)
    t0 = time.perf_counter()

    # 构建 page_actions: 滚动加载更多内容
    actions: list[dict] = [{"wait": L1_INITIAL_WAIT_MS}]
    wait_jitter = int(L1_SCROLL_WAIT_JITTER_MS * (abs(hash(keyword)) % 3) / 2)
    for _ in range(max_scrolls):
        actions.append({"scroll": True})
        actions.append({"wait": L1_SCROLL_WAIT_MIN_MS + wait_jitter})

    pool = get_pool()
    resp = None
    used_proxy = None
    last_error = None
    for proxy_url in pool.acquire_sequence():
        try:
            fetch_kwargs = dict(
                url=url,
                headless=headless,
                wait_selector="body",
                page_actions=actions,
                wait_until="domcontentloaded",
                timeout=L1_NAV_TIMEOUT_MS,
            )
            if proxy_url:
                fetch_kwargs['proxy'] = proxy_url
            try:
                resp = StealthyFetcher.fetch(**fetch_kwargs)
            except TypeError:
                fetch_kwargs.pop("wait_until", None)
                fetch_kwargs.pop("timeout", None)
                resp = StealthyFetcher.fetch(**fetch_kwargs)
            used_proxy = proxy_url
            pool.report_success(proxy_url)
            break
        except Exception as e:
            elapsed = time.perf_counter() - t0
            last_error = e
            err_type = classify_error(e)
            pool.report_failure(proxy_url, err_type)
            if err_type == ERROR_PROXY_CONNECTION and proxy_url is not None:
                log.warning(f"  L1 StealthyFetcher 代理失败 ({proxy_label(proxy_url)}), 切换: {e}")
                pool.report_switch(proxy_url, None)
                continue
            log.warning(f"  L1 StealthyFetcher 失败 ({elapsed:.1f}s, {proxy_label(proxy_url)}): {e}")
            return None
    if resp is None:
        elapsed = time.perf_counter() - t0
        log.warning(f"  L1 StealthyFetcher 全部线路失败 ({elapsed:.1f}s): {last_error}")
        return None

    elapsed = time.perf_counter() - t0
    status = getattr(resp, "status", 0)
    body = _decode_body(resp.body)

    if status != 200:
        log.warning(f"  L1 StealthyFetcher HTTP {status}")
        return None

    if _is_challenge(body):
        log.warning(f"  L1 StealthyFetcher 风控检测")
        return {"keyword": keyword, "count": 0, "ads": [], "elapsed_sec": elapsed,
                "source": "L1-stealthy", "challenge": True}

    ads = _parse_static_ads(body)

    # commerce filter — 跟 Playwright 路径一致
    before = len(ads)
    ads, drop_stats = filter_commerce_ads(ads)
    if drop_stats:
        top = sorted(drop_stats.items(), key=lambda kv: -kv[1])[:3]
        reasons = ', '.join(f'{k}={v}' for k, v in top)
        log.info(f"  L1 commerce filter: kept {len(ads)}/{before} (dropped {sum(drop_stats.values())}: {reasons})")

    result = {
        "keyword": keyword,
        "count": len(ads),
        "ads": ads,
        "elapsed_sec": round(elapsed, 1),
        "source": "L1-stealthy",
    }

    log.info(f"  L1 StealthyFetcher [{status}] {keyword}: {len(ads)} ads, {elapsed:.1f}s via {proxy_label(used_proxy)}")
    return result


# ── 保存结果 ──────────────────────────────────────────────────────────────

def save_result(result: dict) -> Path:
    """保存抓取结果到 RESULTS_DIR, 格式与 Playwright 输出一致."""
    keyword = result["keyword"]
    today = datetime.now().strftime("%Y-%m-%d")
    slug = keyword.strip().replace(" ", "-").replace("+", "plus").replace("/", "_")
    out = RESULTS_DIR / f"{today}_{slug}.json"

    payload = {
        "keyword": keyword,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "source": result.get("source", "scrapling"),
        "country": "US",
        "count": result["count"],
        "version": "9.4",
        "results": result["ads"],
    }
    if result.get("pages_loaded"):
        payload["pages_loaded"] = result["pages_loaded"]
    if result.get("graphql"):
        payload["graphql"] = result["graphql"]
    if result.get("graphql_replay_used"):
        payload["graphql_replay_used"] = True
        payload["graphql_replay_added"] = result.get("graphql_replay_added", 0)
    with open(out, "wb") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8", errors="replace"))
    log.info(f"  保存 {result['count']} 条 → {out.name}")
    return out


# ── 三层决策引擎 ──────────────────────────────────────────────────────────

def triple_scan(
    keyword: str,
    country: str = "US",
    headless: bool = True,
    max_pages: Optional[int] = None,
) -> Optional[object]:
    """三层自动升级抓取.

    返回: Path(成功) / 'CHALLENGE' / None
    - L0 零命中 → None (1.5s, 跳过后续层)
    - L0 有命中 + L1 成功 → Path (6-9s, 无需 Playwright)
    - L0 有命中 + L1 失败 → 标记需要 Playwright fallback
    - CHALLENGE → 直接返回, 不触发更重层

    max_pages: 翻页上限. None=用 env FB_MAX_PAGES (默认 1).
      1=仅首页 (~25-30 条), 5=约 125-150 条.
      >1 时自动用 Patchright+graphql_sniffer 翻页 (L1.5).
    """
    if SCRAPLING_MODE == "off":
        return "NEED_PLAYWRIGHT"

    pages = max_pages if max_pages is not None else MAX_PAGES

    # ── Layer 0: Fetcher 快筛 ──────────────────────────────────────
    l0 = quick_scan(keyword, country)

    if l0 is None:
        # Fetcher 不可用, 回退
        return "NEED_PLAYWRIGHT"

    if l0.get("challenge"):
        return "CHALLENGE"

    if l0["count"] == 0:
        log.info(f"  三层决策: L0 零命中 → 跳过")
        return None

    # ── L0 有命中, 决定是否进入 L1 ────────────────────────────────
    if SCRAPLING_MODE == "quick":
        # quick 模式: L0 仅做筛, 命中后交给 Playwright
        log.info(f"  三层决策(quick): L0={l0['count']}条 → 交给 Playwright")
        return "NEED_PLAYWRIGHT"

    if pages <= 1 and L0_DIRECT_SAVE_MIN_ADS and l0["count"] >= L0_DIRECT_SAVE_MIN_ADS:
        l0["source"] = "L0-fetcher-direct"
        out = save_result(l0)
        log.info(f"  三层决策: L0={l0['count']}条 ≥ {L0_DIRECT_SAVE_MIN_ADS} → 直接保存")
        return out

    # ── L1.5: 翻页模式 (max_pages > 1) ─────────────────────────────
    if pages > 1:
        log.info(f"  三层决策: L0={l0['count']}条 → L1.5 翻页 (max={pages} 页)")
        l15 = _patchright_paginate(keyword, country, headless=headless, max_pages=pages)
        if l15 is not None and l15.get("challenge"):
            return "CHALLENGE"
        if l15 is not None and l15["count"] > 0:
            out = save_result(l15)
            log.info(
                f"  三层决策: L0={l0['count']} → L1.5={l15['count']}条"
                f" ({l15.get('pages_loaded', '?')} 页, {l15['elapsed_sec']}s)"
            )
            return out
        if l15 is not None and l15["count"] == 0:
            log.info(f"  三层决策: L1.5 确认零命中 → 跳过")
            return None
        # L1.5 失败 → 尝试 L1 首页 fallback
        log.info(f"  三层决策: L1.5 失败 → L1 首页 fallback")

    # ── Layer 1: StealthyFetcher 深抓 ──────────────────────────────
    l1 = stealthy_scan(keyword, country, headless=headless)

    if l1 is not None and l1.get("challenge"):
        return "CHALLENGE"

    if l1 is not None and l1["count"] > 0:
        # StealthyFetcher 成功! 保存并返回
        out = save_result(l1)
        log.info(f"  三层决策: L0={l0['count']} → L1={l1['count']}条 → 保存完成 ({l1['elapsed_sec']}s)")
        return out

    # L1 失败或零结果 — 回退到 Playwright
    if l1 is not None and l1["count"] == 0:
        # L1 确认零结果 (比 L0 更可靠)
        log.info(f"  三层决策: L1 确认零命中 → 跳过")
        return None

    # L1 失败 (异常) → L1.3 Camoufox Firefox fallback, 然后 Playwright 兜底
    log.info(f"  三层决策: L1 失败 → L1.3 Camoufox fallback")
    try:
        from .camoufox_scraper import camoufox_scan
        result = camoufox_scan(keyword, country=country, headless=headless)
        if result == "CHALLENGE":
            return "CHALLENGE"
        if result is not None and isinstance(result, Path):
            return result
    except Exception:
        log.exception(f"  L1.3 Camoufox 异常: {keyword}")
    log.info(f"  三层决策: L1.3 也失败 → Playwright fallback")
    return "NEED_PLAYWRIGHT"


def _patchright_paginate(
    keyword: str,
    country: str = "US",
    headless: bool = True,
    max_pages: int = 5,
    max_scrolls_per_page: int = 6,
) -> Optional[dict]:
    """L1.5: Patchright + graphql_sniffer 翻页抓取.

    直接用 patchright (Scrapling 底层同款 Chromium), 绕过 StealthyFetcher
    对 page.on('response') 的冲突. 用 graphql_sniffer 拦截每次 GraphQL
    分页响应, 收集所有页的广告数据.

    max_pages: 最多翻几页 (每页 ~25-30 条, 5 页 ≈ 125-150 条)
    max_scrolls_per_page: 每页最多滚多少次触发下一页加载

    v10 速度内核:
      1) FB_BROWSER_POOL=1 (默认): worker 线程内复用 patchright 实例 + browser
         对象, 节省每个关键词 ~2s 的 launch 开销. context 仍每次新建, 保留
         per-keyword 指纹隔离. 每个 browser 服务 FB_BROWSER_MAX_USES (默认 1)
         个关键词后 retire, 刷新指纹 cohort.
      2) FB_GRAPHQL_REPLAY=1 (默认): 第一页拿到 forward_cursor + cookies 后,
         后续 max_pages-1 页改走 curl_cffi HTTP 直连 GraphQL, 跳过浏览器滚动.
         5 页 keyword 从 ~15-20s 压到 ~3-5s.
      3) BrowserForge 指纹与 L2 Playwright 共用同一生成器, 跨层不串味.
    """
    try:
        from patchright.sync_api import sync_playwright as sync_patchright
    except ImportError:
        log.warning("  patchright 未安装, 翻页不可用")
        return None

    from .graphql_sniffer import install_sniffer, merge_dom_with_graphql
    # Hot-reload js_script so edits to EXTRACT_JS_V9 take effect without restart.
    import importlib
    from . import js_script as _js_script
    try:
        importlib.reload(_js_script)
    except Exception:
        pass
    EXTRACT_JS_V9 = _js_script.EXTRACT_JS_V9

    url = _build_url(keyword, country)
    t0 = time.perf_counter()

    # v10: 统计真实指纹, 与 L2 Playwright 共用同一生成器
    from .fingerprint import generate_fingerprint
    fp = generate_fingerprint()
    ua = fp['user_agent']
    viewport = fp['viewport']
    locale = fp['locale']
    timezone = fp['timezone']

    dom_ads: list[dict] = []
    pages_loaded = 0
    challenge = False
    graphql_replay_used = False
    graphql_replay_added = 0
    graphql_ads_by_id: dict = {}
    graphql_meta: dict = {}

    # ── Browser 获取: 优先 thread-local pool, fallback 临时 launch ──
    pool = get_pool()
    selected_proxy = pool.acquire()

    pool_state = _get_thread_browser(headless)
    own_browser = pool_state is None    # True = we own the browser lifecycle
    pw_temp = None
    if pool_state is not None:
        browser = pool_state['browser']
        pool_state['uses'] += 1
    else:
        try:
            pw_temp = sync_patchright().start()
            launch_kwargs = dict(
                headless=headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            pw_proxy = proxy_for_playwright(selected_proxy)
            if pw_proxy:
                launch_kwargs['proxy'] = pw_proxy
            browser = pw_temp.chromium.launch(**launch_kwargs)
        except Exception as e:
            pool.report_failure(selected_proxy, classify_error(e))
            log.warning(f"  L1.5 Patchright launch 失败 (proxy={proxy_label(selected_proxy)}): {e}")
            if pw_temp is not None:
                try:
                    pw_temp.stop()
                except Exception:
                    pass
            return None

    ctx = None
    try:
        ctx = browser.new_context(
            viewport=viewport,
            user_agent=ua,
            locale=locale,
            timezone_id=timezone,
        )
        page = ctx.new_page()

        # 隐藏 webdriver 指纹
        try:
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
        except Exception:
            pass

        # 安装 GraphQL sniffer — 拦截 /api/graphql/ 分页响应
        sniffer_state = install_sniffer(page)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as e:
            log.warning(f"  L1.5 页面加载失败: {e}")
            if not own_browser:
                _retire_thread_browser()
            return None

        # 风控检测
        try:
            body_sample = page.evaluate(
                "document.body.innerText.substring(0, 500).toLowerCase()"
            )
            if _is_challenge(body_sample):
                challenge = True
        except Exception:
            pass

        if not challenge:
            # 第一页: 等内容渲染 + 收集初始 GraphQL 响应
            _wait_for_first_render_signal(sniffer_state)
            pages_loaded = 1

            # 提取第一页 DOM 数据 (fallback)
            try:
                payload = page.evaluate(EXTRACT_JS_V9)
                if payload:
                    import json as _json
                    data = _json.loads(payload) if isinstance(payload, str) else payload
                    if isinstance(data, dict):
                        dom_ads = data.get("results", [])
            except Exception:
                pass

            # ── 翻页阶段: 优先 graphql_replay HTTP, 失败回退浏览器滚动 ──
            if max_pages > 1 and os.getenv('FB_GRAPHQL_REPLAY', '1') != '0':
                cursor = sniffer_state.get('forward_cursor', '')
                if cursor:
                    try:
                        cookies_list = ctx.cookies()
                        cookies = {
                            c.get('name', ''): c.get('value', '')
                            for c in cookies_list
                            if c.get('name')
                        }
                        from .graphql_replay import graphql_paginate
                        replay_ads = graphql_paginate(
                            keyword,
                            cursor,
                            cookies,
                            country=country,
                            max_pages=max_pages - 1,
                        )
                        # graphql_paginate returns full ad dicts already extracted
                        # via _extract_ad_node (same shape as sniffer state['ads']).
                        # Merge by id, don't overwrite anything sniffer already has
                        # from page 1 (those may be richer for the visible cards).
                        for ad in replay_ads:
                            aid = ad.get('ad_archive_id')
                            if aid and aid not in sniffer_state['ads']:
                                sniffer_state['ads'][aid] = ad
                                graphql_replay_added += 1
                        if replay_ads:
                            pages_loaded = max_pages
                            graphql_replay_used = True
                            log.info(
                                f"  L1.5 graphql_replay: +{graphql_replay_added} ads"
                                f" via HTTP ({len(replay_ads)} fetched, {max_pages - 1} pages)"
                            )
                    except Exception as e:
                        log.warning(
                            f"  L1.5 graphql_replay 失败, 回退浏览器翻页: {e}"
                        )

            # 浏览器翻页 fallback: 仅当 graphql_replay 未使用或未生效
            if not graphql_replay_used and max_pages > 1:
                prev_ad_count = len(sniffer_state.get("ads", {}))
                stable_scrolls = 0

                for page_num in range(2, max_pages + 1):
                    for scroll_i in range(max_scrolls_per_page):
                        try:
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            try:
                                page.mouse.move(
                                    random.randint(200, 1200),
                                    random.randint(200, 800),
                                )
                            except Exception:
                                pass
                            time.sleep(random.uniform(0.6, 1.2))
                        except Exception as e:
                            err = str(e).lower()
                            if "destroyed" in err or "closed" in err:
                                break
                            continue

                    time.sleep(random.uniform(0.8, 1.5))

                    current_ad_count = len(sniffer_state.get("ads", {}))
                    if current_ad_count > prev_ad_count:
                        pages_loaded = page_num
                        prev_ad_count = current_ad_count
                        stable_scrolls = 0
                        log.info(
                            f"  L1.5 翻页 #{page_num}: GraphQL 累计 {current_ad_count} 条广告"
                        )
                    else:
                        stable_scrolls += 1
                        if stable_scrolls >= 2:
                            log.info(
                                f"  L1.5 到底: 第 {page_num} 页无新广告, 停止翻页"
                            )
                            break

        # 收集结果
        graphql_ads_by_id = dict(sniffer_state.get("ads", {}))
        graphql_meta = {
            "graphql_count": sniffer_state.get("graphql_count", 0),
            "ad_responses": sniffer_state.get("ad_responses", 0),
        }

        # 最后提取一次 DOM (滚动后可能有更多内容)
        try:
            payload = page.evaluate(EXTRACT_JS_V9)
            if payload:
                import json as _json
                data = _json.loads(payload) if isinstance(payload, str) else payload
                if isinstance(data, dict):
                    dom_ads = data.get("results", [])
        except Exception:
            pass

    except Exception as e:
        elapsed = time.perf_counter() - t0
        log.warning(f"  L1.5 Patchright 异常 ({elapsed:.1f}s): {e}")
        # 异常会污染 browser 状态 — 强制 retire pool entry
        if not own_browser:
            _retire_thread_browser()
        else:
            try:
                browser.close()
            except Exception:
                pass
            if pw_temp is not None:
                try:
                    pw_temp.stop()
                except Exception:
                    pass
        return None
    finally:
        # Context 每次都关 (无论 pool 还是 cold path)
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
        # Cold path: 关 browser + pw
        if own_browser:
            try:
                browser.close()
            except Exception:
                pass
            if pw_temp is not None:
                try:
                    pw_temp.stop()
                except Exception:
                    pass

    # Challenge 触发或 use 达上限: retire pool entry, 下次冷启
    if not own_browser and (challenge or (pool_state and pool_state['uses'] >= BROWSER_MAX_USES)):
        _retire_thread_browser()

    elapsed = time.perf_counter() - t0

    if challenge:
        return {
            "keyword": keyword, "count": 0, "ads": [],
            "elapsed_sec": round(elapsed, 1), "source": "L1.5-patchright",
            "challenge": True,
        }

    # 合并 DOM + GraphQL (GraphQL 优先)
    results = dom_ads
    if graphql_ads_by_id:
        results = merge_dom_with_graphql(dom_ads, graphql_ads_by_id)
        log.info(
            f"  L1.5 merge: dom={len(dom_ads)} graphql={len(graphql_ads_by_id)}"
            f" (responses={graphql_meta['ad_responses']}/{graphql_meta['graphql_count']})"
            f" → final={len(results)}"
        )

    # commerce filter
    before = len(results)
    results, drop_stats = filter_commerce_ads(results)
    if drop_stats:
        top = sorted(drop_stats.items(), key=lambda kv: -kv[1])[:3]
        reasons = ', '.join(f'{k}={v}' for k, v in top)
        log.info(f"  L1.5 commerce filter: kept {len(results)}/{before} (dropped {sum(drop_stats.values())}: {reasons})")

    result = {
        "keyword": keyword,
        "count": len(results),
        "ads": results,
        "elapsed_sec": round(elapsed, 1),
        "source": "L1.5-graphql-replay" if graphql_replay_used else "L1.5-patchright",
        "pages_loaded": pages_loaded,
        "graphql": graphql_meta if graphql_ads_by_id else {},
        "graphql_replay_used": graphql_replay_used,
        "graphql_replay_added": graphql_replay_added,
    }
    log.info(
        f"  L1.5 {result['source']} {keyword}: {len(results)} ads, "
        f"{pages_loaded} pages, {elapsed:.1f}s"
        + (f" (replay +{graphql_replay_added})" if graphql_replay_used else "")
    )
    return result


def should_deep_scrape(quick_result: Optional[dict]) -> tuple[bool, int]:
    """向后兼容: 判断是否需要 Playwright 深抓 (旧接口)."""
    if quick_result is None:
        return True, 0
    if quick_result.get("challenge"):
        return False, 0
    if quick_result["count"] == 0:
        return False, 0
    return True, quick_result["count"]

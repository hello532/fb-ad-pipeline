"""pipeline/camoufox_scraper.py — Camoufox (Firefox) 反检测抓取层 (v10).

Layer 1.3: 当 Patchright/Chromium 路径失败时, 用 Firefox 指纹尝试.
Camoufox 自动生成统计真实的 Firefox 指纹 (BrowserForge), 与 Chromium 完全不同的
TLS/JA3 指纹, 对 FB 的 Chromium-specific 检测有天然免疫.

用法:
    from pipeline.camoufox_scraper import camoufox_scan
    result = camoufox_scan("keyword", country="US")
    # result: Path (成功) / 'CHALLENGE' / None

env:
    FB_CAMOUFOX=1       启用 (默认)
    FB_CAMOUFOX=0       禁用
"""
from __future__ import annotations

import json
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from core.logger import get_logger
from .paths import RESULTS_DIR
from .commerce_filter import filter_commerce_ads
from .graphql_sniffer import install_sniffer, merge_dom_with_graphql
from .proxy_pool import get_pool, classify_error, proxy_for_playwright
from .runtime import proxy_label

log = get_logger(__name__)

CAMOUFOX_ENABLED = os.getenv('FB_CAMOUFOX', '1') != '0'

CHALLENGE_MARKERS = (
    "temporarily restricted", "unusual activity", "too many requests",
    "rate limit", "unusual_activity", "suspicious",
)


def _is_challenge(text: str) -> bool:
    lower = text[:3000].lower()
    return any(m in lower for m in CHALLENGE_MARKERS)


def camoufox_scan(
    keyword: str,
    country: str = "US",
    headless: bool = True,
    max_scrolls: int = 8,
) -> Optional[object]:
    """Camoufox Firefox 抓取. 返回 Path / 'CHALLENGE' / None."""
    if not CAMOUFOX_ENABLED:
        return None

    try:
        from camoufox.sync_api import Camoufox
    except ImportError:
        log.warning("camoufox 未安装, 跳过 Firefox 层")
        return None

    from . import js_script as _js
    import importlib
    try:
        importlib.reload(_js)
    except Exception:
        pass

    url = (
        f"https://www.facebook.com/ads/library/"
        f"?active_status=active&ad_type=all&search_type=keyword_unordered"
        f"&country={country}&q={keyword.replace(' ', '+')}"
    )

    t0 = time.perf_counter()
    dom_ads: list[dict] = []
    graphql_ads_by_id: dict = {}
    graphql_meta: dict = {}

    try:
        camoufox_kwargs = dict(headless=headless)
        pool = get_pool()
        selected_proxy = pool.acquire()
        pw_proxy = proxy_for_playwright(selected_proxy)
        if pw_proxy:
            camoufox_kwargs['proxy'] = pw_proxy
        with Camoufox(**camoufox_kwargs) as browser:
            page = browser.new_page()

            sniffer_state = install_sniffer(page)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:
                log.warning(f"  L1.3 Camoufox 页面加载失败: {e}")
                return None

            # 风控检测
            try:
                body_sample = page.evaluate(
                    "document.body.innerText.substring(0, 500).toLowerCase()"
                )
                if _is_challenge(body_sample):
                    log.warning(f"  L1.3 Camoufox 风控: {keyword}")
                    return "CHALLENGE"
            except Exception:
                pass

            # 等待渲染
            time.sleep(random.uniform(1.0, 1.8))

            # 滚动加载
            for i in range(max_scrolls):
                try:
                    page.evaluate(f"window.scrollBy(0, {random.randint(600, 1000)})")
                except Exception:
                    break
                time.sleep(random.uniform(0.4, 0.8))

            # 等 networkidle
            try:
                page.wait_for_load_state('networkidle', timeout=3000)
            except Exception:
                pass

            # DOM 提取
            try:
                payload = page.evaluate(_js.EXTRACT_JS_V7)
                if payload:
                    data = json.loads(payload) if isinstance(payload, str) else payload
                    if isinstance(data, dict):
                        dom_ads = data.get("results", [])
            except Exception as e:
                log.warning(f"  L1.3 JS 提取失败: {e}")

            # GraphQL sniffer 结果
            graphql_ads_by_id = dict(sniffer_state.get("ads", {}))
            graphql_meta = {
                "graphql_count": sniffer_state.get("graphql_count", 0),
                "ad_responses": sniffer_state.get("ad_responses", 0),
            }

    except Exception as e:
        elapsed = time.perf_counter() - t0
        log.warning(f"  L1.3 Camoufox 异常 ({elapsed:.1f}s): {e}")
        return None

    elapsed = time.perf_counter() - t0

    # 合并 DOM + GraphQL
    results = dom_ads
    if graphql_ads_by_id:
        results = merge_dom_with_graphql(dom_ads, graphql_ads_by_id)
        log.info(
            f"  L1.3 merge: dom={len(dom_ads)} graphql={len(graphql_ads_by_id)}"
            f" → final={len(results)}"
        )

    # Commerce filter
    before = len(results)
    results, drop_stats = filter_commerce_ads(results)
    if drop_stats:
        top = sorted(drop_stats.items(), key=lambda kv: -kv[1])[:3]
        reasons = ', '.join(f'{k}={v}' for k, v in top)
        log.info(f"  L1.3 commerce filter: kept {len(results)}/{before} (dropped: {reasons})")

    if not results:
        log.info(f"  L1.3 Camoufox {keyword}: 零结果 ({elapsed:.1f}s)")
        return None

    # 保存
    today = datetime.now().strftime("%Y-%m-%d")
    slug = keyword.strip().replace(" ", "-").replace("+", "plus").replace("/", "_")
    out = RESULTS_DIR / f"{today}_{slug}.json"
    payload_out = {
        "keyword": keyword,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "source": "camoufox-firefox",
        "country": country,
        "count": len(results),
        "version": "10.0",
        "graphql": graphql_meta if graphql_ads_by_id else {},
        "results": results,
    }
    with open(out, "wb") as f:
        f.write(json.dumps(payload_out, ensure_ascii=False, indent=2).encode("utf-8", errors="replace"))
    log.info(f"  L1.3 Camoufox {keyword}: {len(results)} ads, {elapsed:.1f}s → {out.name}")
    return out

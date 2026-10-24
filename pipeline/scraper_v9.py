"""pipeline/scraper_v9.py — 高速 FB Ad Library 抓取引擎.

目标:
- 多关键词并发 (默认 3)
- 单关键词多 context 深滚动并发 (默认 3 个 page shard)
- JS 快速滚动替代慢速滚动
- 输出保持 v8/v9 raw JSON 结构: keyword/scanned_at/source/country/count/results

说明: FB Ad Library 没有公开分页 API。这里的"并发翻页"采用多个隔离
browser context 对同一关键词执行不同深度的快速滚动, 再按 ad_archive_id
去重。这样不会改变下游 raw_processor / ad_archive 的输入格式。
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "pipeline"

from core.logger import get_logger

from .commerce_filter import filter_commerce_ads
from .graphql_sniffer import _decode_stream_into
from .paths import RESULTS_DIR
from .scraper import (
    AD_LIBRARY_BASE,
    ANONYMOUS_MODE,
    BLOCKED_DOMAINS,
    BLOCKED_RESOURCE_TYPES,
    CHALLENGE_MARKERS,
    FB_BLOCK_RESOURCES,
    FB_PROXY,
    _generate_fingerprint,
    _get_extract_js,
)

log = get_logger(__name__)

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]

DEFAULT_PAGES = 10
DEFAULT_PARALLEL_KW = 3
DEFAULT_PAGE_CONCURRENCY = 3
FAST_DELAY_RANGE = (0.8, 2.0)


@dataclass
class ScrapeStats:
    keyword: str
    pages_requested: int
    page_shards: int
    started_at: float = field(default_factory=time.perf_counter)
    ads_collected: int = 0
    ads_after_filter: int = 0
    duplicate_ads: int = 0
    failed_pages: int = 0
    challenge_pages: int = 0
    elapsed_sec: float = 0.0

    def done(self) -> dict[str, Any]:
        self.elapsed_sec = round(time.perf_counter() - self.started_at, 2)
        return {
            "keyword": self.keyword,
            "pages_requested": self.pages_requested,
            "page_shards": self.page_shards,
            "elapsed_sec": self.elapsed_sec,
            "ads_collected": self.ads_collected,
            "ads_after_filter": self.ads_after_filter,
            "duplicate_ads": self.duplicate_ads,
            "failed_pages": self.failed_pages,
            "challenge_pages": self.challenge_pages,
        }


async def _emit(progress: ProgressCallback | None, event: dict[str, Any]) -> None:
    if not progress:
        return
    try:
        result = progress(event)
        if inspect.isawaitable(result):
            await result
    except Exception:
        log.debug("scraper_v9 progress callback failed", exc_info=True)


def _slug(keyword: str) -> str:
    return (
        keyword.strip()
        .replace(" ", "-")
        .replace("+", "plus")
        .replace("/", "_")
    )


def _dedupe_ads(ads: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    dupes = 0
    for ad in ads:
        if not isinstance(ad, dict):
            continue
        aid = str(ad.get("ad_archive_id") or ad.get("id") or "").strip()
        if not aid or not aid.isdigit() or len(aid) < 8:
            continue
        if ad.get("is_active") is False:
            continue
        if aid in seen:
            dupes += 1
            continue
        seen.add(aid)
        out.append(ad)
    return out, dupes


def _merge_ads(dom_ads: list[dict[str, Any]], gql_ads: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for ad in dom_ads or []:
        if not isinstance(ad, dict):
            continue
        aid = str(ad.get("ad_archive_id") or ad.get("id") or "").strip()
        if aid:
            merged[aid] = ad
    for aid, ad in (gql_ads or {}).items():
        current = merged.get(aid, {})
        # GraphQL 字段更齐, 但保留 DOM 中非空字段。
        new_ad = dict(current)
        for key, value in ad.items():
            if value not in (None, "", [], {}, 0, False):
                new_ad[key] = value
        merged[aid] = new_ad
    return list(merged.values())


async def _install_async_sniffer(page) -> dict[str, Any]:
    state: dict[str, Any] = {
        "ads": {},
        "graphql_count": 0,
        "ad_responses": 0,
        "skipped_too_big": 0,
        "errors": 0,
    }

    async def _handler(response):
        try:
            url = response.url or ""
            if "/api/graphql/" not in url:
                return
            if response.status and response.status >= 400:
                return
            headers = response.headers or {}
            ct = (headers.get("content-type") or "").lower()
            if "json" not in ct and "javascript" not in ct and "text/" not in ct:
                return
            body = await response.text()
            if not body:
                return
            if len(body) > 12 * 1024 * 1024:
                state["skipped_too_big"] += 1
                return
            state["graphql_count"] += 1
            if '"ad_archive_id"' not in body and '"adArchiveID"' not in body:
                return
            state["ad_responses"] += 1
            _decode_stream_into(body, state)
        except Exception:
            state["errors"] += 1

    page.on("response", lambda response: asyncio.create_task(_handler(response)))
    return state


async def _attach_resource_blocker(context) -> None:
    if not FB_BLOCK_RESOURCES:
        return

    async def _route(route, request):
        try:
            url = request.url or ""
            if request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
            if any(domain in url for domain in BLOCKED_DOMAINS):
                await route.abort()
                return
            await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await context.route("**/*", _route)
    except Exception:
        pass


async def _close_login_popups(page) -> None:
    selectors = [
        '[aria-label="Close"]',
        '[aria-label="关闭"]',
        'div[role="button"][aria-label*="lose"]',
    ]
    for selector in selectors:
        try:
            el = await page.query_selector(selector)
            if el and await el.is_visible():
                await el.click(timeout=1000)
                await page.wait_for_timeout(200)
                return
        except Exception:
            continue


async def _fast_scroll(page, *, rounds: int, shard: int) -> None:
    """注入 JS 快速滚动。每个 shard 用不同步幅, 提高触发 GraphQL 分页的机会。"""
    rounds = max(4, rounds)
    step = 950 + shard * 260
    pause = 85 + shard * 20
    await page.evaluate(
        """async ({rounds, step, pause}) => {
          const wait = ms => new Promise(r => setTimeout(r, ms));
          for (let i = 0; i < rounds; i++) {
            window.scrollBy(0, step + Math.floor(Math.random() * 220));
            if (i % 4 === 3) window.dispatchEvent(new Event('scroll'));
            await wait(pause);
          }
          window.scrollTo(0, Math.max(0, document.body.scrollHeight - window.innerHeight));
          await wait(220);
        }""",
        {"rounds": rounds, "step": step, "pause": pause},
    )


async def _extract_dom_ads(page) -> list[dict[str, Any]]:
    try:
        payload = await page.evaluate(_get_extract_js())
    except Exception as exc:
        log.warning(f"scraper_v9 DOM extract failed: {str(exc)[:120]}")
        return []
    try:
        data = json.loads(payload) if isinstance(payload, str) else (payload or {})
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    return data.get("results") or []


async def _scrape_keyword_shard(
    browser,
    keyword: str,
    *,
    country: str,
    pages: int,
    shard: int,
    headless: bool,
    timeout_ms: int,
) -> dict[str, Any]:
    fp = _generate_fingerprint()
    context = await browser.new_context(
        viewport=fp.get("viewport") or {"width": 1440, "height": 900},
        user_agent=fp.get("user_agent"),
        locale="en-US",
        timezone_id=fp.get("timezone") or "America/New_York",
    )
    await _attach_resource_blocker(context)
    page = await context.new_page()
    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    sniffer_state = await _install_async_sniffer(page)
    q = keyword.replace(" ", "+")
    url = f"{AD_LIBRARY_BASE}&country={country}&q={q}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        await _close_login_popups(page)
        current = (page.url or "").lower()
        if any(marker in current for marker in CHALLENGE_MARKERS):
            return {"ads": [], "challenge": True, "error": f"redirected: {page.url}"}
        try:
            body_sample = await page.evaluate("document.body.innerText.substring(0, 500).toLowerCase()")
            if any(marker in body_sample for marker in ("temporarily restricted", "unusual activity", "too many requests", "rate limit")):
                return {"ads": [], "challenge": True, "error": body_sample[:160]}
        except Exception:
            pass
        await page.wait_for_timeout(int(random.uniform(*FAST_DELAY_RANGE) * 1000))
        await _fast_scroll(page, rounds=pages * 5 + shard * 3, shard=shard)
        try:
            await page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass
        await page.wait_for_timeout(250)
        dom_ads = await _extract_dom_ads(page)
        gql_ads = dict(sniffer_state.get("ads") or {})
        ads = _merge_ads(dom_ads, gql_ads)
        return {
            "ads": ads,
            "challenge": False,
            "graphql": {
                "graphql_count": sniffer_state.get("graphql_count", 0),
                "ad_responses": sniffer_state.get("ad_responses", 0),
                "unique_ads": len(gql_ads),
                "errors": sniffer_state.get("errors", 0),
            },
        }
    except Exception as exc:
        return {"ads": [], "challenge": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            await context.close()
        except Exception:
            pass


async def scrape_keyword_v9(
    keyword: str,
    *,
    pages: int = DEFAULT_PAGES,
    page_concurrency: int = DEFAULT_PAGE_CONCURRENCY,
    country: str = "US",
    headless: bool = True,
    progress: ProgressCallback | None = None,
    timeout_ms: int = 45_000,
) -> dict[str, Any]:
    """抓取单个关键词, 保存 raw JSON, 返回 path/count/scrape_stats。"""
    pages = max(1, int(pages or DEFAULT_PAGES))
    page_concurrency = max(1, min(int(page_concurrency or DEFAULT_PAGE_CONCURRENCY), pages))
    stats = ScrapeStats(keyword=keyword, pages_requested=pages, page_shards=page_concurrency)
    await _emit(progress, {"type": "keyword_start", "keyword": keyword, "pages": pages})

    from playwright.async_api import async_playwright

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
    ]
    launch_kwargs: dict[str, Any] = {"headless": headless, "args": launch_args}
    if ANONYMOUS_MODE and FB_PROXY:
        launch_kwargs["proxy"] = {"server": FB_PROXY}

    all_ads: list[dict[str, Any]] = []
    graphql_totals = {"graphql_count": 0, "ad_responses": 0, "unique_ads": 0, "errors": 0}
    async with async_playwright() as p:
        browser = await p.chromium.launch(**launch_kwargs)
        try:
            shard_pages = max(1, (pages + page_concurrency - 1) // page_concurrency)
            tasks = [
                _scrape_keyword_shard(
                    browser,
                    keyword,
                    country=country,
                    pages=shard_pages + shard,
                    shard=shard,
                    headless=headless,
                    timeout_ms=timeout_ms,
                )
                for shard in range(page_concurrency)
            ]
            for i, coro in enumerate(asyncio.as_completed(tasks), 1):
                result = await coro
                if result.get("challenge"):
                    stats.challenge_pages += 1
                if result.get("error"):
                    stats.failed_pages += 1
                    await _emit(progress, {
                        "type": "page_failed",
                        "keyword": keyword,
                        "done": i,
                        "error": result.get("error"),
                    })
                    continue
                ads = result.get("ads") or []
                all_ads.extend(ads)
                for key in graphql_totals:
                    graphql_totals[key] += int((result.get("graphql") or {}).get(key) or 0)
                await _emit(progress, {
                    "type": "page_done",
                    "keyword": keyword,
                    "done": i,
                    "ads": len(ads),
                })
        finally:
            await browser.close()

    stats.ads_collected = len(all_ads)
    deduped, dupes = _dedupe_ads(all_ads)
    stats.duplicate_ads = dupes
    filtered, drop_stats = filter_commerce_ads(deduped)
    stats.ads_after_filter = len(filtered)

    if not filtered:
        done = stats.done()
        await _emit(progress, {"type": "keyword_done", "keyword": keyword, "count": 0, "scrape_stats": done})
        return {"keyword": keyword, "path": None, "count": 0, "scrape_stats": done, "drop_stats": drop_stats}

    today = datetime.now().strftime("%Y-%m-%d")
    out = RESULTS_DIR / f"{today}_{_slug(keyword)}.json"
    payload = {
        "keyword": keyword,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "source": "scraper-v9-fast-anonymous" if ANONYMOUS_MODE else "scraper-v9-fast-auth",
        "country": country,
        "count": len(filtered),
        "version": "9.0-fast",
        "graphql": graphql_totals,
        "scrape_stats": stats.done(),
        "results": filtered,
    }
    out.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8", errors="replace"))
    await _emit(progress, {
        "type": "keyword_done",
        "keyword": keyword,
        "count": len(filtered),
        "path": str(out),
        "scrape_stats": payload["scrape_stats"],
    })
    log.info(f"scraper_v9 saved {len(filtered)} ads -> {out.name}")
    return {"keyword": keyword, "path": str(out), "count": len(filtered), "scrape_stats": payload["scrape_stats"]}


async def scrape_keywords_v9(
    keywords: list[str],
    *,
    pages: int = DEFAULT_PAGES,
    parallel_kw: int = DEFAULT_PARALLEL_KW,
    page_concurrency: int = DEFAULT_PAGE_CONCURRENCY,
    country: str = "US",
    headless: bool = True,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """多关键词并发抓取。单关键词失败不会阻塞整体。"""
    started = time.perf_counter()
    clean = []
    seen = set()
    for kw in keywords or []:
        s = str(kw or "").strip()
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            clean.append(s[:120])
    sem = asyncio.Semaphore(max(1, int(parallel_kw or DEFAULT_PARALLEL_KW)))
    results: list[dict[str, Any]] = []

    async def _run_one(kw: str) -> None:
        async with sem:
            try:
                result = await scrape_keyword_v9(
                    kw,
                    pages=pages,
                    page_concurrency=page_concurrency,
                    country=country,
                    headless=headless,
                    progress=progress,
                )
            except Exception as exc:
                result = {"keyword": kw, "path": None, "count": 0, "error": f"{type(exc).__name__}: {exc}"}
                await _emit(progress, {"type": "keyword_error", "keyword": kw, "error": result["error"]})
            results.append(result)
            await asyncio.sleep(random.uniform(*FAST_DELAY_RANGE))

    await _emit(progress, {"type": "batch_start", "keywords": clean, "pages": pages, "parallel_kw": parallel_kw})
    await asyncio.gather(*[_run_one(kw) for kw in clean])
    elapsed = round(time.perf_counter() - started, 2)
    summary = {
        "keywords": len(clean),
        "saved_keywords": sum(1 for r in results if r.get("path")),
        "ads_total": sum(int(r.get("count") or 0) for r in results),
        "failed_keywords": sum(1 for r in results if r.get("error") or not r.get("path")),
        "elapsed_sec": elapsed,
        "results": sorted(results, key=lambda r: str(r.get("keyword") or "")),
    }
    await _emit(progress, {"type": "batch_done", **summary})
    return summary


def scrape_keywords_sync(**kwargs) -> dict[str, Any]:
    """同步包装, 方便 CLI / 后台线程调用。"""
    return asyncio.run(scrape_keywords_v9(**kwargs))


def scrape_keyword_sync(keyword: str, **kwargs) -> dict[str, Any]:
    return asyncio.run(scrape_keyword_v9(keyword, **kwargs))


def scrape_queue_v9(
    *,
    limit: int = 5,
    pages: int = DEFAULT_PAGES,
    parallel_kw: int = DEFAULT_PARALLEL_KW,
    page_concurrency: int = DEFAULT_PAGE_CONCURRENCY,
    country: str = "US",
    headless: bool = True,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    from .scan_instructions import scan_queue_loader

    items = scan_queue_loader()
    keywords = [item["keyword"] for item in items[: max(0, limit)]]
    return asyncio.run(
        scrape_keywords_v9(
            keywords,
            pages=pages,
            parallel_kw=parallel_kw,
            page_concurrency=page_concurrency,
            country=country,
            headless=headless,
            progress=progress,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="FB Ad Library scraper v9 fast engine")
    parser.add_argument("--keywords", nargs="*", default=None, help="关键词列表")
    parser.add_argument("--limit", type=int, default=5, help="从 scan_queue 取多少个关键词")
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES, help="每关键词目标页数, 默认 10")
    parser.add_argument("--parallel-kw", type=int, default=DEFAULT_PARALLEL_KW, help="关键词并发数, 默认 3")
    parser.add_argument("--page-concurrency", type=int, default=DEFAULT_PAGE_CONCURRENCY, help="单关键词 context 并发, 默认 3")
    parser.add_argument("--country", default="US")
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args()

    async def _print_progress(event: dict[str, Any]) -> None:
        print(json.dumps(event, ensure_ascii=False), flush=True)

    if args.keywords:
        result = asyncio.run(
            scrape_keywords_v9(
                args.keywords,
                pages=args.pages,
                parallel_kw=args.parallel_kw,
                page_concurrency=args.page_concurrency,
                country=args.country,
                headless=not args.headful,
                progress=_print_progress,
            )
        )
    else:
        result = scrape_queue_v9(
            limit=args.limit,
            pages=args.pages,
            parallel_kw=args.parallel_kw,
            page_concurrency=args.page_concurrency,
            country=args.country,
            headless=not args.headful,
            progress=_print_progress,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

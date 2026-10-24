"""pipeline/graphql_replay.py — 直接 GraphQL 分页 (v10).

绕过浏览器翻页, 用 curl_cffi (TLS 指纹模拟) 直接 POST /api/graphql/.
第一页由浏览器层拿到 forward_cursor + cookies, 后续页纯 HTTP 请求.

速度: 5 页从 15-20s (浏览器翻页) → 3-5s (纯 HTTP).

用法:
    from pipeline.graphql_replay import graphql_paginate
    ads = graphql_paginate(keyword, cursor, cookies, max_pages=5)

env:
    FB_GRAPHQL_REPLAY=1   启用 (默认)
    FB_GRAPHQL_REPLAY=0   禁用
"""
from __future__ import annotations

import json
import os
import random
import time
from typing import Optional

from core.logger import get_logger
from .proxy_pool import get_pool, classify_error, ERROR_PROXY_CONNECTION
from .runtime import proxy_label

log = get_logger(__name__)

GRAPHQL_REPLAY_ENABLED = os.getenv('FB_GRAPHQL_REPLAY', '1') != '0'
GQL_REPLAY_PAUSE_MIN_SEC = max(0.05, float(os.getenv('FB_GQL_REPLAY_PAUSE_MIN_SEC', '0.18')))
GQL_REPLAY_PAUSE_MAX_SEC = max(GQL_REPLAY_PAUSE_MIN_SEC, float(os.getenv('FB_GQL_REPLAY_PAUSE_MAX_SEC', '0.45')))

GRAPHQL_ENDPOINT = "https://www.facebook.com/api/graphql/"

# Known doc_id for AdLibrarySearchPaginationQuery (May 2026)
# FB rotates these periodically; sniffer captures the live one
DOC_ID = os.getenv('FB_GRAPHQL_DOC_ID', '7268381195839281')

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


def _build_variables(keyword: str, country: str, cursor: str) -> str:
    """Build the GraphQL variables JSON for pagination query."""
    variables = {
        "activeStatus": "active",
        "adType": "ALL",
        "bylines": [],
        "collationToken": None,
        "contentLanguages": [],
        "countries": [country],
        "cursor": cursor,
        "excludedIDs": [],
        "first": 30,
        "location": None,
        "mediaType": "ALL",
        "potentialReachInput": [],
        "publisherPlatforms": [],
        "queryString": keyword,
        "regions": [],
        "searchType": "KEYWORD_UNORDERED",
        "sortData": None,
        "source": None,
        "startDate": None,
        "v": "default",
    }
    return json.dumps(variables, separators=(',', ':'))


def graphql_paginate(
    keyword: str,
    initial_cursor: str,
    cookies: dict,
    country: str = "US",
    max_pages: int = 5,
    doc_id: Optional[str] = None,
) -> list[dict]:
    """Fetch pages 2..N via direct GraphQL POST. Returns list of raw ad dicts.

    Args:
        keyword: search keyword
        initial_cursor: forward_cursor from page 1 (captured by graphql_sniffer)
        cookies: session cookies dict from browser context
        country: country code
        max_pages: how many additional pages to fetch
        doc_id: override doc_id if sniffer captured a newer one
    """
    if not GRAPHQL_REPLAY_ENABLED:
        return []

    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        log.warning("curl_cffi 未安装, GraphQL replay 不可用")
        return []

    effective_doc_id = doc_id or DOC_ID
    ua = random.choice(USER_AGENTS)
    cursor = initial_cursor
    all_ads: list[dict] = []

    headers = {
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://www.facebook.com",
        "Referer": "https://www.facebook.com/ads/library/",
        "X-FB-Friendly-Name": "AdLibrarySearchPaginationQuery",
        "X-FB-LSD": cookies.get("lsd", ""),
    }

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    for page_num in range(2, max_pages + 2):
        if not cursor:
            log.info(f"  GraphQL replay: no cursor at page {page_num}, stopping")
            break

        variables = _build_variables(keyword, country, cursor)
        form_data = {
            "doc_id": effective_doc_id,
            "variables": variables,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": "AdLibrarySearchPaginationQuery",
            "server_timestamps": "true",
        }

        resp = None
        pool = get_pool()
        for proxy_url in pool.acquire_sequence():
            proxies = {'http': proxy_url, 'https': proxy_url} if proxy_url else None
            try:
                resp = cffi_requests.post(
                    GRAPHQL_ENDPOINT,
                    data=form_data,
                    headers=headers,
                    cookies=cookies,
                    proxies=proxies,
                    impersonate="chrome131",
                    timeout=15,
                )
                pool.report_success(proxy_url)
                break
            except Exception as e:
                err_type = classify_error(e)
                pool.report_failure(proxy_url, err_type)
                if err_type == ERROR_PROXY_CONNECTION and proxy_url is not None:
                    log.warning(
                        f"  GraphQL replay page {page_num} 代理失败 ({proxy_label(proxy_url)}), 切换: {e}"
                    )
                    pool.report_switch(proxy_url, None)
                    continue
                log.warning(f"  GraphQL replay page {page_num} request failed ({proxy_label(proxy_url)}): {e}")
                resp = None
                break
        if resp is None:
            break

        if resp.status_code != 200:
            log.warning(f"  GraphQL replay page {page_num} HTTP {resp.status_code}")
            break

        body = resp.text
        if not body or '"ad_archive_id"' not in body:
            log.info(f"  GraphQL replay page {page_num}: no ads in response, stopping")
            break

        # Parse streamed JSON (same format as graphql_sniffer handles)
        page_ads, next_cursor = _parse_graphql_response(body)
        if not page_ads:
            log.info(f"  GraphQL replay page {page_num}: parsed 0 ads, stopping")
            break

        all_ads.extend(page_ads)
        log.info(f"  GraphQL replay page {page_num}: +{len(page_ads)} ads (total {len(all_ads)})")

        cursor = next_cursor
        # Brief randomized pause between requests
        time.sleep(random.uniform(GQL_REPLAY_PAUSE_MIN_SEC, GQL_REPLAY_PAUSE_MAX_SEC))

    return all_ads


def _parse_graphql_response(body: str) -> tuple[list[dict], str]:
    """Parse a GraphQL pagination response. Returns (ads, next_cursor)."""
    from .graphql_sniffer import _walk_and_extract

    ads_state: dict = {'ads': {}, 'graphql_count': 0, 'ad_responses': 0, 'errors': 0}
    next_cursor = ""

    decoder = json.JSONDecoder()
    i = 0
    n = len(body)
    safety = 0

    while i < n:
        while i < n and body[i] in ' \t\r\n,':
            i += 1
        if i >= n:
            break
        if body[i] not in '{[':
            j = body.find('{', i)
            if j < 0:
                break
            i = j
        try:
            obj, end = decoder.raw_decode(body, i)
        except ValueError:
            i += 1
            safety += 1
            if safety > 20000:
                break
            continue

        # Extract cursor
        if isinstance(obj, dict):
            _extract_cursor(obj, lambda c: nonlocal_set(c))
            _walk_and_extract(obj, ads_state)

        i = end

    # Hacky but functional: find forward_cursor in the raw text
    import re
    cursor_match = re.search(r'"forward_cursor"\s*:\s*"([^"]+)"', body)
    if cursor_match:
        next_cursor = cursor_match.group(1)

    return list(ads_state['ads'].values()), next_cursor


def _extract_cursor(obj, setter, depth=0):
    """Recursively find forward_cursor in nested dict."""
    if depth > 20:
        return
    if isinstance(obj, dict):
        if 'forward_cursor' in obj:
            val = obj['forward_cursor']
            if val and isinstance(val, str):
                setter(val)
        for v in obj.values():
            _extract_cursor(v, setter, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _extract_cursor(v, setter, depth + 1)


def nonlocal_set(val):
    """Placeholder — actual cursor extraction uses regex on raw body."""
    pass

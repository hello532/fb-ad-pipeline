"""pipeline/firecrawl_scraper.py — Firecrawl 层 (FB Ad Library).

用 Firecrawl 云 API / 自托管 渲染 FB Ad Library 页面, 再复用 scrapling 的
_parse_static_ads 抽广告字段, 输出与 L0/L1/L2 一致的 result dict.

启用条件 (任一):
  - 环境变量 FB_FIRECRAWL=1 / true / on
  - 环境变量 FB_ENGINE=firecrawl
  - 存在 FIRECRAWL_API_KEY 且 FB_FIRECRAWL 未显式设为 0

Env:
  FIRECRAWL_API_KEY      云 API key (或自托管仍可空, 视服务配置)
  FIRECRAWL_API_URL      默认 https://api.firecrawl.dev; 自托管改 http://127.0.0.1:3002
  FB_FIRECRAWL           1=优先 / 0=关闭 / only=只用 Firecrawl 不回退
  FB_ENGINE              firecrawl 时等同 FB_FIRECRAWL=1
  FB_FIRECRAWL_SCROLLS   滚动次数 (默认 3)
  FB_FIRECRAWL_WAIT_MS   首屏 wait_for 毫秒 (默认 3500)
  FB_FIRECRAWL_TIMEOUT   单次 scrape 超时秒 (默认 90)
  FB_FIRECRAWL_PROXY     auto|basic|stealth|none (默认 auto)
  FB_FIRECRAWL_LOCATION  如 US / US,California (可选)
"""
from __future__ import annotations

import os
import time
from typing import Optional

from core.logger import get_logger

from .commerce_filter import filter_commerce_ads
from .scrapling_scraper import (
    _build_url,
    _is_challenge,
    _parse_static_ads,
    save_result,
)

log = get_logger(__name__)


def _cfg() -> dict:
    """每次调用读 env, 避免 CLI 设 env 后被模块级缓存卡住."""
    return {
        "explicit": os.getenv("FB_FIRECRAWL", "").strip().lower(),
        "engine": os.getenv("FB_ENGINE", "").strip().lower(),
        "scrolls": max(0, min(int(os.getenv("FB_FIRECRAWL_SCROLLS", "3")), 12)),
        "wait_ms": max(500, int(os.getenv("FB_FIRECRAWL_WAIT_MS", "3500"))),
        "timeout": max(15, int(os.getenv("FB_FIRECRAWL_TIMEOUT", "90"))),
        "proxy": os.getenv("FB_FIRECRAWL_PROXY", "auto").strip().lower(),
        "location": os.getenv("FB_FIRECRAWL_LOCATION", "").strip(),
        "api_url": os.getenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev").rstrip("/"),
        "api_key": os.getenv("FIRECRAWL_API_KEY") or None,
    }


def firecrawl_mode() -> str:
    """返回 off / on / only."""
    c = _cfg()
    explicit, engine = c["explicit"], c["engine"]
    if explicit in ("0", "false", "no", "off"):
        return "off"
    if explicit in ("only", "force", "strict"):
        return "only"
    if explicit in ("1", "true", "yes", "on"):
        return "on"
    if engine == "firecrawl":
        return "on"
    # 有 key 或非官方 api_url 默认开启 (可被 FB_FIRECRAWL=0 关掉)
    if c["api_key"] or c["api_url"] not in (
        "https://api.firecrawl.dev",
        "https://api.firecrawl.dev/",
    ):
        return "on"
    return "off"


def firecrawl_enabled() -> bool:
    return firecrawl_mode() != "off"


def _get_client():
    try:
        from firecrawl import Firecrawl
    except ImportError as e:
        raise RuntimeError(
            "firecrawl-py 未安装. 在 scrapling venv 执行: pip install firecrawl-py"
        ) from e
    c = _cfg()
    api_key, api_url = c["api_key"], c["api_url"]
    # 云端必须有 key; 自托管可能不需要
    if not api_key and "api.firecrawl.dev" in api_url:
        raise RuntimeError(
            "缺少 FIRECRAWL_API_KEY. 去 https://www.firecrawl.dev/app/api-keys 创建后 export"
        )
    return Firecrawl(api_key=api_key, api_url=api_url)


def _doc_to_html(doc) -> str:
    """从 Firecrawl Document 抽出最完整 HTML (优先 raw_html)."""
    if doc is None:
        return ""
    # pydantic model or dict
    raw = getattr(doc, "raw_html", None) or getattr(doc, "rawHtml", None)
    html = getattr(doc, "html", None)
    md = getattr(doc, "markdown", None)
    if isinstance(doc, dict):
        raw = raw or doc.get("raw_html") or doc.get("rawHtml")
        html = html or doc.get("html")
        md = md or doc.get("markdown")
        # nested data
        data = doc.get("data") if isinstance(doc.get("data"), dict) else None
        if data:
            raw = raw or data.get("rawHtml") or data.get("raw_html")
            html = html or data.get("html")
            md = md or data.get("markdown")
    # pick richest for ad_archive_id regex
    candidates = [c for c in (raw, html, md) if isinstance(c, str) and c.strip()]
    if not candidates:
        return ""
    # prefer the one that contains ad markers
    for c in candidates:
        if "ad_archive_id" in c:
            return c
    return max(candidates, key=len)


def _build_actions(max_scrolls: int, wait_ms: int) -> list[dict]:
    actions: list[dict] = [
        {"type": "wait", "milliseconds": min(wait_ms, 8000)},
    ]
    for _ in range(max(0, max_scrolls)):
        actions.append({"type": "scroll", "direction": "down"})
        actions.append({"type": "wait", "milliseconds": 800})
    return actions


def firecrawl_scan(
    keyword: str,
    country: str = "US",
    max_scrolls: Optional[int] = None,
) -> Optional[dict]:
    """用 Firecrawl 抓一个关键词. 返回 result dict / challenge dict / None."""
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return None
    country = (country or "US").strip().upper() or "US"
    c = _cfg()
    scrolls = c["scrolls"] if max_scrolls is None else max(0, min(int(max_scrolls), 12))
    url = _build_url(keyword, country)
    t0 = time.perf_counter()

    try:
        client = _get_client()
    except Exception as e:
        log.warning(f"  Firecrawl client 不可用: {e}")
        return None

    kwargs: dict = {
        "formats": ["rawHtml", "html"],
        "only_main_content": False,
        "wait_for": c["wait_ms"],
        "timeout": c["timeout"] * 1000,  # SDK 多为 ms; 若报错下方会降级
        "actions": _build_actions(scrolls, c["wait_ms"]),
        "block_ads": False,
        "remove_base64_images": True,
        "store_in_cache": False,
        "max_age": 0,
    }
    if c["proxy"] and c["proxy"] not in ("none", "off", "0"):
        # auto|basic|stealth 透传; 其它当自定义 proxy URL
        kwargs["proxy"] = c["proxy"]
    if c["location"]:
        # "US" or "US,California"
        parts = [p.strip() for p in c["location"].split(",") if p.strip()]
        if parts:
            loc: dict = {"country": parts[0]}
            if len(parts) > 1:
                loc["languages"] = parts[1:]
            kwargs["location"] = loc

    doc = None
    last_err = None
    # timeout 单位兼容: 先试 ms, 再试秒
    for attempt, timeout_val in enumerate(
        (kwargs["timeout"], c["timeout"], None)
    ):
        try:
            call_kwargs = dict(kwargs)
            if timeout_val is None:
                call_kwargs.pop("timeout", None)
            else:
                call_kwargs["timeout"] = timeout_val
            doc = client.scrape(url, **call_kwargs)
            last_err = None
            break
        except TypeError as e:
            # 旧/新 SDK 参数名差异: 逐步剥离
            last_err = e
            msg = str(e)
            for bad in (
                "timeout",
                "max_age",
                "store_in_cache",
                "remove_base64_images",
                "block_ads",
                "actions",
                "location",
                "proxy",
                "only_main_content",
                "wait_for",
            ):
                if bad in msg and bad in kwargs:
                    kwargs.pop(bad, None)
                    log.debug(f"  Firecrawl scrape 去掉不支持参数: {bad}")
                    break
            else:
                # formats 命名 rawHtml vs raw_html
                if "formats" in msg:
                    kwargs["formats"] = ["html"]
                else:
                    log.warning(f"  Firecrawl TypeError 无法恢复: {e}")
                    break
        except Exception as e:
            last_err = e
            log.warning(f"  Firecrawl scrape 失败 (attempt {attempt+1}): {e}")
            # 参数单位问题再试
            if attempt == 0 and "timeout" in kwargs:
                continue
            break

    elapsed = time.perf_counter() - t0
    if doc is None:
        log.warning(f"  Firecrawl 无文档返回 ({elapsed:.1f}s): {last_err}")
        return None

    body = _doc_to_html(doc)
    if not body:
        log.warning(f"  Firecrawl 空 HTML ({elapsed:.1f}s): {keyword}")
        return None

    if _is_challenge(body):
        log.warning(f"  Firecrawl 风控检测: {keyword}")
        return {
            "keyword": keyword,
            "count": 0,
            "ads": [],
            "elapsed_sec": round(elapsed, 1),
            "source": "firecrawl",
            "challenge": True,
            "country": country,
        }

    ads = _parse_static_ads(body)
    before = len(ads)
    ads, drop_stats = filter_commerce_ads(ads)
    if drop_stats:
        top = sorted(drop_stats.items(), key=lambda kv: -kv[1])[:3]
        reasons = ", ".join(f"{k}={v}" for k, v in top)
        log.info(
            f"  Firecrawl commerce filter: kept {len(ads)}/{before} "
            f"(dropped {sum(drop_stats.values())}: {reasons})"
        )

    result = {
        "keyword": keyword,
        "count": len(ads),
        "ads": ads,
        "elapsed_sec": round(elapsed, 1),
        "source": "firecrawl",
        "country": country,
    }
    log.info(f"  Firecrawl {keyword}: {len(ads)} ads, {elapsed:.1f}s")
    return result


def scrape_via_firecrawl(
    keyword: str,
    country: str = "US",
    max_scrolls: Optional[int] = None,
):
    """高层入口: 返回 Path / 'CHALLENGE' / None (与 scrape_keyword 一致)."""
    if not firecrawl_enabled():
        return None
    result = firecrawl_scan(keyword, country=country, max_scrolls=max_scrolls)
    if result is None:
        return None
    if result.get("challenge"):
        return "CHALLENGE"
    if result.get("count", 0) <= 0:
        # 0 命中: only 模式当作空; on 模式让上层回退
        if firecrawl_mode() == "only":
            # 仍保存空结果? 与 L0 一致返回 None
            return None
        return None
    # 注入 country 到 save payload
    out = save_result(result)
    return out

"""pipeline/orchestrator.py — Structured fallback orchestrator.

Replaces the ad-hoc triple_scan() + scrape_keyword() chain with:
- Explicit layer state machine: L0 → L1.5 → L1 → L1.3 → L2
- Error classification per layer attempt
- Per-keyword trace: which layers tried, why each failed, proxy switches
- Smart routing: different errors → different next steps
- Async loop detection handled properly (no silent skips)
- Structured ScrapeResult for observability

Usage:
    from pipeline.orchestrator import Orchestrator
    orch = Orchestrator()
    result = orch.scrape(keyword, country="US", headless=True, max_pages=5)
    # result.trace shows every layer attempted and why
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.logger import get_logger

log = get_logger(__name__)


@dataclass
class LayerAttempt:
    layer: str
    status: str = "pending"  # "success", "zero", "challenge", "proxy_fail", "browser_fail",
                 # "page_load_fail", "js_fail", "graphql_fail", "async_loop",
                 # "not_installed", "skipped", "error"
    proxy_used: Optional[str] = None
    proxy_switched: bool = False
    direct_takeover: bool = False
    duration_sec: float = 0.0
    ads_count: int = 0
    error_detail: str = ""


@dataclass
class ScrapeResult:
    keyword: str
    country: str
    final_status: str = "pending"  # "success", "zero", "challenge", "failed"
    output_path: Optional[Path] = None
    total_duration_sec: float = 0.0
    ads_count: int = 0
    source_layer: str = ""
    trace: list[LayerAttempt] = field(default_factory=list)
    proxy_switches: int = 0
    direct_takeovers: int = 0

    @property
    def legacy_return(self):
        if self.final_status == "success":
            return self.output_path
        if self.final_status == "challenge":
            return "CHALLENGE"
        return None

    def trace_summary(self) -> str:
        parts = []
        for a in self.trace:
            tag = f"{a.layer}:{a.status}"
            if a.ads_count > 0:
                tag += f"({a.ads_count})"
            if a.proxy_switched:
                tag += "[switch]"
            if a.direct_takeover:
                tag += "[direct]"
            if a.error_detail:
                tag += f"[{a.error_detail[:40]}]"
            parts.append(tag)
        return " → ".join(parts)


def _classify_scrape_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if "net::err_connection_closed" in text or "proxy" in text or "tunnel" in text:
        return "proxy_fail"
    if "browser" in text or "executable" in text or "launch" in text:
        return "browser_fail"
    if "goto" in text or "navigation" in text or "timeout" in text:
        return "page_load_fail"
    if "context" in text and "destroyed" in text:
        return "js_fail"
    if "async" in text and "loop" in text:
        return "async_loop"
    return "error"


class Orchestrator:
    def __init__(self, stats: Optional[dict] = None):
        self._stats = stats or {}
        self._lock = threading.Lock()
        self._layer_stats: dict[str, dict] = {}

    def scrape(
        self,
        keyword: str,
        country: str = "US",
        headless: bool = True,
        max_pages: Optional[int] = None,
    ) -> ScrapeResult:
        from .scrapling_scraper import SCRAPLING_MODE, MAX_PAGES

        t0 = time.perf_counter()
        result = ScrapeResult(keyword=keyword, country=country)
        pages = max_pages if max_pages is not None else MAX_PAGES

        try:
            from .proxy_pool import scrape_proxy_context

            with scrape_proxy_context():
                if SCRAPLING_MODE != "off":
                    self._try_l0(result, keyword, country)
                    if result.final_status in ("success", "challenge"):
                        return self._finalize(result, t0)
                    if result.final_status == "zero":
                        return self._finalize(result, t0)

                    if SCRAPLING_MODE != "quick":
                        if pages > 1:
                            self._try_l15(result, keyword, country, headless, pages)
                            if result.final_status in ("success", "challenge", "zero"):
                                return self._finalize(result, t0)

                        self._try_l1(result, keyword, country, headless)
                        if result.final_status in ("success", "challenge", "zero"):
                            return self._finalize(result, t0)

                        self._try_l13(result, keyword, country, headless)
                        if result.final_status in ("success", "challenge"):
                            return self._finalize(result, t0)

                self._try_l2(result, keyword, country, headless, max_pages)

        except Exception as exc:
            result.trace.append(LayerAttempt(
                layer="orchestrator",
                status="error",
                error_detail=str(exc)[:200],
            ))
            log.exception(f"  orchestrator fatal: {keyword}")

        return self._finalize(result, t0)

    def _try_l0(self, result: ScrapeResult, keyword: str, country: str):
        t0 = time.perf_counter()
        attempt = LayerAttempt(layer="L0")
        try:
            from .scrapling_scraper import quick_scan
            l0 = quick_scan(keyword, country)
            attempt.duration_sec = time.perf_counter() - t0

            if l0 is None:
                attempt.status = "not_available"
            elif l0.get("challenge"):
                attempt.status = "challenge"
                result.final_status = "challenge"
            elif l0["count"] == 0:
                attempt.status = "zero"
                result.final_status = "zero"
            else:
                attempt.status = "has_results"
                attempt.ads_count = l0["count"]
        except Exception as exc:
            attempt.status = _classify_scrape_error(exc)
            attempt.error_detail = str(exc)[:200]
            attempt.duration_sec = time.perf_counter() - t0
        result.trace.append(attempt)
        self._record_layer("L0", attempt.status)

    def _try_l15(self, result: ScrapeResult, keyword: str, country: str,
                 headless: bool, max_pages: int):
        t0 = time.perf_counter()
        attempt = LayerAttempt(layer="L1.5")
        try:
            from .scrapling_scraper import _patchright_paginate, save_result
            l15 = _patchright_paginate(keyword, country, headless=headless, max_pages=max_pages)
            attempt.duration_sec = time.perf_counter() - t0

            if l15 is None:
                attempt.status = "error"
                attempt.error_detail = "returned None"
            elif l15.get("challenge"):
                attempt.status = "challenge"
                result.final_status = "challenge"
            elif l15["count"] > 0:
                out = save_result(l15)
                attempt.status = "success"
                attempt.ads_count = l15["count"]
                result.final_status = "success"
                result.output_path = out
                result.ads_count = l15["count"]
                result.source_layer = "L1.5"
            else:
                attempt.status = "zero"
                result.final_status = "zero"
        except Exception as exc:
            attempt.status = _classify_scrape_error(exc)
            attempt.error_detail = str(exc)[:200]
            attempt.duration_sec = time.perf_counter() - t0
        result.trace.append(attempt)
        self._record_layer("L1.5", attempt.status)

    def _try_l1(self, result: ScrapeResult, keyword: str, country: str, headless: bool):
        t0 = time.perf_counter()
        attempt = LayerAttempt(layer="L1")
        try:
            from .scrapling_scraper import stealthy_scan, save_result
            l1 = stealthy_scan(keyword, country, headless=headless)
            attempt.duration_sec = time.perf_counter() - t0

            if l1 is None:
                attempt.status = "error"
                attempt.error_detail = "returned None"
            elif l1.get("challenge"):
                attempt.status = "challenge"
                result.final_status = "challenge"
            elif l1["count"] > 0:
                out = save_result(l1)
                attempt.status = "success"
                attempt.ads_count = l1["count"]
                result.final_status = "success"
                result.output_path = out
                result.ads_count = l1["count"]
                result.source_layer = "L1"
            else:
                attempt.status = "zero"
                result.final_status = "zero"
        except Exception as exc:
            attempt.status = _classify_scrape_error(exc)
            attempt.error_detail = str(exc)[:200]
            attempt.duration_sec = time.perf_counter() - t0
        result.trace.append(attempt)
        self._record_layer("L1", attempt.status)

    def _try_l13(self, result: ScrapeResult, keyword: str, country: str, headless: bool):
        t0 = time.perf_counter()
        attempt = LayerAttempt(layer="L1.3")
        try:
            from .camoufox_scraper import camoufox_scan
            r = camoufox_scan(keyword, country=country, headless=headless)
            attempt.duration_sec = time.perf_counter() - t0

            if r == "CHALLENGE":
                attempt.status = "challenge"
                result.final_status = "challenge"
            elif r is not None and isinstance(r, Path):
                attempt.status = "success"
                result.final_status = "success"
                result.output_path = r
                result.source_layer = "L1.3"
            else:
                attempt.status = "error"
                attempt.error_detail = "returned None"
        except ImportError:
            attempt.status = "not_installed"
            attempt.error_detail = "camoufox not available"
            attempt.duration_sec = time.perf_counter() - t0
        except Exception as exc:
            attempt.status = _classify_scrape_error(exc)
            attempt.error_detail = str(exc)[:200]
            attempt.duration_sec = time.perf_counter() - t0
        result.trace.append(attempt)
        self._record_layer("L1.3", attempt.status)

    def _try_l2(self, result: ScrapeResult, keyword: str, country: str,
                headless: bool, max_pages: Optional[int]):
        t0 = time.perf_counter()
        attempt = LayerAttempt(layer="L2")
        try:
            from .scraper import scrape_keyword, _has_running_asyncio_loop, _run_l2_fallback_in_clean_thread

            if _has_running_asyncio_loop():
                attempt.layer = "L2-thread"
                log.info(f"  orchestrator: L2 via clean thread (async loop active): {keyword}")
                r = _run_l2_fallback_in_clean_thread(keyword, 12, headless, country, max_pages)
            else:
                r = scrape_keyword(keyword, headless=headless, country=country,
                                   max_pages=max_pages, _force_l2=True)
            attempt.duration_sec = time.perf_counter() - t0

            if r == "CHALLENGE":
                attempt.status = "challenge"
                result.final_status = "challenge"
            elif r is not None and isinstance(r, Path):
                attempt.status = "success"
                result.final_status = "success"
                result.output_path = r
                result.source_layer = attempt.layer
            elif r is None:
                attempt.status = "zero"
                if result.final_status not in ("challenge",):
                    result.final_status = "zero"
            else:
                attempt.status = "error"
                attempt.error_detail = f"unexpected return: {type(r)}"
        except Exception as exc:
            attempt.status = _classify_scrape_error(exc)
            attempt.error_detail = str(exc)[:200]
            attempt.duration_sec = time.perf_counter() - t0
        result.trace.append(attempt)
        self._record_layer("L2", attempt.status)

    def _finalize(self, result: ScrapeResult, t0: float) -> ScrapeResult:
        result.total_duration_sec = round(time.perf_counter() - t0, 2)
        result.proxy_switches = sum(1 for a in result.trace if a.proxy_switched)
        result.direct_takeovers = sum(1 for a in result.trace if a.direct_takeover)

        if result.final_status == "pending":
            result.final_status = "failed"

        log.info(
            f"  orchestrator: {result.keyword} → {result.final_status} "
            f"({result.total_duration_sec:.1f}s, {result.ads_count} ads, "
            f"layer={result.source_layer or 'none'}) "
            f"trace=[{result.trace_summary()}]"
        )
        return result

    def _record_layer(self, layer: str, status: str):
        with self._lock:
            if layer not in self._layer_stats:
                self._layer_stats[layer] = {"calls": 0, "success": 0, "zero": 0,
                                             "challenge": 0, "error": 0}
            self._layer_stats[layer]["calls"] += 1
            if status == "success" or status == "has_results":
                self._layer_stats[layer]["success"] += 1
            elif status == "zero":
                self._layer_stats[layer]["zero"] += 1
            elif status == "challenge":
                self._layer_stats[layer]["challenge"] += 1
            else:
                self._layer_stats[layer]["error"] += 1

    def layer_stats(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._layer_stats.items()}


_orch: Optional[Orchestrator] = None
_orch_lock = threading.Lock()


def get_orchestrator() -> Orchestrator:
    global _orch
    if _orch is None:
        with _orch_lock:
            if _orch is None:
                _orch = Orchestrator()
    return _orch

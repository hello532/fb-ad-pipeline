"""pipeline/proxy_pool.py — Production proxy runtime with health tracking.

Replaces the single FB_PROXY string with a proper proxy management layer:
- Multiple proxy candidates (FB_PROXY_LIST env, comma-separated)
- Per-proxy health: success/fail counts, consecutive failures, circuit breaker
- Weighted selection: prefer proxies with higher recent success rate
- Automatic direct fallback when all proxies are fused
- Thread-safe, shared across L0/L1/L1.3/L1.5/L2/GraphQL replay
- Statistics output for observability

Usage:
    from pipeline.proxy_pool import get_pool
    pool = get_pool()
    proxy = pool.acquire()        # get best available proxy (or None=direct)
    pool.report_success(proxy)    # after successful request
    pool.report_failure(proxy, "proxy_connection_error")  # after failure
    pool.stats()                  # {candidates, healthy, fused, ...}
"""
from __future__ import annotations

import os
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

from core.logger import get_logger

log = get_logger(__name__)

FUSE_THRESHOLD = int(os.getenv("FB_PROXY_FUSE_THRESHOLD", "5"))
FUSE_COOLDOWN_SEC = float(os.getenv("FB_PROXY_FUSE_COOLDOWN_SEC", "120"))
HEALTH_WINDOW_SEC = float(os.getenv("FB_PROXY_HEALTH_WINDOW_SEC", "300"))
DIRECT_FALLBACK = os.getenv("FB_PROXY_FALLBACK_DIRECT", "0").lower() not in ("0", "false", "no")
ROTATE_PER_SCRAPE = os.getenv("FB_ROTATE_PROXY_PER_SCRAPE", "1").lower() not in ("0", "false", "no")
_SCRAPE_LOCAL = threading.local()

ERROR_PROXY_CONNECTION = "proxy_connection_error"
ERROR_CHALLENGE = "challenge"
ERROR_BROWSER = "browser_error"
ERROR_TIMEOUT = "timeout"
ERROR_PAGE_LOAD = "page_load_error"
ERROR_OTHER = "other"

_PROXY_ERROR_MARKERS = (
    "err_connection_closed", "err_proxy_connection_failed",
    "err_tunnel_connection_failed", "proxy", "connection reset",
    "connection aborted", "econnreset", "closed",
    "dial", "proxy auth", "tunnel",
)


def classify_error(exc: BaseException) -> str:
    text = str(exc).lower()
    if any(m in text for m in _PROXY_ERROR_MARKERS):
        return ERROR_PROXY_CONNECTION
    if "challenge" in text or "rate limit" in text or "restricted" in text:
        return ERROR_CHALLENGE
    if "timeout" in text or "timed out" in text:
        return ERROR_TIMEOUT
    if "browser" in text or "launch" in text or "executable" in text:
        return ERROR_BROWSER
    if "goto" in text or "navigation" in text or "net::" in text:
        return ERROR_PAGE_LOAD
    return ERROR_OTHER


@dataclass
class ProxyEntry:
    url: Optional[str]
    success_count: int = 0
    fail_count: int = 0
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    fused_until: float = 0.0
    total_switches_to: int = 0
    error_counts: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.url or "direct"

    @property
    def is_fused(self) -> bool:
        if self.fused_until <= 0:
            return False
        return time.monotonic() < self.fused_until

    @property
    def score(self) -> float:
        total = self.success_count + self.fail_count
        if total == 0:
            return 0.5
        base = self.success_count / total
        recency_bonus = 0.0
        if self.last_success_time > 0:
            age = time.monotonic() - self.last_success_time
            if age < HEALTH_WINDOW_SEC:
                recency_bonus = 0.2 * (1.0 - age / HEALTH_WINDOW_SEC)
        return min(1.0, base + recency_bonus)


class ProxyPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries: dict[Optional[str], ProxyEntry] = {}
        self._switch_count = 0
        self._direct_takeover_count = 0
        self._init_from_env()

    def _init_from_env(self):
        proxy_list_raw = os.getenv("FB_PROXY_LIST", "")
        single_proxy = os.getenv("FB_PROXY", "http://127.0.0.1:6152")

        candidates: list[Optional[str]] = []

        if proxy_list_raw.strip():
            for p in proxy_list_raw.split(","):
                p = p.strip()
                if p and p.lower() not in ("", "direct", "none", "null"):
                    candidates.append(p)

        if not candidates and single_proxy:
            norm = single_proxy.strip()
            if norm.lower() not in ("", "direct", "none", "null", "0", "false", "off"):
                candidates.append(norm)

        if DIRECT_FALLBACK or not candidates:
            candidates.append(None)

        seen = set()
        for c in candidates:
            key = c
            if key not in seen:
                seen.add(key)
                self._entries[key] = ProxyEntry(url=c)

        labels = [e.label for e in self._entries.values()]
        log.info(f"proxy_pool: initialized {len(self._entries)} candidates: {labels}")

    def acquire(self, *, prefer_proxy: bool = True) -> Optional[str]:
        scoped_proxy = getattr(_SCRAPE_LOCAL, "proxy_url", None)
        if ROTATE_PER_SCRAPE and getattr(_SCRAPE_LOCAL, "active", False):
            return scoped_proxy

        return self._pick_proxy(prefer_proxy=prefer_proxy)

    def acquire_sequence(self) -> list[Optional[str]]:
        with self._lock:
            healthy = [e for e in self._entries.values() if not e.is_fused]
            if not healthy:
                sequence = [e.url for e in sorted(self._entries.values(),
                                                  key=lambda e: e.fused_until)]
            else:
                ordered = sorted(healthy, key=lambda e: -e.score)
                sequence = [e.url for e in ordered]

        scoped_proxy = getattr(_SCRAPE_LOCAL, "proxy_url", None)
        if ROTATE_PER_SCRAPE and getattr(_SCRAPE_LOCAL, "active", False):
            return [scoped_proxy] + [p for p in sequence if p != scoped_proxy]
        return sequence

    def _pick_proxy(self, *, prefer_proxy: bool = True) -> Optional[str]:
        with self._lock:
            healthy = [e for e in self._entries.values() if not e.is_fused]
            if not healthy:
                best = self._least_fused()
                if best:
                    log.warning(f"proxy_pool: all fused, force-using least-fused: {best.label}")
                    return best.url
                return None

            if prefer_proxy:
                proxy_entries = [e for e in healthy if e.url is not None]
                if proxy_entries:
                    return self._weighted_pick(proxy_entries).url

            return self._weighted_pick(healthy).url

    def report_success(self, proxy_url: Optional[str]):
        with self._lock:
            entry = self._entries.get(proxy_url)
            if entry is None:
                return
            entry.success_count += 1
            entry.consecutive_failures = 0
            entry.last_success_time = time.monotonic()

    def report_failure(self, proxy_url: Optional[str], error_type: str = ERROR_OTHER):
        with self._lock:
            entry = self._entries.get(proxy_url)
            if entry is None:
                return
            entry.fail_count += 1
            entry.consecutive_failures += 1
            entry.last_failure_time = time.monotonic()
            entry.error_counts[error_type] = entry.error_counts.get(error_type, 0) + 1

            if (entry.consecutive_failures >= FUSE_THRESHOLD
                    and error_type in (ERROR_PROXY_CONNECTION, ERROR_TIMEOUT)):
                cooldown = FUSE_COOLDOWN_SEC * (1 + random.uniform(-0.1, 0.1))
                entry.fused_until = time.monotonic() + cooldown
                log.warning(
                    f"proxy_pool: FUSED {entry.label} "
                    f"({entry.consecutive_failures} consecutive {error_type}), "
                    f"cooldown {cooldown:.0f}s"
                )

    def report_switch(self, from_proxy: Optional[str], to_proxy: Optional[str]):
        with self._lock:
            self._switch_count += 1
            to_entry = self._entries.get(to_proxy)
            if to_entry:
                to_entry.total_switches_to += 1
            if to_proxy is None and from_proxy is not None:
                self._direct_takeover_count += 1

    def stats(self) -> dict:
        with self._lock:
            entries = []
            for e in self._entries.values():
                entries.append({
                    "url": e.label,
                    "success": e.success_count,
                    "fail": e.fail_count,
                    "consecutive_failures": e.consecutive_failures,
                    "fused": e.is_fused,
                    "score": round(e.score, 3),
                    "error_types": dict(e.error_counts),
                    "switches_to": e.total_switches_to,
                })
            healthy = sum(1 for e in self._entries.values() if not e.is_fused)
            fused = len(self._entries) - healthy
            return {
                "candidates": len(self._entries),
                "healthy": healthy,
                "fused": fused,
                "total_switches": self._switch_count,
                "direct_takeovers": self._direct_takeover_count,
                "entries": entries,
            }

    def _weighted_pick(self, entries: list[ProxyEntry]) -> ProxyEntry:
        if len(entries) == 1:
            return entries[0]
        weights = [max(0.01, e.score) for e in entries]
        total = sum(weights)
        r = random.uniform(0, total)
        acc = 0.0
        for e, w in zip(entries, weights):
            acc += w
            if r <= acc:
                return e
        return entries[-1]

    def _least_fused(self) -> Optional[ProxyEntry]:
        fused = [e for e in self._entries.values() if e.is_fused]
        if not fused:
            return None
        return min(fused, key=lambda e: e.fused_until)


_pool: Optional[ProxyPool] = None
_pool_lock = threading.Lock()


def get_pool() -> ProxyPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ProxyPool()
    return _pool


@contextmanager
def scrape_proxy_context():
    if not ROTATE_PER_SCRAPE:
        yield None
        return

    previous_active = getattr(_SCRAPE_LOCAL, "active", False)
    previous_proxy = getattr(_SCRAPE_LOCAL, "proxy_url", None)
    if previous_active:
        yield previous_proxy
        return

    proxy_url = get_pool()._pick_proxy(prefer_proxy=True)
    _SCRAPE_LOCAL.active = True
    _SCRAPE_LOCAL.proxy_url = proxy_url
    try:
        yield proxy_url
    finally:
        _SCRAPE_LOCAL.active = previous_active
        _SCRAPE_LOCAL.proxy_url = previous_proxy


def proxy_for_playwright(proxy_url: Optional[str]) -> Optional[dict]:
    if proxy_url is None:
        return None
    return {"server": proxy_url}

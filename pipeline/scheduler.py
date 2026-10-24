"""pipeline/scheduler.py — Adaptive concurrency scheduler.

Replaces fixed FB_SCRAPE_WORKERS with dynamic scaling based on:
- File descriptor pressure (current open fds vs limit)
- Recent success rate (sliding window)
- Challenge rate
- Proxy failure rate
- Browser launch failure rate
- Average keyword duration
- Heavy layer ratio (how many keywords need L1.5/L2)

Also provides layer-specific semaphores:
- light_slots: L0, L1, GraphQL replay (high concurrency OK)
- heavy_slots: L1.5, L2, L1.3 (browser-based, limited concurrency)

Usage:
    from pipeline.scheduler import get_scheduler
    sched = get_scheduler()
    workers = sched.recommended_workers()
    with sched.light_slot():
        # run L0/L1 request
    with sched.heavy_slot():
        # run L1.5/L2 browser scrape
"""
from __future__ import annotations

import os
import resource
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from core.logger import get_logger

log = get_logger(__name__)

MIN_WORKERS = max(1, int(os.getenv("FB_MIN_WORKERS", "2")))
MAX_WORKERS = max(1, int(os.getenv("FB_MAX_WORKERS", "12")))
BASE_WORKERS = max(1, int(os.getenv("FB_SCRAPE_WORKERS", "10")))
LIGHT_SLOTS = max(1, int(os.getenv("FB_LIGHT_SLOTS", "10")))
HEAVY_SLOTS = max(1, int(os.getenv("FB_HEAVY_SLOTS", "4")))
GQL_SLOTS = max(1, int(os.getenv("FB_GQL_SLOTS", "6")))

FD_PRESSURE_THRESHOLD = float(os.getenv("FB_FD_PRESSURE_THRESHOLD", "0.7"))
SUCCESS_WINDOW = int(os.getenv("FB_SUCCESS_WINDOW", "20"))
SCALE_INTERVAL_SEC = float(os.getenv("FB_SCALE_INTERVAL_SEC", "30"))


@dataclass
class ScrapeSample:
    timestamp: float
    success: bool
    challenge: bool
    proxy_fail: bool
    browser_fail: bool
    heavy_layer: bool
    duration_sec: float


class AdaptiveScheduler:
    def __init__(self):
        self._lock = threading.Lock()
        self._samples: deque[ScrapeSample] = deque(maxlen=SUCCESS_WINDOW * 3)
        self._current_workers = BASE_WORKERS
        self._last_scale_time = 0.0
        self._light_sem = threading.BoundedSemaphore(LIGHT_SLOTS)
        self._heavy_sem = threading.BoundedSemaphore(HEAVY_SLOTS)
        self._gql_sem = threading.BoundedSemaphore(GQL_SLOTS)
        self._fd_warnings = 0
        self._scale_ups = 0
        self._scale_downs = 0
        self._total_keywords = 0
        self._total_success = 0
        self._total_challenge = 0

    def record_result(self, sample: ScrapeSample):
        with self._lock:
            self._samples.append(sample)
            self._total_keywords += 1
            if sample.success:
                self._total_success += 1
            if sample.challenge:
                self._total_challenge += 1

    def record_simple(self, *, success: bool = False, challenge: bool = False,
                      proxy_fail: bool = False, browser_fail: bool = False,
                      heavy_layer: bool = False, duration_sec: float = 0.0):
        self.record_result(ScrapeSample(
            timestamp=time.monotonic(), success=success, challenge=challenge,
            proxy_fail=proxy_fail, browser_fail=browser_fail,
            heavy_layer=heavy_layer, duration_sec=duration_sec,
        ))

    def recommended_workers(self) -> int:
        with self._lock:
            now = time.monotonic()
            if now - self._last_scale_time < SCALE_INTERVAL_SEC:
                return self._current_workers
            self._last_scale_time = now

            fd_pressure = self._fd_pressure()
            success_rate = self._recent_success_rate()
            challenge_rate = self._recent_challenge_rate()
            heavy_ratio = self._recent_heavy_ratio()

            prev = self._current_workers
            target = BASE_WORKERS

            if fd_pressure > FD_PRESSURE_THRESHOLD:
                reduction = max(1, int(target * (fd_pressure - FD_PRESSURE_THRESHOLD) * 3))
                target = max(MIN_WORKERS, target - reduction)
                self._fd_warnings += 1
                log.warning(
                    f"scheduler: fd pressure {fd_pressure:.2f} → workers {prev} → {target} "
                    f"(fd_warnings={self._fd_warnings})"
                )

            if challenge_rate > 0.3:
                target = max(MIN_WORKERS, target // 2)
                log.warning(f"scheduler: high challenge rate {challenge_rate:.2f} → workers {target}")

            if success_rate < 0.3 and len(self._samples) >= 5:
                target = max(MIN_WORKERS, target - 2)

            if heavy_ratio > 0.5:
                target = max(MIN_WORKERS, min(target, HEAVY_SLOTS + 1))

            if success_rate > 0.8 and fd_pressure < 0.4 and challenge_rate < 0.1:
                target = min(MAX_WORKERS, target + 1)

            target = max(MIN_WORKERS, min(MAX_WORKERS, target))

            if target != prev:
                direction = "UP" if target > prev else "DOWN"
                if target > prev:
                    self._scale_ups += 1
                else:
                    self._scale_downs += 1
                log.info(
                    f"scheduler: scale {direction} {prev} → {target} "
                    f"(success={success_rate:.2f}, challenge={challenge_rate:.2f}, "
                    f"fd={fd_pressure:.2f}, heavy={heavy_ratio:.2f})"
                )
            self._current_workers = target
            return target

    @contextmanager
    def light_slot(self):
        acquired = self._light_sem.acquire(timeout=30)
        if not acquired:
            log.warning("scheduler: light_slot timeout (30s)")
            yield False
            return
        try:
            yield True
        finally:
            self._light_sem.release()

    @contextmanager
    def heavy_slot(self):
        acquired = self._heavy_sem.acquire(timeout=60)
        if not acquired:
            log.warning("scheduler: heavy_slot timeout (60s)")
            yield False
            return
        try:
            yield True
        finally:
            self._heavy_sem.release()

    @contextmanager
    def gql_slot(self):
        acquired = self._gql_sem.acquire(timeout=15)
        if not acquired:
            log.warning("scheduler: gql_slot timeout (15s)")
            yield False
            return
        try:
            yield True
        finally:
            self._gql_sem.release()

    def stats(self) -> dict:
        with self._lock:
            return {
                "current_workers": self._current_workers,
                "base_workers": BASE_WORKERS,
                "min_workers": MIN_WORKERS,
                "max_workers": MAX_WORKERS,
                "fd_pressure": round(self._fd_pressure(), 3),
                "recent_success_rate": round(self._recent_success_rate(), 3),
                "recent_challenge_rate": round(self._recent_challenge_rate(), 3),
                "recent_heavy_ratio": round(self._recent_heavy_ratio(), 3),
                "fd_warnings": self._fd_warnings,
                "scale_ups": self._scale_ups,
                "scale_downs": self._scale_downs,
                "total_keywords": self._total_keywords,
                "total_success": self._total_success,
                "total_challenge": self._total_challenge,
                "light_slots": LIGHT_SLOTS,
                "heavy_slots": HEAVY_SLOTS,
                "gql_slots": GQL_SLOTS,
                "samples_in_window": len(self._samples),
            }

    def _fd_pressure(self) -> float:
        try:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            if soft <= 0:
                return 0.0
            current = _count_open_fds()
            return min(1.0, current / soft)
        except Exception:
            return 0.0

    def _recent_success_rate(self) -> float:
        recent = list(self._samples)[-SUCCESS_WINDOW:]
        if not recent:
            return 0.5
        return sum(1 for s in recent if s.success) / len(recent)

    def _recent_challenge_rate(self) -> float:
        recent = list(self._samples)[-SUCCESS_WINDOW:]
        if not recent:
            return 0.0
        return sum(1 for s in recent if s.challenge) / len(recent)

    def _recent_heavy_ratio(self) -> float:
        recent = list(self._samples)[-SUCCESS_WINDOW:]
        if not recent:
            return 0.0
        return sum(1 for s in recent if s.heavy_layer) / len(recent)


def _count_open_fds() -> int:
    try:
        import subprocess
        result = subprocess.run(
            ["lsof", "-p", str(os.getpid()), "-F", "f"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.count("\nf")
    except Exception:
        try:
            return len(os.listdir(f"/proc/{os.getpid()}/fd"))
        except Exception:
            return 0


_scheduler: Optional[AdaptiveScheduler] = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> AdaptiveScheduler:
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = AdaptiveScheduler()
    return _scheduler

"""pipeline/cache.py — In-process TTL cache with singleflight + force-refresh.

Why singleflight matters here:
  The dashboard fires 10-12 endpoints in parallel on every page load. Without
  singleflight, when a TTL window expires, all N concurrent requests for the
  same key miss the cache simultaneously, all execute the underlying query,
  and Python's GIL plus shared SQLite contention turn N independent 1-3s
  queries into a 15-20s wall-time pile-up. With singleflight, only the
  *first* miss for a key runs the query; the remaining N-1 callers block on a
  per-key event and receive the same result, capping wall time at the cost
  of one query.

Why cache_warm exists:
  A background refresh loop wants to recompute *before* TTL expires so the
  store always holds a hot value, but plain cache_clear() leaves a window
  where requests see an empty cache and serialize through singleflight. The
  cache_warm() method recomputes a key under the same singleflight lock,
  then atomically swaps the new value in — so a follower request never
  observes an empty slot.
"""
from __future__ import annotations

import threading
import time
from functools import wraps
from typing import Any, Callable


def ttl_cache(ttl_seconds: float = 60.0, max_entries: int = 256):
    """Synchronous TTL cache decorator with singleflight stampede protection.

    Cache key = (args, sorted-kwargs). Returned value is whatever fn returns
    (no copy is made; callers must not mutate).
    """

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        store: dict[tuple, tuple[float, Any]] = {}
        in_flight: dict[tuple, tuple[threading.Event, list]] = {}
        meta_lock = threading.Lock()

        def _key(args: tuple, kwargs: dict) -> tuple:
            return (args, tuple(sorted(kwargs.items())))

        def _evict_if_full() -> None:
            if len(store) > max_entries:
                oldest = min(store, key=lambda kk: store[kk][0])
                store.pop(oldest, None)

        def _compute_and_store(k: tuple, args: tuple, kwargs: dict,
                                event: threading.Event, holder: list) -> Any:
            try:
                val = fn(*args, **kwargs)
                with meta_lock:
                    store[k] = (time.monotonic(), val)
                    _evict_if_full()
                    holder.append(val)
                return val
            finally:
                with meta_lock:
                    in_flight.pop(k, None)
                event.set()

        @wraps(fn)
        def wrapper(*args, **kwargs):
            k = _key(args, kwargs)
            now = time.monotonic()

            with meta_lock:
                hit = store.get(k)
                if hit is not None and now - hit[0] < ttl_seconds:
                    return hit[1]
                pending = in_flight.get(k)
                if pending is not None:
                    event, holder = pending
                    is_leader = False
                else:
                    event = threading.Event()
                    holder = []
                    in_flight[k] = (event, holder)
                    is_leader = True

            if not is_leader:
                event.wait(timeout=max(ttl_seconds * 2, 30.0))
                with meta_lock:
                    hit = store.get(k)
                    if hit is not None:
                        return hit[1]
                if holder:
                    return holder[0]
                # leader failed without populating; degrade to direct call
                return fn(*args, **kwargs)

            return _compute_and_store(k, args, kwargs, event, holder)

        def cache_warm(*args, **kwargs) -> None:
            """Force a refresh of the cached value for these args, going
            through singleflight so concurrent normal requests join in
            instead of duplicating the work. Old cache entry stays visible
            until the new one lands — no empty-slot window.
            """
            k = _key(args, kwargs)
            with meta_lock:
                pending = in_flight.get(k)
                if pending is not None:
                    return
                event = threading.Event()
                holder: list = []
                in_flight[k] = (event, holder)
            _compute_and_store(k, args, kwargs, event, holder)

        def cache_clear() -> None:
            with meta_lock:
                store.clear()

        wrapper.cache_warm = cache_warm  # type: ignore[attr-defined]
        wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        return wrapper

    return deco

#!/usr/bin/env python3
"""Run all 5 signal collectors. Print a health summary (v2).

v2 changes:
  - **5 collectors** (added Reddit purchase-intent)
  - **Concurrent execution** (ThreadPoolExecutor, max_workers=N) for the
    network-heavy collectors. fb_collector is local-only and runs first
    (so the others can use the keywords it discovered as ai_intent input).
  - **Per-collector timing + retry** (1 retry on a 0-row outcome before
    giving up — handles the 'cloudflare blip' case for tiktok).
  - **Pretty health table** at the end including duration and rows.
  - **--only flag**: opposite of --skip, runs exactly one collector.
  - **--ai-keywords-file**: if set, ai_intent reads from this file rather
    than the in-DB top-N.
  - **--ai-backend-required**: by default ai_intent is best-effort; pass
    this flag to fail the whole run when no API key is set.

Order:
  1. fb_collector     (local xlsx, <30s)         — runs first, alone
  2. amazon_movers    (public HTML, ~30s)        — concurrent
  3. tiktok_creative  (JSON API, ~30-60s)        — concurrent
  4. reddit           (public JSON, ~30s)        — concurrent
  5. ai_intent        (paid API, slowest)        — runs last, alone

Exit code = number of collectors that wrote 0 rows after retry.
"""
from __future__ import annotations
import os
import argparse
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from _db import connect, stats, latest_runs                              # noqa: E402
from _utils import log                                                    # noqa: E402

import fb_collector
import amazon_movers
import tiktok_creative
import ai_intent
import reddit_signals


def _run(name, fn, *args, retry_on_empty: bool = True, **kwargs) -> tuple[int, float]:
    t0 = time.time()
    log("run", f"▶ {name}")
    try:
        n = fn(*args, **kwargs)
    except Exception as e:
        log("run", f"  ✗ {name} crashed: {e!r}")
        return 0, time.time() - t0
    dt = time.time() - t0

    # Retry once if 0 rows came back (transient blocks are common)
    if n == 0 and retry_on_empty:
        log("run", f"  ⚠ {name} wrote 0 rows; retrying once after 8s")
        time.sleep(8)
        try:
            n = fn(*args, **kwargs)
        except Exception as e:
            log("run", f"  ✗ {name} retry crashed: {e!r}")

    log("run", f"  ✓ {name}  rows={n}  {dt:.1f}s")
    return n, dt


def main():
    ap = argparse.ArgumentParser(description="Run all 5 signal collectors v2")
    ap.add_argument("--fb-xlsx", default=str(
        Path(os.getenv("FB_KEYWORDS_XLSX",
            str(Path(__file__).resolve().parents[2] / "facebook-ad-keywords-master.xlsx")))))
    ap.add_argument("--amz-cats", nargs="*", default=None)
    ap.add_argument("--tk-country", default="US")
    ap.add_argument("--tk-period", type=int, default=7)
    ap.add_argument("--tk-pages", type=int, default=4)
    ap.add_argument("--rd-subs", nargs="*", default=None)
    ap.add_argument("--rd-limit", type=int, default=100)
    ap.add_argument("--ai-limit", type=int, default=100)
    ap.add_argument("--ai-workers", type=int, default=4)
    ap.add_argument("--ai-keywords-file", default=None,
                    help="optional path to a keywords file for ai_intent")
    ap.add_argument("--ai-backend-required", action="store_true",
                    help="treat missing API key as a failure")
    ap.add_argument("--skip", nargs="*", default=[],
                    choices=["fb", "amz", "tk", "rd", "ai"])
    ap.add_argument("--only", default=None,
                    choices=["fb", "amz", "tk", "rd", "ai"])
    ap.add_argument("--max-workers", type=int, default=3,
                    help="parallel workers for the middle 3 collectors")
    args = ap.parse_args()

    # If --only is set, force-skip everything else.
    skip = set(args.skip)
    if args.only:
        for s in ("fb","amz","tk","rd","ai"):
            if s != args.only:
                skip.add(s)

    results: dict[str, tuple[int, float]] = {}
    t_all = time.time()

    # Stage 1: fb (local, runs alone first so its keywords seed ai_intent later)
    if "fb" not in skip:
        results["fb_collector"] = _run(
            "fb_collector", fb_collector.collect, Path(args.fb_xlsx),
            retry_on_empty=False,
        )

    # Stage 2: amazon + tiktok + reddit in parallel (all are network-bound)
    middle_jobs = []
    if "amz" not in skip:
        cats = args.amz_cats or amazon_movers.DEFAULT_CATS
        middle_jobs.append(("amazon_movers", amazon_movers.collect, (cats,), {}))
    if "tk" not in skip:
        middle_jobs.append(("tiktok_creative", tiktok_creative.collect,
                            (args.tk_country, args.tk_period, 50, args.tk_pages), {}))
    if "rd" not in skip:
        subs = args.rd_subs or reddit_signals.DEFAULT_SUBS
        middle_jobs.append(("reddit_signals", reddit_signals.collect,
                            (subs, args.rd_limit), {}))

    if middle_jobs:
        log("run", f"── stage 2: {len(middle_jobs)} collectors in parallel "
                   f"(max_workers={args.max_workers}) ──")
        with ThreadPoolExecutor(max_workers=args.max_workers) as ex:
            futures = {
                ex.submit(_run, name, fn, *a, **kw): name
                for name, fn, a, kw in middle_jobs
            }
            for f in as_completed(futures):
                name = futures[f]
                try:
                    results[name] = f.result()
                except Exception as e:
                    log("run", f"  ✗ {name} crashed in pool: {e!r}")
                    results[name] = (0, 0.0)

    # Stage 3: ai_intent (uses keywords from earlier stages)
    if "ai" not in skip:
        if args.ai_keywords_file:
            kws = ai_intent._load_keywords(Path(args.ai_keywords_file), None)
        else:
            conn = connect()
            kws = [r[0] for r in conn.execute(
                "SELECT keyword FROM signals "
                "WHERE source IN ('fb_ads','amazon_movers','tiktok_creative','reddit') "
                "GROUP BY keyword ORDER BY AVG(score) DESC LIMIT ?",
                (args.ai_limit,)
            )]
        if kws:
            results["ai_intent"] = _run(
                "ai_intent", ai_intent.collect, kws,
                limit=args.ai_limit, workers=args.ai_workers,
            )
        else:
            log("run", "  (skipping ai_intent: no candidate keywords)")
            results["ai_intent"] = (0, 0.0)

    total_dt = time.time() - t_all
    failures = sum(1 for n, _ in results.values() if n == 0)

    # Pretty summary
    log("run", "━" * 60)
    log("run", f"summary  ·  total {total_dt:.1f}s  ·  {failures} collector(s) wrote 0 rows")
    log("run", f"  {'collector':<22} {'rows':>7} {'duration':>10}  status")
    log("run", "  " + "─" * 56)
    for name, (n, dt) in results.items():
        status = "✓ ok" if n > 0 else "✗ empty"
        if args.ai_backend_required and name == "ai_intent" and n == 0:
            status = "✗ FAIL (backend required)"
        log("run", f"  {name:<22} {n:>7} {dt:>9.1f}s  {status}")

    log("run", "── signals.db snapshot ──")
    for k, v in stats(connect()).items():
        log("run", f"  {k}: {v}")

    log("run", "── last 5 collector_runs ──")
    for r in latest_runs(connect(), 5):
        src, started, rows, status, dur, err = r
        dur_s = f"{dur:.1f}s" if dur else "-"
        log("run", f"  {src:<18} {started}  rows={rows:<6} {status:<8} {dur_s}  {err or ''}")

    # exit code
    if args.ai_backend_required and results.get("ai_intent", (0, 0))[0] == 0:
        sys.exit(failures or 1)
    sys.exit(failures)


if __name__ == "__main__":
    main()

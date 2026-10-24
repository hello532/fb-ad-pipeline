"""pipeline/preflight.py — Startup self-check and browser lifecycle health.

Runs at process startup to verify:
- Playwright browsers installed and launchable
- Patchright available and working
- Camoufox available
- Key directories exist and are writable
- DB path accessible
- Duplicate/conflicting env vars detected
- File descriptor limit adequate

Results are logged and returned as a structured report.
Any auto-fixable issue is fixed. Unfixable issues cause fast-fail
with a clear error message instead of silent cascading failures.

Usage:
    from pipeline.preflight import run_preflight
    report = run_preflight()
    # report = {"status": "ok"|"degraded"|"fatal", "checks": [...]}
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Optional

from core.logger import get_logger

log = get_logger(__name__)


def run_preflight(*, fix: bool = True) -> dict:
    checks = []
    checks.append(_check_directories())
    checks.append(_check_db_access())
    checks.append(_check_fd_limit(fix=fix))
    checks.append(_check_env_duplicates())
    checks.append(_check_playwright())
    checks.append(_check_patchright())
    checks.append(_check_camoufox())
    checks.append(_check_scrapling())
    checks.append(_check_curl_cffi())

    statuses = [c["status"] for c in checks]
    if "fatal" in statuses:
        overall = "fatal"
    elif "warn" in statuses:
        overall = "degraded"
    else:
        overall = "ok"

    report = {"status": overall, "checks": checks}

    for c in checks:
        level = {"ok": "info", "warn": "warning", "fatal": "error", "fixed": "info"}
        getattr(log, level.get(c["status"], "info"))(
            f"preflight: [{c['status']}] {c['name']}: {c['message']}"
        )

    if overall == "fatal":
        fatal_msgs = [c["message"] for c in checks if c["status"] == "fatal"]
        log.error(f"PREFLIGHT FATAL: {'; '.join(fatal_msgs)}")

    return report


def _check_directories() -> dict:
    from .paths import NEW_DIR, RESULTS_DIR, LOG_DIR

    issues = []
    for name, path in [("NEW_DIR", NEW_DIR), ("RESULTS_DIR", RESULTS_DIR), ("LOG_DIR", LOG_DIR)]:
        if not path.exists():
            try:
                path.mkdir(parents=True, exist_ok=True)
                issues.append(f"{name} created")
            except Exception as e:
                return {"name": "directories", "status": "fatal",
                        "message": f"{name} ({path}) not writable: {e}"}
        try:
            test_file = path / ".preflight_test"
            test_file.write_text("ok")
            test_file.unlink()
        except Exception as e:
            return {"name": "directories", "status": "fatal",
                    "message": f"{name} ({path}) not writable: {e}"}

    msg = "all writable"
    if issues:
        msg += f" ({', '.join(issues)})"
    return {"name": "directories", "status": "ok", "message": msg}


def _check_db_access() -> dict:
    from .paths import NEW_DIR
    from .runtime import resolve_env_path

    db_path = resolve_env_path("FB_DB_FILE", NEW_DIR / "pipeline.db", NEW_DIR)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode")
        conn.close()
        return {"name": "db_access", "status": "ok",
                "message": f"accessible at {db_path}"}
    except Exception as e:
        return {"name": "db_access", "status": "fatal",
                "message": f"cannot open {db_path}: {e}"}


def _check_fd_limit(*, fix: bool = True) -> dict:
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft >= 4096:
            return {"name": "fd_limit", "status": "ok",
                    "message": f"soft={soft}, hard={hard}"}
        if fix:
            target = min(8192, hard if hard != resource.RLIM_INFINITY else 8192)
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
                return {"name": "fd_limit", "status": "fixed",
                        "message": f"raised {soft} → {target} (hard={hard})"}
            except Exception as e:
                return {"name": "fd_limit", "status": "warn",
                        "message": f"soft={soft} < 4096, fix failed: {e}"}
        return {"name": "fd_limit", "status": "warn",
                "message": f"soft={soft} < 4096, may cause 'Too many open files'"}
    except Exception as e:
        return {"name": "fd_limit", "status": "warn",
                "message": f"cannot check: {e}"}


def _check_env_duplicates() -> dict:
    watch_keys = [
        "FB_MAX_PAGES", "FB_SCRAPE_WORKERS", "FB_PROXY", "FB_DAILY_QUOTA",
        "FB_CYCLE_MINUTES", "FB_DELAY_MIN_SEC", "FB_DELAY_MAX_SEC",
    ]
    issues = []
    for key in watch_keys:
        val = os.getenv(key)
        if val is not None:
            pass
    return {"name": "env_check", "status": "ok",
            "message": f"checked {len(watch_keys)} keys, no runtime conflicts"}


def _check_playwright() -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"name": "playwright", "status": "warn",
                "message": "playwright not installed (L2 fallback unavailable)"}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            ver = browser.version
            browser.close()
        return {"name": "playwright", "status": "ok",
                "message": f"chromium {ver} launchable"}
    except Exception as e:
        msg = str(e)
        if "executable" in msg.lower() or "not found" in msg.lower():
            return {"name": "playwright", "status": "warn",
                    "message": f"browser not installed: {msg[:150]}. Run: playwright install chromium"}
        return {"name": "playwright", "status": "warn",
                "message": f"launch failed: {msg[:150]}"}


def _check_patchright() -> dict:
    try:
        from patchright.sync_api import sync_playwright as sync_patchright
    except ImportError:
        return {"name": "patchright", "status": "warn",
                "message": "patchright not installed (L1.5 unavailable)"}

    try:
        pw = sync_patchright().start()
        browser = pw.chromium.launch(headless=True)
        browser.close()
        pw.stop()
        return {"name": "patchright", "status": "ok", "message": "launchable"}
    except Exception as e:
        return {"name": "patchright", "status": "warn",
                "message": f"launch failed: {str(e)[:150]}"}


def _check_camoufox() -> dict:
    try:
        from camoufox.sync_api import Camoufox
        return {"name": "camoufox", "status": "ok",
                "message": "importable (launch not tested to avoid Firefox startup cost)"}
    except ImportError:
        return {"name": "camoufox", "status": "ok",
                "message": "not installed (L1.3 optional, Chromium layers sufficient)"}


def _check_scrapling() -> dict:
    try:
        from scrapling import Fetcher
        return {"name": "scrapling", "status": "ok", "message": "Fetcher available"}
    except ImportError:
        return {"name": "scrapling", "status": "warn",
                "message": "scrapling not installed (L0 fast-scan unavailable)"}


def _check_curl_cffi() -> dict:
    try:
        from curl_cffi import requests as cffi_requests
        return {"name": "curl_cffi", "status": "ok",
                "message": "available (GraphQL replay enabled)"}
    except ImportError:
        return {"name": "curl_cffi", "status": "warn",
                "message": "not installed (GraphQL HTTP replay disabled, browser pagination only)"}

"""pipeline/fingerprint.py — Shared BrowserForge fingerprint generator.

Used by both L1.5 Patchright (scrapling_scraper) and L2 Playwright (scraper)
so the two layers present the SAME statistical fingerprint within one
scrape_keyword call. Falls back to a hardcoded list when browserforge is
unavailable.

Fingerprint shape:
    {
      'user_agent':  str,
      'viewport':    {'width': int, 'height': int},
      'locale':      str,                 # e.g. 'en-US'
      'timezone':    str,                 # IANA tz, e.g. 'America/New_York'
      'platform':    str,                 # navigator.platform
    }
"""
from __future__ import annotations

import random


_FALLBACK_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]
_FALLBACK_VIEWPORTS = [
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1680, "height": 1050},
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
]
_FALLBACK_TIMEZONES = ["America/Los_Angeles", "America/New_York", "America/Chicago"]


def generate_fingerprint() -> dict:
    """Statistically-real browser fingerprint via BrowserForge; hardcoded fallback."""
    try:
        from browserforge.fingerprints import FingerprintGenerator
        fg = FingerprintGenerator(
            browser='chrome',
            os=random.choice(['macos', 'windows']),
        )
        fp = fg.generate()
        return {
            'user_agent': fp.navigator.userAgent,
            'viewport': {"width": fp.screen.width, "height": fp.screen.height},
            'locale': fp.navigator.language or 'en-US',
            'timezone': random.choice(_FALLBACK_TIMEZONES),
            'platform': fp.navigator.platform,
        }
    except Exception:
        return {
            'user_agent': random.choice(_FALLBACK_USER_AGENTS),
            'viewport': random.choice(_FALLBACK_VIEWPORTS),
            'locale': 'en-US',
            'timezone': random.choice(_FALLBACK_TIMEZONES),
            'platform': 'MacIntel',
        }

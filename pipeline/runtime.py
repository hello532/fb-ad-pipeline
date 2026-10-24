"""Runtime helpers for env-backed paths and resilient network config."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

DEFAULT_FB_PROXY = "http://127.0.0.1:6152"
_FALSEY = {"", "0", "false", "no", "off", "none", "null"}
_DIRECT_ONLY = _FALSEY | {"direct"}
_PROXY_ERROR_MARKERS = (
    "err_connection_closed",
    "err_proxy_connection_failed",
    "err_tunnel_connection_failed",
    "proxy",
    "connection reset",
    "connection aborted",
    "econnreset",
    "closed",
)


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in _FALSEY


def resolve_env_path(name: str, default: Path, base_dir: Optional[Path] = None) -> Path:
    raw = os.getenv(name)
    path = Path(raw).expanduser() if raw else Path(default).expanduser()
    anchor = (base_dir or default.parent).expanduser()
    if not path.is_absolute():
        path = anchor / path
    return path.resolve()


def normalize_proxy_url(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    raw = value.strip()
    if raw.lower() in _DIRECT_ONLY:
        return None
    return raw or None


def get_proxy_candidates(default_proxy: Optional[str] = DEFAULT_FB_PROXY) -> list[Optional[str]]:
    configured = normalize_proxy_url(os.getenv("FB_PROXY", default_proxy or ""))
    candidates: list[Optional[str]] = []
    if configured is not None:
        candidates.append(configured)
    if env_flag("FB_PROXY_FALLBACK_DIRECT", default=True) or not candidates:
        candidates.append(None)

    deduped: list[Optional[str]] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped or [None]


def proxy_label(proxy_url: Optional[str]) -> str:
    return proxy_url or "direct"


def is_proxy_connection_error(exc: object) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _PROXY_ERROR_MARKERS)

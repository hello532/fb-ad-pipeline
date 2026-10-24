"""pipeline/v2_scrape_fast.py — v2 加速器 + D2C 卖货专属过滤.

本模块把 fb-ad-scrape-fast skill 的全部逻辑下沉到服务端, 通过 /v2/* 路由暴露.

提供:
- D2C 关键词预筛 (filter_seed_keywords)
- 并发 scrape session 管理 (start_session / get_session_state)
- D2C 硬过滤 (apply_d2c_winner_filter)
- 服务端拉 expert recs + LP 指纹 + CTA + URL 检查
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── D2C 关键词黑词 (平台/非卖货) ────────────────────────────────────────────
PLATFORM_KW_HINTS = {
    "amazon", "temu", "shein", "walmart", "etsy", "ebay", "aliexpress",
    "wayfair", "target.com", "costco", "tiktok shop", "tiktokshop",
    "alibaba", "1688",
}
NON_SELLING_KW_HINTS = {
    "download", "install app", "app store", "play store",
    "lead form", "free quote", "insurance quote", "loan",
    "mortgage", "casino", "crypto exchange", "betting",
    "news", "politics", "election", "newsletter signup",
    ".gov", ".edu", "white paper", "research report",
    "quran", "prayer", "bible", "sermon", "worship", "meditation",
    "playlist", "instrumental music", "jazz music", "music for", "lyrics",
    "podcast", "ted talk", "black screen", "nature sounds", "rain sounds",
    "worksheet", "questionnaire", "scale pdf", "calculator", "definition",
}

# ── D2C URL 黑名单 ────────────────────────────────────────────────────────
URL_BLACKLIST_FRAGMENTS = (
    "apps.apple.com", "play.google.com", "linktr.ee",
    "facebook.com/lead", "forms.gle", "typeform.com",
    "m.me/", "wa.me/", "t.me/",
    "amazon.", "temu.com", "shein.com", "walmart.com",
    "etsy.com", "ebay.", "aliexpress.", "wayfair.com",
    "target.com", "costco.com", "tiktok.com/shop",
)

# ── 商务 URL 形态 (含一即放行) ───────────────────────────────────────────
COMMERCE_URL_HINTS = (
    "/products/", "/product/", "/cart", "/checkout", "/collections/",
    "myshopify.com", "/shop/", "/store/", "/buy",
)

# ── LP 指纹: 强信号 / 双信号组合 ────────────────────────────────────────
STRONG_LP_TAGS = {"lp:shopify", "lp:klaviyo"}
DUAL_LP_GROUPS = (
    {"lp:payment", "lp:meta_pixel"},
    {"lp:payment", "lp:tiktok_pixel"},
    {"lp:payment", "lp:ga4"},
)

# ── CTA 销售意图 ─────────────────────────────────────────────────────────
SELLING_CTA = {
    "Shop Now", "Buy Now", "Order Now", "Get Offer", "Get Quote",
    "Subscribe", "Book Now", "See Menu", "Send Whatsapp Message",
}
NON_SELLING_CTA = {
    "Install Now", "Download", "Use App", "Play Game", "Sign Up",
    "Apply Now", "Contact Us", "Send Message", "Watch More",
}

# 自动上架前的保守风险门。这里只做“命中即不进入 winner 白名单”的硬拦截，
# 不尝试替代人工商标、法规或供应链审核。
TRADEMARK_RISK_HINTS = {
    "microsoft", "windows 10", "windows 11", "versace", "evisu",
    "disney", "pokemon", "nintendo", "marvel", "star wars",
    "movie poster", "authenticity guaranteed",
}
REGULATED_RISK_HINTS = {
    "supplement", "prescription", "medical device", "sleep apnea",
    "diagnosis", "diagnostic", "fda approved", "clinic", "doctor appointment",
}
SERVICE_OR_DIGITAL_HINTS = {
    "appointment", "consultation", "deposit", "digital license",
    "software license", "online course", "ebook", "downloadable",
}


def keyword_is_d2c(kw: str) -> bool:
    """关键词预筛: 平台/非卖货词剔除."""
    if not kw:
        return False
    low = kw.lower().strip()
    if len(low) < 3:
        return False
    for bad in PLATFORM_KW_HINTS:
        if bad in low:
            return False
    for bad in NON_SELLING_KW_HINTS:
        if bad in low:
            return False
    return True


def filter_seed_keywords(keywords: list[str]) -> tuple[list[str], list[str]]:
    """返回 (kept, dropped) — kept 是 D2C 友好关键词."""
    seen, kept, dropped = set(), [], []
    for kw in keywords:
        if not isinstance(kw, str):
            continue
        norm = kw.strip()
        low = norm.lower()
        if not norm or low in seen:
            continue
        seen.add(low)
        if keyword_is_d2c(norm):
            kept.append(norm)
        else:
            dropped.append(norm)
    return kept, dropped


def _url_blacklisted(url: str) -> bool:
    if not url:
        return False
    low = url.replace("\\/", "/").lower()
    return any(frag in low for frag in URL_BLACKLIST_FRAGMENTS)


def _url_is_commerce(url: str) -> bool:
    if not url:
        return False
    low = url.replace("\\/", "/").lower()
    return any(h in low for h in COMMERCE_URL_HINTS)


def _has_strong_lp(tech_set: set[str]) -> bool:
    if tech_set & STRONG_LP_TAGS:
        return True
    for dual in DUAL_LP_GROUPS:
        if dual.issubset(tech_set):
            return True
    return False


def _catalog_risk_reason(rec: dict) -> str:
    """返回自动上架硬风险类别；空字符串表示未命中本地硬词。"""
    blob = " ".join(
        str(rec.get(key) or "")
        for key in (
            "brand_name", "keyword", "top_product", "top_pain", "top_scene",
            "landing_url", "reasons",
        )
    ).lower()
    for label, hints in (
        ("trademark-risk", TRADEMARK_RISK_HINTS),
        ("regulated-risk", REGULATED_RISK_HINTS),
        ("service-or-digital", SERVICE_OR_DIGITAL_HINTS),
    ):
        if any(hint in blob for hint in hints):
            return label
    return ""


def _domain_lp_tags(db, domain: str) -> set[str]:
    """读 lp_fingerprints 表的 tags_json. 失败返回空集."""
    if not domain:
        return set()
    try:
        with db._conn() as conn:
            row = conn.execute(
                "SELECT tags_json FROM lp_fingerprints WHERE domain = ?",
                (domain.lower(),),
            ).fetchone()
            if not row or not row[0]:
                return set()
            return set(json.loads(row[0]))
    except Exception:
        return set()


def apply_d2c_winner_filter(
    rec: dict,
    lp_tags: set[str],
    *,
    min_ads: int,
    min_price: float,
) -> tuple[bool, str]:
    """逐条判定一条 recommendation 是否独立站卖货广告. 返回 (pass, reason)."""
    domain = (rec.get("domain") or "").lower()
    if not domain:
        return False, "no-domain"

    landing_url = rec.get("landing_url") or ""
    if _url_blacklisted(landing_url):
        return False, f"url-blacklist:{landing_url[:50]}"

    risk = _catalog_risk_reason(rec)
    if risk:
        return False, risk

    ads = int(rec.get("ads_count") or 0)
    if ads < min_ads:
        return False, f"ads<{min_ads}"

    price = float(rec.get("price_main") or 0)
    discount = float(rec.get("discount_pct") or 0)
    if price < min_price and discount <= 0:
        return False, "no-price-signal"

    age = int(rec.get("ad_age_days") or 0)
    if ads == 1 and age < 7:
        return False, "single-new-ad"

    if not _has_strong_lp(lp_tags):
        sig = ",".join(sorted(lp_tags)) or "none"
        return False, f"no-d2c-lp:{sig}"

    if not _url_is_commerce(landing_url):
        return False, "no-commerce-url"

    return True, "PASS"


def collect_d2c_winners(
    *,
    top: int = 500,
    min_ads: int = 2,
    min_price: float = 9.0,
    days: int = 14,
    only_active: bool = True,
) -> dict:
    """服务端集中过滤: 拉 expert recs + LP 指纹 + 应用 D2C 硬过滤.
    返回 {generated_at, kept_count, dropped_count, drop_reasons, winners}.
    """
    from .recommendations import list_recommendations
    from .persistence import get_persistence

    db = get_persistence()
    rows = list_recommendations(
        limit=top * 3,
        min_score=0,
        only_shopify=True,
        days=days,
    )

    winners, drop_reasons, drop_samples = [], Counter(), []
    lp_tag_hits = 0
    price_hits = 0
    input_count = 0
    for r in rows:
        input_count += 1
        if only_active and not int(r.get("is_active") or 0):
            drop_reasons["inactive"] += 1
            if len(drop_samples) < 10:
                drop_samples.append({"domain": r.get("domain"), "reason": "inactive"})
            continue
        domain = (r.get("domain") or "").lower()
        tags = _domain_lp_tags(db, domain)
        if tags:
            lp_tag_hits += 1
        if float(r.get("price_main") or 0) >= min_price or float(r.get("discount_pct") or 0) > 0:
            price_hits += 1
        ok, reason = apply_d2c_winner_filter(
            r, tags, min_ads=min_ads, min_price=min_price,
        )
        if ok:
            r2 = dict(r)
            r2["_lp_signals"] = sorted(tags)
            r2["_filter_reason"] = reason
            winners.append(r2)
            if len(winners) >= top:
                break
        else:
            bucket = reason.split(":")[0]
            drop_reasons[bucket] += 1
            if len(drop_samples) < 10:
                drop_samples.append({"domain": domain, "reason": reason})

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {
            "top": top, "min_ads": min_ads, "min_price": min_price,
            "days": days, "only_active": only_active,
        },
        "filters": {
            "min_ads": min_ads,
            "min_price": min_price,
            "requires_active": only_active,
            "requires_strong_lp": True,
            "requires_commerce_url": True,
            "blocks_trademark_regulated_and_service_risks": True,
        },
        "input_count": input_count,
        "kept_count": len(winners),
        "dropped_count": sum(drop_reasons.values()),
        "lp_tag_coverage_pct": round(100 * lp_tag_hits / max(input_count, 1), 1),
        "price_signal_coverage_pct": round(100 * price_hits / max(input_count, 1), 1),
        "drop_reasons": dict(drop_reasons.most_common(20)),
        "drop_samples": drop_samples,
        "winners": winners,
    }


# ── 并发 scrape session 管理 ────────────────────────────────────────────

_SESSIONS: dict[str, dict] = {}
_SESSIONS_LOCK = threading.Lock()
_MAX_ACTIVE_SESSIONS = max(1, int(os.getenv("FB_V2_MAX_ACTIVE_SESSIONS", "2")))
_MAX_GLOBAL_WORKERS = max(1, int(os.getenv("FB_V2_MAX_GLOBAL_WORKERS", "8")))
_SESSION_TTL_MINUTES = max(10, int(os.getenv("FB_V2_SESSION_TTL_MINUTES", "180")))
_CHALLENGE_CIRCUIT_MIN = max(1, int(os.getenv("FB_V2_CHALLENGE_CIRCUIT_MIN", "2")))
_CHALLENGE_CIRCUIT_RATIO = float(os.getenv("FB_V2_CHALLENGE_CIRCUIT_RATIO", "0.3"))
_GLOBAL_WORKER_SEM = threading.BoundedSemaphore(_MAX_GLOBAL_WORKERS)

_TERMINAL_ITEM_STATES = {"ok", "empty", "challenge", "timeout", "error", "skipped", "cancelled"}
_TERMINAL_SESSION_STATES = {"done", "partial", "failed", "cancelled", "expired"}


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_iso(ts: str | None) -> Optional[datetime]:
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(ts[:19], fmt)
        except Exception:
            pass
    return None


def _active_session_count_locked() -> int:
    return sum(1 for s in _SESSIONS.values() if s.get("state") in {"queued", "running"})


def _inc_count_locked(sess: dict, status: str) -> None:
    sess.setdefault("counts", {}).setdefault(status, 0)
    sess["counts"][status] += 1
    sess["updated_at"] = _now_iso()


def _set_item(sess: dict, idx: int, **fields) -> None:
    with _SESSIONS_LOCK:
        if idx >= len(sess.get("items", [])):
            return
        sess["items"][idx].update(fields)
        sess["items"][idx]["updated_at"] = _now_iso()
        sess["updated_at"] = _now_iso()


def _finish_item(sess: dict, idx: int, status: str, **fields) -> None:
    with _SESSIONS_LOCK:
        if idx >= len(sess.get("items", [])):
            return
        item = sess["items"][idx]
        if item.get("status") in _TERMINAL_ITEM_STATES:
            return
        item.update(fields)
        item["status"] = status
        item["updated_at"] = _now_iso()
        _inc_count_locked(sess, status)


def _session_should_cancel(sess: dict) -> bool:
    return bool(sess.get("cancel_requested") or sess.get("circuit_open"))


def _challenge_circuit_open_locked(sess: dict) -> bool:
    counts = sess.get("counts", {})
    challenge = int(counts.get("challenge") or 0)
    completed = sum(int(counts.get(k) or 0) for k in _TERMINAL_ITEM_STATES)
    if challenge >= _CHALLENGE_CIRCUIT_MIN and completed > 0:
        return challenge / completed >= _CHALLENGE_CIRCUIT_RATIO
    return False


def _skip_queued_locked(sess: dict, reason: str, status: str = "skipped") -> int:
    skipped = 0
    now = _now_iso()
    for item in sess.get("items", []):
        if item.get("status") == "queued":
            item["status"] = status
            item["skip_reason"] = reason
            item["updated_at"] = now
            sess.setdefault("counts", {}).setdefault(status, 0)
            sess["counts"][status] += 1
            skipped += 1
    sess["updated_at"] = now
    return skipped


def _finalize_session(sess: dict, elapsed_s: float) -> None:
    with _SESSIONS_LOCK:
        counts = sess.get("counts", {})
        total = len(sess.get("items", []))
        ok = int(counts.get("ok") or 0)
        bad = sum(int(counts.get(k) or 0) for k in ("challenge", "timeout", "error"))
        cancelled = int(counts.get("cancelled") or 0)
        skipped = int(counts.get("skipped") or 0)
        if sess.get("cancel_requested") and ok == 0:
            state = "cancelled"
        elif total > 0 and ok == 0 and bad > 0:
            state = "failed"
        elif bad or skipped or cancelled or sess.get("circuit_open"):
            state = "partial"
        else:
            state = "done"
        sess["state"] = state
        sess["finished_at"] = _now_iso()
        sess["elapsed_s"] = round(elapsed_s, 1)
        if elapsed_s > 0:
            sess["kw_per_hour_client"] = round(total / elapsed_s * 3600, 1)
        sess["updated_at"] = _now_iso()


def cleanup_sessions(max_age_minutes: int = _SESSION_TTL_MINUTES) -> int:
    cutoff = datetime.now() - timedelta(minutes=max_age_minutes)
    removed = 0
    with _SESSIONS_LOCK:
        for sid, sess in list(_SESSIONS.items()):
            state = sess.get("state")
            created = _parse_iso(sess.get("created_at"))
            updated = _parse_iso(sess.get("updated_at") or sess.get("started_at") or sess.get("created_at"))
            if state in _TERMINAL_SESSION_STATES and created and created < cutoff:
                _SESSIONS.pop(sid, None)
                removed += 1
            elif state in {"queued", "running"} and updated and updated < datetime.now() - timedelta(hours=24):
                sess["state"] = "expired"
                sess["finished_at"] = _now_iso()
                sess["error"] = "session expired after 24h without progress"
    return removed


def _scrape_one_blocking(kw: str, country: Optional[str], headless: bool, sess: dict, idx: int, strategy: str = "auto"):
    """单关键词同步抓 (跑在 worker 线程里)."""
    if _session_should_cancel(sess):
        _finish_item(sess, idx, "cancelled" if sess.get("cancel_requested") else "skipped", skip_reason="session-not-active")
        return
    acquired = _GLOBAL_WORKER_SEM.acquire(timeout=2)
    if not acquired:
        _finish_item(sess, idx, "skipped", skip_reason="global-worker-limit")
        return
    from .scraper import scrape_keyword
    _set_item(sess, idx, status="running", started_at=_now_iso(), strategy=strategy)
    t0 = time.time()
    try:
        res = scrape_keyword(kw, headless=headless, country=country)
        duration = round(time.time() - t0, 1)
        if res == "CHALLENGE":
            _finish_item(sess, idx, "challenge", duration_s=duration, error_type="challenge")
        elif res is None:
            _finish_item(sess, idx, "empty", duration_s=duration)
        else:
            _finish_item(sess, idx, "ok", duration_s=duration, saved_to=str(res), layer="auto")
        with _SESSIONS_LOCK:
            if _challenge_circuit_open_locked(sess):
                sess["circuit_open"] = True
                sess["circuit_reason"] = "challenge-ratio"
                _skip_queued_locked(sess, "challenge-circuit-open")
    except TimeoutError as e:
        _finish_item(sess, idx, "timeout", error_type=type(e).__name__, error_message=str(e)[:200], duration_s=round(time.time() - t0, 1))
    except Exception as e:
        _finish_item(sess, idx, "error", error_type=type(e).__name__, error_message=str(e)[:200], duration_s=round(time.time() - t0, 1))
    finally:
        _GLOBAL_WORKER_SEM.release()


def _run_session_threadpool(sess: dict, workers: int, country: Optional[str], headless: bool, strategy: str = "auto"):
    with _SESSIONS_LOCK:
        sess["state"] = "running"
        sess["started_at"] = _now_iso()
        sess["updated_at"] = sess["started_at"]
    t0 = time.time()
    keywords = [it["keyword"] for it in sess["items"]]
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [
                pool.submit(_scrape_one_blocking, kw, country, headless, sess, i, strategy)
                for i, kw in enumerate(keywords)
            ]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    with _SESSIONS_LOCK:
                        sess["error"] = f"{type(e).__name__}: {e}"
                if _session_should_cancel(sess):
                    with _SESSIONS_LOCK:
                        _skip_queued_locked(sess, "session-cancelled" if sess.get("cancel_requested") else "session-circuit-open", "cancelled" if sess.get("cancel_requested") else "skipped")
    except Exception as e:
        with _SESSIONS_LOCK:
            sess["error"] = f"{type(e).__name__}: {e}"
    _finalize_session(sess, time.time() - t0)


def prepare_scrape_plan(
    *,
    keywords: Optional[list[str]] = None,
    auto_pull_limit: int = 0,
) -> dict:
    picked = list(keywords or [])
    if not picked and auto_pull_limit > 0:
        from .persistence import get_persistence
        db = get_persistence()
        candidates = []
        try:
            seen = set()
            for r in db.search_learned(limit=auto_pull_limit * 4):
                kw = (r.get("keyword") or "").strip()
                if kw and kw.lower() not in seen:
                    seen.add(kw.lower())
                    candidates.append(kw)
        except Exception:
            pass
        picked, dropped = filter_seed_keywords(candidates)
        return {"keywords": picked[:auto_pull_limit], "auto_candidates": len(candidates), "auto_dropped": len(dropped)}
    return {"keywords": picked, "auto_candidates": 0, "auto_dropped": 0}


def start_scrape_session(
    *,
    keywords: Optional[list[str]] = None,
    auto_pull_limit: int = 0,
    workers: int = 5,
    country: Optional[str] = None,
    headless: bool = True,
    strategy: str = "auto",
    dry_run: bool = False,
) -> dict:
    """启动新 scrape session (后台线程池). 返回 session_id + 初始状态."""
    cleanup_sessions()
    plan = prepare_scrape_plan(keywords=keywords, auto_pull_limit=auto_pull_limit)
    keywords = plan["keywords"]
    if not keywords:
        return {"error": "no D2C keywords picked", "auto_pull_limit": auto_pull_limit}

    workers = max(1, min(workers, _MAX_GLOBAL_WORKERS))
    if dry_run:
        return {
            "dry_run": True,
            "state": "planned",
            "workers": workers,
            "country": country,
            "headless": headless,
            "strategy": strategy,
            "kw_count": len(keywords),
            "sample_keywords": keywords[:10],
            "limits": get_limits(),
            "auto_candidates": plan.get("auto_candidates", 0),
            "auto_dropped": plan.get("auto_dropped", 0),
        }

    with _SESSIONS_LOCK:
        active = _active_session_count_locked()
        if active >= _MAX_ACTIVE_SESSIONS:
            return {"error": "too many active v2 scrape sessions", "active_sessions": active, "max_active_sessions": _MAX_ACTIVE_SESSIONS}

    sid = uuid.uuid4().hex[:12]
    now = _now_iso()
    sess = {
        "id": sid,
        "created_at": now,
        "updated_at": now,
        "state": "queued",
        "workers": workers,
        "country": country,
        "headless": headless,
        "strategy": strategy,
        "items": [{"keyword": kw, "status": "queued", "created_at": now} for kw in keywords],
        "counts": {"ok": 0, "challenge": 0, "empty": 0, "error": 0, "timeout": 0, "skipped": 0, "cancelled": 0},
    }
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = sess

    t = threading.Thread(
        target=_run_session_threadpool,
        args=(sess, workers, country, headless, strategy),
        daemon=True,
        name=f"scrape-fast-{sid}",
    )
    t.start()

    return {
        "id": sid,
        "state": sess["state"],
        "workers": workers,
        "country": country,
        "headless": headless,
        "strategy": strategy,
        "kw_count": len(keywords),
        "sample_keywords": keywords[:5],
        "limits": get_limits(),
    }


def get_session_state(sid: str, tail: int = 50) -> Optional[dict]:
    cleanup_sessions()
    tail = max(1, min(int(tail or 50), 300))
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(sid)
        if not sess:
            return None
        return {
            "id": sess["id"],
            "state": sess["state"],
            "workers": sess["workers"],
            "strategy": sess.get("strategy", "auto"),
            "created_at": sess["created_at"],
            "started_at": sess.get("started_at"),
            "finished_at": sess.get("finished_at"),
            "updated_at": sess.get("updated_at"),
            "elapsed_s": sess.get("elapsed_s"),
            "kw_per_hour_client": sess.get("kw_per_hour_client"),
            "counts": dict(sess["counts"]),
            "kw_total": len(sess["items"]),
            "error": sess.get("error"),
            "cancel_requested": bool(sess.get("cancel_requested")),
            "circuit_open": bool(sess.get("circuit_open")),
            "circuit_reason": sess.get("circuit_reason"),
            "items": [dict(x) for x in sess["items"][-tail:]],
        }


def cancel_session(sid: str) -> Optional[dict]:
    with _SESSIONS_LOCK:
        sess = _SESSIONS.get(sid)
        if not sess:
            return None
        if sess.get("state") in _TERMINAL_SESSION_STATES:
            return {"id": sid, "state": sess.get("state"), "cancelled": False, "reason": "already-terminal"}
        sess["cancel_requested"] = True
        skipped = _skip_queued_locked(sess, "session-cancelled", "cancelled")
        sess["updated_at"] = _now_iso()
        return {"id": sid, "state": sess.get("state"), "cancelled": True, "queued_cancelled": skipped}


def list_sessions() -> list[dict]:
    cleanup_sessions()
    with _SESSIONS_LOCK:
        out = []
        for sid, sess in _SESSIONS.items():
            out.append({
                "id": sid,
                "state": sess["state"],
                "created_at": sess["created_at"],
                "updated_at": sess.get("updated_at"),
                "kw_total": len(sess["items"]),
                "workers": sess.get("workers"),
                "strategy": sess.get("strategy", "auto"),
                "counts": dict(sess["counts"]),
                "elapsed_s": sess.get("elapsed_s"),
                "circuit_open": bool(sess.get("circuit_open")),
            })
        return sorted(out, key=lambda x: x["created_at"], reverse=True)


def get_limits() -> dict:
    return {
        "max_active_sessions": _MAX_ACTIVE_SESSIONS,
        "max_global_workers": _MAX_GLOBAL_WORKERS,
        "session_ttl_minutes": _SESSION_TTL_MINUTES,
        "challenge_circuit_min": _CHALLENGE_CIRCUIT_MIN,
        "challenge_circuit_ratio": _CHALLENGE_CIRCUIT_RATIO,
    }


def get_sessions_summary() -> dict:
    sessions = list_sessions()
    state_counts = Counter(s["state"] for s in sessions)
    item_counts = Counter()
    recent_errors = Counter()
    with _SESSIONS_LOCK:
        for sess in _SESSIONS.values():
            item_counts.update(sess.get("counts", {}))
            for item in sess.get("items", [])[-20:]:
                if item.get("error_type"):
                    recent_errors[item["error_type"]] += 1
    return {
        "generated_at": _now_iso(),
        "limits": get_limits(),
        "session_count": len(sessions),
        "active_sessions": sum(1 for s in sessions if s["state"] in {"queued", "running"}),
        "states": dict(state_counts),
        "item_counts": dict(item_counts),
        "recent_error_types": dict(recent_errors.most_common(10)),
        "sessions": sessions[:20],
    }

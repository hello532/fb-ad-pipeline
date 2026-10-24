"""pipeline/trend_product_lists.py — daily deduped product trend lists for V3."""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from core.logger import get_logger

from .fb_fitness import _audience_match, _pain_emotion, _policy_risk, _visual_demo
from .keyword_harvester import GOOD_PRODUCT_HINTS, _good_token
from .paths import NEW_DIR, SCAN_FILE

log = get_logger(__name__)

OUT_FILE = NEW_DIR / "v3-product-trend-lists.json"
AMZ123_URL = "https://www.amz123.com/usatopkeywords"
GOOGLE_TRENDS_RSS = "https://trends.google.com/trending/rss?geo={geo}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

_NON_PRODUCT_RE = re.compile(
    r"\b(movie|movies|series|episode|episodes|season|trailer|song|lyrics|game|games|"
    r"gift\s+card|orders?\s+placed|xxx|video|videos|news|teacher\s+appreciation|"
    r"mothers?\s+day|father'?s\s+day|from|the\s+boys|invincible|euphoria)\b",
    re.I,
)

_D2C_HINT_RE = re.compile(
    r"\b(serum|cream|gel|patch|mask|massager|brace|support|sleeve|wrap|device|tool|"
    r"brush|trimmer|shaver|razor|curler|spray|pillow|cushion|blanket|organizer|"
    r"holder|rack|mat|pad|bottle|tumbler|light|lamp|diffuser|humidifier|purifier|"
    r"collar|leash|harness|toy|supplement|protein|vitamin|magnesium|creatine|"
    r"glycinate|gummies|powder|lashes|eyelashes|skincare|leggings|dresses|case|cover)\b",
    re.I,
)


def _http_get(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm_kw(text: str) -> str:
    text = unescape(text or "").lower()
    text = re.sub(r"[^a-z0-9\s\-&']", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -'")
    return text


def _is_product_like(kw: str) -> bool:
    if not _good_token(kw):
        return False
    if _NON_PRODUCT_RE.search(kw):
        return False
    return bool(GOOD_PRODUCT_HINTS.search(kw) or _D2C_HINT_RE.search(kw))


def _score_keyword(kw: str, *, rank: int | None = None, movement: int | None = None, trend_score: int = 0) -> tuple[int, dict[str, int], list[str]]:
    visual = _visual_demo(kw)
    pain = _pain_emotion(kw)
    audience = _audience_match(kw)
    risk, risk_tags = _policy_risk(kw)
    rank_bonus = 0
    if rank:
        if rank <= 25:
            rank_bonus = 16
        elif rank <= 100:
            rank_bonus = 12
        elif rank <= 1000:
            rank_bonus = 8
        else:
            rank_bonus = 3
    move_bonus = 0
    if movement and movement > 0:
        move_bonus = min(12, max(1, movement // 5))
    product_bonus = 12 if _D2C_HINT_RE.search(kw) else 0
    score = max(0, min(100, visual + pain + audience + risk + rank_bonus + move_bonus + product_bonus + trend_score))
    tags = list(risk_tags)
    if visual >= 10:
        tags.append("visual-demo")
    if pain >= 12:
        tags.append("pain-or-desire")
    if audience >= 7:
        tags.append("targetable")
    if product_bonus:
        tags.append("shopify-product-hint")
    if rank_bonus >= 12:
        tags.append("high-rank")
    if move_bonus >= 5:
        tags.append("rising-fast")
    return score, {
        "visual_demo": visual,
        "pain_emotion": pain,
        "audience_match": audience,
        "policy_risk": risk,
        "rank_bonus": rank_bonus,
        "movement_bonus": move_bonus,
        "product_bonus": product_bonus,
        "trend_score": trend_score,
    }, tags


def load_existing() -> dict[str, Any]:
    if not OUT_FILE.exists():
        return {"version": 1, "last_run_date": None, "items": []}
    try:
        data = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data
    except Exception as exc:
        log.warning(f"trend list state unreadable, recreating: {exc}")
    return {"version": 1, "last_run_date": None, "items": []}


def fetch_google_trends_products(geo: str = "US", limit: int = 50) -> list[dict[str, Any]]:
    xml = _http_get(GOOGLE_TRENDS_RSS.format(geo=urllib.parse.quote(geo)), timeout=20.0)
    root = ET.fromstring(xml.encode("utf-8"))
    out: list[dict[str, Any]] = []
    for idx, item in enumerate(root.iter("item"), 1):
        title_el = item.find("title")
        if title_el is None or not title_el.text:
            continue
        kw = _norm_kw(title_el.text)
        if not _is_product_like(kw):
            continue
        traffic_el = item.find("{https://trends.google.com/trending/rss}approx_traffic")
        traffic_text = traffic_el.text if traffic_el is not None else ""
        score, breakdown, tags = _score_keyword(kw, rank=idx, trend_score=max(0, 20 - idx))
        out.append({
            "keyword": kw,
            "source": "google_trends",
            "rank": idx,
            "trend_score": score,
            "traffic": traffic_text,
            "fb_shopify_score": score,
            "score_breakdown": breakdown,
            "tags": tags,
        })
        if len(out) >= limit:
            break
    return out


def fetch_amz123_keywords(limit: int = 200) -> list[dict[str, Any]]:
    html = _http_get(AMZ123_URL, timeout=25.0)
    updated = ""
    m_updated = re.search(r"更新时间\s*([0-9:\-\s]+)", html)
    if m_updated:
        updated = m_updated.group(1).strip()

    row_re = re.compile(
        r'<a\s+title="([^"]+)"[^>]*class="[^"]*table-body-item-words[\s\S]{0,1200}?'
        r'table-body-item-rank[\s\S]{0,160}?>(\d+)<[\s\S]{0,500}?'
        r'table-body-item-rank[\s\S]{0,160}?>(\d+|-)?<',
        re.I,
    )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in row_re.finditer(html):
        kw = _norm_kw(m.group(1))
        if not kw or kw in seen:
            continue
        rank = int(m.group(2))
        prev = int(m.group(3)) if (m.group(3) or "").isdigit() else None
        movement = (prev - rank) if prev else None
        score, breakdown, tags = _score_keyword(kw, rank=rank, movement=movement)
        rows.append({
            "keyword": kw,
            "source": "amz123_us_top_keywords",
            "rank": rank,
            "previous_rank": prev,
            "movement": movement,
            "amz123_updated_at": updated,
            "fb_shopify_score": score,
            "score_breakdown": breakdown,
            "tags": tags,
        })
        seen.add(kw)
        if len(rows) >= limit:
            break
    return rows


def pick_fb_shopify_products(rows: list[dict[str, Any]], limit: int = 80, min_score: int = 45) -> list[dict[str, Any]]:
    picked = []
    for row in rows:
        kw = row.get("keyword", "")
        if not _is_product_like(kw):
            continue
        score = int(row.get("fb_shopify_score") or 0)
        if score < min_score:
            continue
        item = dict(row)
        item["source"] = "amz123_fb_shopify_fit"
        item["fit_reason"] = ", ".join(item.get("tags") or [])
        picked.append(item)
    picked.sort(key=lambda r: (-int(r.get("fb_shopify_score") or 0), int(r.get("rank") or 999999)))
    return picked[:limit]


def update_daily_lists(
    *,
    force: bool = False,
    google_limit: int = 50,
    amz_limit: int = 200,
    fit_limit: int = 80,
    append_scan: bool = False,
) -> dict[str, Any]:
    state = load_existing()
    today = _today()
    if not force and state.get("last_run_date") == today:
        return {"skipped": True, "reason": "already_ran_today", "file": str(OUT_FILE), **build_summary(state)}

    existing_keys = {str(item.get("keyword", "")).lower() for item in state.get("items", []) if item.get("keyword")}
    existing_by_list = {
        (str(item.get("list", "")), str(item.get("keyword", "")).lower())
        for item in state.get("items", [])
        if item.get("keyword")
    }

    errors: list[dict[str, str]] = []
    google: list[dict[str, Any]] = []
    amz: list[dict[str, Any]] = []
    fit: list[dict[str, Any]] = []

    try:
        google = fetch_google_trends_products(limit=google_limit)
    except Exception as exc:
        errors.append({"source": "google_trends", "error": f"{type(exc).__name__}: {exc}"})
        log.warning(f"google trends product fetch failed: {exc}")

    try:
        amz = fetch_amz123_keywords(limit=amz_limit)
        fit = pick_fb_shopify_products(amz, limit=fit_limit)
    except Exception as exc:
        errors.append({"source": "amz123", "error": f"{type(exc).__name__}: {exc}"})
        log.warning(f"amz123 product fetch failed: {exc}")

    new_items: list[dict[str, Any]] = []
    for list_name, rows in (("google_trends_products", google), ("amz123_new_top_products", amz), ("amz123_fb_shopify_fit", fit)):
        for row in rows:
            kw = str(row.get("keyword", "")).lower()
            if not kw or (list_name, kw) in existing_by_list:
                continue
            item = dict(row)
            item["list"] = list_name
            item["first_seen"] = today
            item["last_seen"] = today
            item["global_duplicate"] = kw in existing_keys
            new_items.append(item)
            existing_by_list.add((list_name, kw))
            existing_keys.add(kw)

    if new_items:
        state.setdefault("items", []).extend(new_items)
    state["version"] = 1
    state["last_run_date"] = today
    state["updated_at"] = _now_iso()
    state["last_errors"] = errors
    OUT_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    appended = 0
    if append_scan:
        appended = append_fit_keywords_to_scan([i for i in new_items if i.get("list") == "amz123_fb_shopify_fit"])

    return {
        "skipped": False,
        "file": str(OUT_FILE),
        "new_items": len(new_items),
        "appended_to_scan": appended,
        "errors": errors,
        **build_summary(state),
    }


def append_fit_keywords_to_scan(items: list[dict[str, Any]]) -> int:
    if not items:
        return 0
    seen = set()
    if SCAN_FILE.exists():
        for line in SCAN_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            kw = line.split("#", 1)[0].strip().lower()
            if kw:
                seen.add(kw)
    fresh = []
    for item in items:
        kw = str(item.get("keyword", "")).strip().lower()
        if kw and kw not in seen:
            fresh.append(item)
            seen.add(kw)
    if not fresh:
        return 0
    with open(SCAN_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n# === v3 trend products {_today()} ===\n")
        for item in fresh:
            f.write(f"{item['keyword']}  # v3-trend score={item.get('fb_shopify_score')} rank={item.get('rank')}\n")
    return len(fresh)


def build_summary(state: dict[str, Any]) -> dict[str, Any]:
    items = state.get("items", []) if isinstance(state.get("items"), list) else []
    lists = ["google_trends_products", "amz123_new_top_products", "amz123_fb_shopify_fit"]
    out = {"last_run_date": state.get("last_run_date"), "total_items": len(items), "lists": {}}
    for name in lists:
        rows = [i for i in items if i.get("list") == name]
        latest = sorted(rows, key=lambda r: (r.get("first_seen", ""), int(r.get("fb_shopify_score") or 0)), reverse=True)[:25]
        out["lists"][name] = {
            "count": len(rows),
            "top": [
                {
                    "keyword": r.get("keyword"),
                    "rank": r.get("rank"),
                    "movement": r.get("movement"),
                    "fb_shopify_score": r.get("fb_shopify_score"),
                    "first_seen": r.get("first_seen"),
                    "tags": r.get("tags", [])[:5],
                }
                for r in latest
            ],
        }
    return out


def summary() -> dict[str, Any]:
    return {"file": str(OUT_FILE), **build_summary(load_existing())}

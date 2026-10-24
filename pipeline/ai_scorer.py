"""pipeline/ai_scorer.py — 纯规则 AI 选品评分引擎.

不调用外部模型 API。评分完全由确定性启发式规则组成, 方便复现、
缓存和审计。输入兼容 scraper / ad_archive 的广告字典。
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from core.logger import get_logger

from .persistence import DB_FILE

log = get_logger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS product_scores (
    ad_id TEXT PRIMARY KEY,
    brand_domain TEXT,
    score INTEGER,
    grade TEXT,
    longevity INTEGER, density INTEGER, market INTEGER,
    price_signal INTEGER, creative INTEGER,
    signals TEXT,
    flags TEXT,
    ali_keywords TEXT,
    est_price_range TEXT,
    scored_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_product_scores_grade_score
  ON product_scores(grade, score DESC);
CREATE INDEX IF NOT EXISTS idx_product_scores_brand
  ON product_scores(brand_domain);
CREATE INDEX IF NOT EXISTS idx_product_scores_scored_at
  ON product_scores(scored_at DESC);
"""

GRADE_LABELS = (
    (80, "HERO"),
    (65, "SOLID"),
    (45, "WATCH"),
    (0, "SKIP"),
)

COUNTRY_ALIASES = {
    "UNITED STATES": "US",
    "USA": "US",
    "US": "US",
    "UNITED KINGDOM": "UK",
    "GREAT BRITAIN": "UK",
    "GB": "UK",
    "UK": "UK",
    "AUSTRALIA": "AU",
    "AU": "AU",
    "CANADA": "CA",
    "CA": "CA",
    "GERMANY": "DE",
    "DEUTSCHLAND": "DE",
    "DE": "DE",
    "FRANCE": "FR",
    "FR": "FR",
}

STOPWORDS = {
    "about", "above", "after", "again", "against", "also", "amazon", "and",
    "are", "because", "before", "best", "better", "brand", "buy", "can",
    "click", "code", "deal", "deals", "discount", "does", "don", "each",
    "facebook", "find", "free", "from", "get", "gets", "getting", "good",
    "great", "have", "here", "into", "just", "learn", "less", "like",
    "limited", "link", "make", "makes", "made", "more", "most", "new",
    "now", "off", "offer", "official", "only", "order", "our", "out",
    "over", "price", "sale", "save", "shop", "shopping", "shipping",
    "should", "site", "store", "than", "that", "the", "their", "them",
    "then", "these", "they", "this", "today", "try", "use", "using",
    "very", "was", "were", "when", "with", "without", "your",
}

PRODUCT_HINTS = {
    "tool", "device", "kit", "set", "machine", "massager", "lamp", "light",
    "brush", "cleaner", "organizer", "pillow", "cushion", "chair", "desk",
    "mat", "bottle", "bag", "case", "cover", "cream", "serum", "mask",
    "patch", "belt", "brace", "band", "filter", "sprayer", "shaver",
    "trimmer", "charger", "adapter", "holder", "roller", "wrap",
}

PRICE_RE = re.compile(r"(?<![A-Za-z])(?:US\$|\$)\s*([0-9]{1,4}(?:\.[0-9]{1,2})?)")
WORD_RE = re.compile(r"[a-z][a-z0-9'-]{2,}", re.I)


def ensure_schema(db_file: Path | str = DB_FILE) -> None:
    """确保 product_scores 表存在。服务启动和独立批处理都可安全调用。"""
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _connect(db_file: Path | str = DB_FILE) -> sqlite3.Connection:
    ensure_schema(db_file)
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _ad_id(ad: dict[str, Any]) -> str:
    return str(
        ad.get("ad_id")
        or ad.get("ad_archive_id")
        or ad.get("id")
        or ""
    ).strip()


def _host(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url if "://" in str(url) else f"https://{url}")
        host = (parsed.hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _brand_domain(ad: dict[str, Any]) -> str:
    for key in ("brand_domain", "domain", "landing_domain"):
        value = str(ad.get(key) or "").strip().lower()
        if value:
            return _host(value) or value
    for key in ("landing_url", "link_url", "url"):
        value = _host(str(ad.get(key) or ""))
        if value:
            return value
    return ""


def _json_load_maybe(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str) or not value.strip():
        return value
    s = value.strip()
    if s[0] not in "[{":
        return value
    try:
        return json.loads(s)
    except Exception:
        return value


def _as_text_parts(value: Any) -> Iterable[str]:
    value = _json_load_maybe(value)
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("name", "country", "label", "code", "title", "body"):
            if value.get(key):
                parts.append(str(value[key]))
        for child in value.values():
            if isinstance(child, (list, dict)):
                parts.extend(_as_text_parts(child))
        return parts
    if isinstance(value, list):
        parts = []
        for item in value:
            parts.extend(_as_text_parts(item))
        return parts
    return [str(value)]


def _target_countries(ad: dict[str, Any]) -> set[str]:
    raw_values = [
        ad.get("target_countries"),
        ad.get("target_locations"),
        ad.get("countries"),
        ad.get("country"),
        ad.get("regional_distribution"),
    ]
    out: set[str] = set()
    for raw in raw_values:
        for part in _as_text_parts(raw):
            upper = re.sub(r"\s+", " ", part.upper()).strip()
            if not upper:
                continue
            if upper in COUNTRY_ALIASES:
                out.add(COUNTRY_ALIASES[upper])
            elif len(upper) == 2 and upper.isalpha():
                out.add(upper)
            else:
                for name, code in COUNTRY_ALIASES.items():
                    if name in upper:
                        out.add(code)
    return out


def _parse_date(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        try:
            # FB 的 unix timestamp 常见为秒级。
            if value > 1_000_000:
                return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return _parse_date(int(s))
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            return datetime.strptime(s[:19] if "T" in fmt else s[:10] if fmt == "%Y-%m-%d" else s, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _active_days(ad: dict[str, Any]) -> int:
    for key in ("active_days", "ad_age_days", "days_running"):
        try:
            value = int(ad.get(key) or 0)
            if value > 0:
                return value
        except Exception:
            pass
    start = (
        ad.get("ad_delivery_start_time")
        or ad.get("start_date_iso")
        or ad.get("start_date")
    )
    dt = _parse_date(start)
    if not dt:
        return 0
    return max(0, (datetime.now() - dt).days)


def _text_blob(ad: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "title", "body", "ad_creative_body", "ad_creative_link_title",
        "link_description", "link_caption", "page_name", "byline",
    ):
        if ad.get(key):
            parts.append(str(ad[key]))
    cards = _json_load_maybe(ad.get("cards"))
    if isinstance(cards, list):
        for card in cards[:8]:
            if isinstance(card, dict):
                parts.extend(str(card.get(k) or "") for k in ("title", "body", "link_description"))
    return " ".join(p for p in parts if p).strip()


def _extract_prices(text: str) -> list[float]:
    prices: list[float] = []
    for match in PRICE_RE.finditer(text or ""):
        try:
            value = float(match.group(1))
        except Exception:
            continue
        if 1 <= value <= 5000:
            prices.append(value)
    return prices


def _price_score(prices: list[float], signals: list[str], flags: list[str]) -> tuple[int, str]:
    if not prices:
        signals.append("no explicit price")
        return 8, ""
    price = min(prices)
    est_range = f"${min(prices):.0f}-${max(prices):.0f}" if len(prices) > 1 else f"${price:.0f}"
    if 25 <= price <= 80:
        signals.append("$25-$80 sweet spot")
        return 20, est_range
    if 15 <= price <= 24 or 81 <= price <= 150:
        signals.append("acceptable price band")
        return 12, est_range
    if 8 <= price <= 14:
        flags.append("thin margin price")
        return 5, est_range
    if price > 150:
        flags.append("premium price requires proof")
        return 3, est_range
    flags.append("very low price")
    return 2, est_range


def _density_from_db(domain: str) -> int:
    if not domain:
        return 1
    try:
        conn = sqlite3.connect(str(DB_FILE))
        try:
            row = conn.execute(
                "SELECT total_ads_across_scans FROM brands WHERE domain = ?",
                (domain,),
            ).fetchone()
            return int(row[0] or 1) if row else 1
        finally:
            conn.close()
    except Exception:
        return 1


def _density_score(ad: dict[str, Any], domain: str, signals: list[str], flags: list[str]) -> int:
    total = 0
    for key in ("brand_total_ads", "_brand_total_ads", "total_ads", "ads_count", "total_ads_across_scans"):
        try:
            total = max(total, int(ad.get(key) or 0))
        except Exception:
            pass
    if total <= 0:
        total = _density_from_db(domain)
    if total >= 20:
        signals.append("20+ ads by brand")
        return 20
    if total >= 10:
        signals.append("10+ ads by brand")
        return 15
    if total >= 5:
        signals.append("5+ ads by brand")
        return 10
    if total >= 2:
        return 5
    flags.append("single-ad brand")
    return 0


def _creative_score(ad: dict[str, Any], signals: list[str]) -> int:
    cards = _json_load_maybe(ad.get("cards"))
    card_count = int(ad.get("cards_count") or 0)
    if isinstance(cards, list):
        card_count = max(card_count, len(cards))
    images = 1 if ad.get("image_url") else 0
    videos = 1 if ad.get("video_url") else 0
    if isinstance(cards, list):
        for card in cards:
            if not isinstance(card, dict):
                continue
            images += 1 if card.get("image_url") else 0
            videos += 1 if card.get("video_url") else 0
    fmt_counts = ad.get("_brand_format_counts") if isinstance(ad.get("_brand_format_counts"), dict) else {}
    brand_video = int(fmt_counts.get("video", 0) or 0)
    brand_image = int(fmt_counts.get("image", 0) or 0)
    mixed = (images > 0 and videos > 0) or (brand_image > 0 and brand_video > 0)
    if mixed:
        signals.append("mixed image/video creative")
        return 15
    if videos >= 3 or brand_video >= 3:
        signals.append("video creative cluster")
        return 12
    if images >= 5 or card_count >= 5 or brand_image >= 5:
        signals.append("image/carousel creative depth")
        return 8
    if images + videos >= 1:
        return 3
    return 0


def _market_score(countries: set[str], signals: list[str], flags: list[str]) -> int:
    score = 0
    if "US" in countries:
        score += 10
        signals.append("US market")
    if countries & {"UK", "AU", "CA"}:
        score += 5
        signals.append("tier-1 English market")
    if countries & {"DE", "FR"}:
        score += 3
        signals.append("DE/FR expansion")
    if score == 0:
        flags.append("weak/unknown target market")
    return min(score, 20)


def _longevity_score(days: int, signals: list[str], flags: list[str]) -> int:
    if days >= 60:
        signals.append("60d+ running")
        return 25
    if days >= 30:
        signals.append("30d+ running")
        return 20
    if days >= 14:
        signals.append("14d+ running")
        return 12
    if days >= 7:
        return 6
    flags.append("too new to trust")
    return 0


def _grade(score: int) -> str:
    for threshold, grade in GRADE_LABELS:
        if score >= threshold:
            return grade
    return "SKIP"


def _brand_words(ad: dict[str, Any], domain: str) -> set[str]:
    words = set()
    for source in (domain, ad.get("page_name") or "", ad.get("brand") or ""):
        for token in WORD_RE.findall(str(source).replace(".", " ")):
            words.add(token.lower())
    return words


def _ali_keywords(ad: dict[str, Any], domain: str) -> list[str]:
    text = _text_blob(ad).lower()
    text = re.sub(r"https?://\S+|\$[0-9.]+|[0-9]+%?", " ", text)
    brand_words = _brand_words(ad, domain)
    tokens = [
        t.strip("'").lower()
        for t in WORD_RE.findall(text)
        if len(t) >= 3 and t.lower() not in STOPWORDS and t.lower() not in brand_words
    ]
    if not tokens:
        return []
    phrases: dict[str, int] = {}
    for n in (3, 2):
        for i in range(0, max(0, len(tokens) - n + 1)):
            chunk = tokens[i:i + n]
            if any(t in STOPWORDS or t in brand_words for t in chunk):
                continue
            phrase = " ".join(chunk)
            score = n
            if PRODUCT_HINTS & set(chunk):
                score += 4
            phrases[phrase] = max(phrases.get(phrase, 0), score)
    if not phrases:
        return tokens[:3]
    ranked = sorted(phrases.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
    out: list[str] = []
    seen = set()
    for phrase, _ in ranked:
        key = phrase.replace("-", " ")
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
        if len(out) == 3:
            break
    return out


def score_product(ad: dict) -> dict:
    """给单条广告评分。

    输入: 单条广告 JSON。
    输出: score / grade / dimensions / signals / flags / ali_keywords / est_price_range。
    """
    safe_ad = dict(ad or {})
    ad_id = _ad_id(safe_ad)
    domain = _brand_domain(safe_ad)
    signals: list[str] = []
    flags: list[str] = []

    days = _active_days(safe_ad)
    longevity = _longevity_score(days, signals, flags)
    density = _density_score(safe_ad, domain, signals, flags)
    countries = _target_countries(safe_ad)
    market = _market_score(countries, signals, flags)
    prices = _extract_prices(_text_blob(safe_ad))
    price_signal, est_price_range = _price_score(prices, signals, flags)
    creative = _creative_score(safe_ad, signals)

    score = int(max(0, min(100, longevity + density + market + price_signal + creative)))
    if density >= 20 and score < 70:
        flags.append("high competition")
    if not domain:
        flags.append("missing merchant domain")

    return {
        "ad_id": ad_id,
        "brand_domain": domain,
        "score": score,
        "grade": _grade(score),
        "dimensions": {
            "longevity": longevity,
            "density": density,
            "market": market,
            "price_signal": price_signal,
            "creative": creative,
        },
        "signals": sorted(set(signals)),
        "flags": sorted(set(flags)),
        "ali_keywords": _ali_keywords(safe_ad, domain),
        "est_price_range": est_price_range,
        "active_days": days,
        "target_countries": sorted(countries),
        "page_name": safe_ad.get("page_name") or safe_ad.get("brand") or "",
        "title": safe_ad.get("title") or safe_ad.get("ad_creative_link_title") or "",
        "link_url": safe_ad.get("link_url") or safe_ad.get("landing_url") or "",
    }


def _score_to_row(item: dict) -> tuple:
    dims = item.get("dimensions") or {}
    return (
        item.get("ad_id") or "",
        item.get("brand_domain") or "",
        int(item.get("score") or 0),
        item.get("grade") or "SKIP",
        int(dims.get("longevity") or 0),
        int(dims.get("density") or 0),
        int(dims.get("market") or 0),
        int(dims.get("price_signal") or 0),
        int(dims.get("creative") or 0),
        json.dumps(item.get("signals") or [], ensure_ascii=False),
        json.dumps(item.get("flags") or [], ensure_ascii=False),
        json.dumps(item.get("ali_keywords") or [], ensure_ascii=False),
        item.get("est_price_range") or "",
    )


def persist_scores(items: list[dict], db_file: Path | str = DB_FILE) -> int:
    """批量写入评分结果。相同 ad_id 只保留首次评分, 避免重复计算漂移。"""
    rows = [_score_to_row(item) for item in items if item.get("ad_id")]
    if not rows:
        return 0
    conn = _connect(db_file)
    try:
        conn.executemany(
            """INSERT OR IGNORE INTO product_scores(
                 ad_id, brand_domain, score, grade,
                 longevity, density, market, price_signal, creative,
                 signals, flags, ali_keywords, est_price_range
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        changed = conn.total_changes
        conn.commit()
        return changed
    finally:
        conn.close()


def _cached_scores(ad_ids: list[str], db_file: Path | str = DB_FILE) -> dict[str, dict]:
    ids = [i for i in ad_ids if i]
    if not ids:
        return {}
    conn = _connect(db_file)
    try:
        out: dict[str, dict] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ",".join("?" for _ in chunk)
            for row in conn.execute(f"SELECT * FROM product_scores WHERE ad_id IN ({q})", chunk):
                item = dict(row)
                dims = {
                    "longevity": item.pop("longevity", 0),
                    "density": item.pop("density", 0),
                    "market": item.pop("market", 0),
                    "price_signal": item.pop("price_signal", 0),
                    "creative": item.pop("creative", 0),
                }
                item["dimensions"] = dims
                for key in ("signals", "flags", "ali_keywords"):
                    try:
                        item[key] = json.loads(item.get(key) or "[]")
                    except Exception:
                        item[key] = []
                out[item["ad_id"]] = item
        return out
    finally:
        conn.close()


def _brand_context(ads: list[dict]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for ad in ads:
        domain = _brand_domain(ad)
        if not domain:
            continue
        bucket = grouped.setdefault(domain, {"total": 0, "formats": {"video": 0, "image": 0}})
        bucket["total"] += 1
        fmt = str(ad.get("display_format") or ad.get("format") or "").lower()
        if ad.get("video_url") or "video" in fmt:
            bucket["formats"]["video"] += 1
        if ad.get("image_url") or ad.get("cards") or "image" in fmt or "carousel" in fmt:
            bucket["formats"]["image"] += 1
    # 数据库里的品牌累计广告数优先作为 density 上限。
    try:
        conn = sqlite3.connect(str(DB_FILE))
        try:
            for domain, bucket in grouped.items():
                row = conn.execute(
                    "SELECT total_ads_across_scans FROM brands WHERE domain = ?",
                    (domain,),
                ).fetchone()
                if row:
                    bucket["total"] = max(int(bucket["total"]), int(row[0] or 0))
        finally:
            conn.close()
    except Exception:
        pass
    return grouped


def batch_score(ads: list[dict], min_score: int = 45) -> list[dict]:
    """并行评分广告, 过滤低分, 按分数降序返回并写入 product_scores。"""
    ensure_schema()
    clean_ads = [dict(ad) for ad in (ads or []) if isinstance(ad, dict)]
    if not clean_ads:
        return []

    context = _brand_context(clean_ads)
    for ad in clean_ads:
        domain = _brand_domain(ad)
        if domain in context:
            ad["_brand_total_ads"] = context[domain]["total"]
            ad["_brand_format_counts"] = context[domain]["formats"]

    cached = _cached_scores([_ad_id(ad) for ad in clean_ads])
    by_id = {_ad_id(ad): ad for ad in clean_ads}
    missing = [ad for ad in clean_ads if _ad_id(ad) not in cached]
    scored: list[dict] = []

    workers = max(1, min(8, math.ceil(len(missing) / 20) or 1))
    if missing:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(score_product, ad) for ad in missing]
            for fut in as_completed(futures):
                try:
                    scored.append(fut.result())
                except Exception as exc:
                    log.warning(f"ai_scorer: score failed: {exc}")
        persist_scores(scored)

    out: list[dict] = []
    for item in [*cached.values(), *scored]:
        if int(item.get("score") or 0) < min_score:
            continue
        ad = by_id.get(item.get("ad_id") or "", {})
        merged = dict(item)
        merged.setdefault("brand_domain", _brand_domain(ad))
        merged.setdefault("page_name", ad.get("page_name") or "")
        merged.setdefault("title", ad.get("title") or "")
        merged.setdefault("link_url", ad.get("link_url") or "")
        if "dimensions" not in merged:
            merged["dimensions"] = {}
        out.append(merged)

    out.sort(key=lambda x: (int(x.get("score") or 0), int(x.get("active_days") or 0)), reverse=True)
    return out


def score_brand_ads(brand_domain: str, min_score: int = 45, limit: int = 5000) -> list[dict]:
    """从 ad_archive 拉取某品牌广告并批量评分。"""
    domain = (brand_domain or "").strip().lower()
    if not domain:
        return []
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT a.*, COALESCE(b.total_ads_across_scans, 0) AS brand_total_ads
                   FROM ad_archive a
                   LEFT JOIN brands b ON b.domain = a.domain
                   WHERE a.domain = ?
                   ORDER BY COALESCE(a.active_days, 0) DESC, a.scan_date DESC
                   LIMIT ?""",
                (domain, limit),
            )
        ]
    finally:
        conn.close()
    return batch_score(rows, min_score=min_score)


def score_unscored_ads(limit: int = 2000, min_score: int = 0) -> dict[str, Any]:
    """自动评分最近入库但尚未评分的广告。

    daemon / scrape-now 在 raw_processor 写入 ad_archive 后调用这个函数,
    让新数据进入系统后自动拥有 AI 分。
    """
    ensure_schema()
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT a.*, COALESCE(b.total_ads_across_scans, 0) AS brand_total_ads
                   FROM ad_archive a
                   LEFT JOIN brands b ON b.domain = a.domain
                   LEFT JOIN product_scores ps ON ps.ad_id = a.ad_archive_id
                   WHERE ps.ad_id IS NULL
                   ORDER BY a.inserted_at DESC
                   LIMIT ?""",
                (max(1, int(limit)),),
            )
        ]
    finally:
        conn.close()
    scored = batch_score(rows, min_score=min_score) if rows else []
    return {
        "eligible": len(rows),
        "scored": len(scored),
        "min_score": min_score,
    }

"""pipeline/v3_selection_core.py — V3 极限选品核心.

异步 aiohttp 落地页探测 + 严格独立站过滤 + 批量 SQLite 落库 + 爆款识别。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Literal
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

import aiohttp
from aiohttp import ClientTimeout
from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from core.logger import get_logger

from .persistence import DB_FILE

log = get_logger(__name__)

MAX_HTML_BYTES = 1_500_000
DEFAULT_CONCURRENCY = 96
DEFAULT_BATCH_SIZE = 500
DEFAULT_TIMEOUT_SEC = 12

SCHEMA = """
CREATE TABLE IF NOT EXISTS v3_dtc_ads (
  ad_id TEXT PRIMARY KEY,
  landing_url TEXT NOT NULL,
  landing_domain TEXT NOT NULL,
  shop_engine TEXT NOT NULL,
  ad_copy TEXT,
  first_seen_date TEXT,
  last_seen_date TEXT,
  active_days INTEGER DEFAULT 0,
  is_active INTEGER DEFAULT 0,
  imp_upper INTEGER DEFAULT 0,
  previous_imp_upper INTEGER DEFAULT 0,
  impressions_growth_pct REAL DEFAULT 0,
  price_value REAL,
  price_band TEXT,
  volume_class TEXT,
  shipping_advantage INTEGER DEFAULT 0,
  status_label TEXT NOT NULL,
  quality_score INTEGER DEFAULT 0,
  reject_reason TEXT DEFAULT '',
  reasons TEXT,
  raw_json TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_v3_dtc_ads_label_score
  ON v3_dtc_ads(status_label, quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_v3_dtc_ads_domain
  ON v3_dtc_ads(landing_domain);
CREATE INDEX IF NOT EXISTS idx_v3_dtc_ads_seen
  ON v3_dtc_ads(last_seen_date DESC);

CREATE TABLE IF NOT EXISTS v3_fetch_runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT DEFAULT 'running',
  source TEXT,
  requested INTEGER DEFAULT 0,
  accepted INTEGER DEFAULT 0,
  rejected INTEGER DEFAULT 0,
  failed INTEGER DEFAULT 0,
  elapsed_sec REAL DEFAULT 0,
  config_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_v3_fetch_runs_started
  ON v3_fetch_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS v3_filter_rejections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  ad_id TEXT,
  landing_url TEXT,
  landing_domain TEXT,
  reject_reason TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_v3_filter_rejections_run
  ON v3_filter_rejections(run_id);
CREATE INDEX IF NOT EXISTS idx_v3_filter_rejections_reason
  ON v3_filter_rejections(reject_reason);

CREATE TABLE IF NOT EXISTS v3_ad_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ad_id TEXT NOT NULL,
  action TEXT NOT NULL,
  note TEXT DEFAULT '',
  reviewed_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_v3_ad_reviews_ad_time
  ON v3_ad_reviews(ad_id, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_v3_ad_reviews_action
  ON v3_ad_reviews(action);
"""

MARKETPLACE_ROOTS = {
    "amazon.com", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.it",
    "amazon.es", "amazon.ca", "amazon.com.au", "amazon.co.jp", "amazon.in",
    "amzn.to", "a.co", "aliexpress.com", "aliexpress.us", "alibaba.com",
    "1688.com", "taobao.com", "tmall.com", "tmall.hk", "lazada.com", "lazada.sg",
    "lazada.com.my", "lazada.co.id", "lazada.co.th", "lazada.com.ph", "lazada.vn",
    "temu.com", "shein.com", "romwe.com", "wish.com", "dhgate.com", "banggood.com",
    "gearbest.com", "lightinthebox.com", "made-in-china.com", "globalsources.com",
    "shopee.com", "shopee.sg", "shopee.com.my", "shopee.co.id", "shopee.co.th",
    "shopee.ph", "shopee.vn", "shopee.tw", "shopee.com.br", "shopee.com.mx",
    "walmart.com", "ebay.com", "ebay.co.uk", "etsy.com", "target.com", "costco.com",
    "bestbuy.com", "homedepot.com", "lowes.com", "wayfair.com", "overstock.com",
    "mercari.com", "poshmark.com", "depop.com", "newegg.com", "rakuten.com", "rakuten.co.jp",
    "flipkart.com", "myntra.com", "snapdeal.com", "tokopedia.com", "bukalapak.com",
    "blibli.com", "mercadolibre.com", "mercadolivre.com.br", "noon.com", "jumia.com",
    "otto.de", "zalando.com", "asos.com", "farfetch.com", "allegro.pl", "bol.com",
    "coupang.com", "gmarket.co.kr", "11st.co.kr", "qoo10.sg", "qoo10.jp",
    "tiktokshop.com", "shop.tiktok.com", "facebook.com/marketplace",
}

SOCIAL_ROOTS = {
    "facebook.com", "fb.com", "fb.me", "messenger.com", "m.me",
    "instagram.com", "youtube.com", "youtu.be", "tiktok.com",
    "tiktokshop.com", "twitter.com", "x.com", "pinterest.com",
    "reddit.com", "snapchat.com", "linkedin.com", "whatsapp.com",
    "wa.me", "telegram.me", "t.me", "discord.gg", "discord.com",
}

APP_MEDIA_ROOTS = {
    "apps.apple.com", "itunes.apple.com", "play.google.com", "spotify.com",
    "soundcloud.com", "netflix.com", "hulu.com", "twitch.tv", "medium.com",
    "substack.com", "patreon.com", "kickstarter.com", "gofundme.com",
}

LINK_AGGREGATORS = {
    "linktr.ee", "beacons.ai", "bio.link", "lnk.bio", "tap.bio",
    "campsite.bio", "msha.ke", "stan.store", "hoo.be", "solo.to",
    "bio.site", "allmylinks.com", "taplink.cc", "linkpop.com", "carrd.co",
    "flow.page", "milkshake.app", "withkoji.com", "direct.me",
}

SHORTENERS = {
    "bit.ly", "tinyurl.com", "rebrand.ly", "ow.ly", "buff.ly", "cutt.ly",
    "shorturl.at", "is.gd", "lnkd.in", "onelink.me", "bitly.com", "t.co",
}

MARKETPLACE_PATH_RE = re.compile(
    r"/(?:marketplace|shop|shops|seller|storefront|view/product|product/)\b|/dp/|/itm/|/listing/",
    re.I,
)
SOCIAL_SHOP_PATH_RE = re.compile(r"/(?:marketplace|shop|shops|store|seller|commerce|catalog|product)", re.I)

TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "fbclid", "gclid", "msclkid", "ttclid",
}

SHOPIFY_MARKERS = (
    "cdn.shopify.com", "cdn/shop/", "Shopify.theme", "window.Shopify",
    "shopify-section", "x-shopify", "myshopify.com", "shopify-payment-button",
)
WOO_MARKERS = (
    "wp-content/plugins/woocommerce", "woocommerce", "wc-ajax",
    "add-to-cart", "single_add_to_cart_button",
)
BIGCOMMERCE_MARKERS = (
    "cdn11.bigcommerce.com", "stencil-utils", "bigcommerce", "bc-cart",
)
MAGENTO_MARKERS = (
    "mage/cookies", "Magento_", "checkout/cart", "form_key",
)
SHOPLINE_MARKERS = (
    "shoplineapp.com", "SHOPLINE", "shopline-section",
)
WIX_MARKERS = (
    "static.wixstatic.com", "wixstores", "WixStores",
)

COMMERCE_PATH_RE = re.compile(
    r"/(?:products?|collections?|cart|checkout|shop|store|buy|sale|offer)s?(?:/|$)|[?&]variant=",
    re.I,
)
PRICE_RE = re.compile(r"(?<![A-Za-z])(?:US\$|\$)\s*([1-9][0-9]{0,3}(?:\.[0-9]{1,2})?)")
SMALL_SHIP_RE = re.compile(
    r"\b(?:portable|mini|compact|lightweight|foldable|wireless|handheld|travel|pocket|desk|countertop)\b",
    re.I,
)
BULKY_RE = re.compile(
    r"\b(?:sofa|mattress|treadmill|furniture|wardrobe|refrigerator|bike|bicycle|table|cabinet|large|oversized)\b",
    re.I,
)


def ensure_v3_schema() -> None:
    conn = sqlite3.connect(str(DB_FILE))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(SCHEMA)
        _ensure_column(conn, "v3_dtc_ads", "reject_reason", "TEXT DEFAULT ''")
        _ensure_column(conn, "v3_fetch_runs", "status", "TEXT DEFAULT 'done'")
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _host(url: str) -> str:
    try:
        p = urlparse(url if "://" in url else f"https://{url}")
        host = (p.hostname or "").lower().strip(".")
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def _root_domain(host: str) -> str:
    host = (host or "").lower().strip(".")
    if not host:
        return ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    second_level_cc = {"co", "com", "net", "org", "gov", "ac"}
    if len(parts[-1]) == 2 and parts[-2] in second_level_cc and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _suffix_match(host: str, roots: set[str]) -> bool:
    if not host:
        return False
    root = _root_domain(host)
    return host in roots or root in roots or any(host.endswith("." + d) for d in roots)


def normalize_url(url: str) -> str:
    raw = (url or "").strip().replace("\\/", "/")
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if "://" not in raw:
        raw = "https://" + raw
    try:
        p = urlparse(raw)
        if p.scheme not in {"http", "https"} or not p.netloc:
            return ""
        query = parse_qs(p.query, keep_blank_values=True)
        clean_query = "&".join(
            f"{k}={v}"
            for k, vals in query.items()
            if k not in TRACKING_QUERY_KEYS
            for v in vals[:1]
        )
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        netloc = host + ((f":{p.port}") if p.port else "")
        path = re.sub(r"/{2,}", "/", unquote(p.path or "/"))
        return urlunparse((p.scheme, netloc, path, "", clean_query, ""))
    except Exception:
        return ""


def hard_reject_reason(url_or_host: str) -> str:
    clean = normalize_url(url_or_host) or (url_or_host if "://" in str(url_or_host) else f"https://{url_or_host}")
    host = _host(clean)
    path = urlparse(clean if "://" in clean else f"https://{clean}").path.lower()
    if _suffix_match(host, MARKETPLACE_ROOTS):
        return "blocked_marketplace_domain"
    if _suffix_match(host, SOCIAL_ROOTS):
        return "blocked_social_shop_path" if SOCIAL_SHOP_PATH_RE.search(path) else "blocked_social_domain"
    if _suffix_match(host, APP_MEDIA_ROOTS):
        return "blocked_app_media_domain"
    if _suffix_match(host, LINK_AGGREGATORS):
        return "blocked_link_aggregator"
    if _suffix_match(host, SHORTENERS):
        return "blocked_shortener"
    if MARKETPLACE_PATH_RE.search(path) and any(x in host for x in ("facebook", "tiktok", "amazon", "ebay", "etsy")):
        return "blocked_marketplace_path"
    return ""


def is_hard_rejected_domain(url_or_host: str) -> bool:
    return bool(hard_reject_reason(url_or_host))


def _detect_engine(url: str, html: str, headers: dict[str, str]) -> str:
    host = _host(url)
    text = html[:MAX_HTML_BYTES]
    header_blob = " ".join(f"{k}:{v}" for k, v in (headers or {}).items())
    blob = f"{host}\n{header_blob}\n{text}"
    if host.endswith(".myshopify.com") or any(m in blob for m in SHOPIFY_MARKERS):
        return "shopify"
    if any(m.lower() in blob.lower() for m in WOO_MARKERS):
        return "woocommerce"
    if any(m.lower() in blob.lower() for m in BIGCOMMERCE_MARKERS):
        return "bigcommerce"
    if any(m.lower() in blob.lower() for m in MAGENTO_MARKERS):
        return "magento"
    if any(m.lower() in blob.lower() for m in SHOPLINE_MARKERS):
        return "shopline"
    if any(m.lower() in blob.lower() for m in WIX_MARKERS):
        return "wixstores"
    # 极严 fallback: 非平台域名 + 强 commerce path + cart/checkout 语义同时出现。
    if COMMERCE_PATH_RE.search(url) and re.search(r"\b(?:cart|checkout|add to cart|buy now)\b", text, re.I):
        return "custom_dtc"
    return ""


def is_strict_dtc(url: str, html: str = "", headers: dict[str, str] | None = None) -> tuple[bool, str, str]:
    clean = normalize_url(url)
    host = _host(clean)
    if not clean or not host:
        return False, "", "empty_url"
    reject_reason = hard_reject_reason(clean)
    if reject_reason:
        return False, "", reject_reason
    engine = _detect_engine(clean, html or "", headers or {})
    if not engine:
        return False, "", "no_dtc_engine"
    return True, engine, ""


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        try:
            if value > 1_000_000:
                return datetime.fromtimestamp(float(value))
        except Exception:
            return None
    s = str(value).strip().replace("Z", "+00:00")
    if not s:
        return None
    if s.isdigit():
        return _parse_dt(int(s))
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19] if "%S" in fmt else s[:10], fmt)
        except Exception:
            pass
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except Exception:
        return None


def _iso_date(value: Any, fallback: datetime | None = None) -> str:
    dt = _parse_dt(value) or fallback or datetime.now()
    return dt.strftime("%Y-%m-%d")


def _copy(ad: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "ad_copy", "Ad_Copy", "body", "title", "ad_creative_body",
        "link_description", "link_caption", "cta_text",
    ):
        if ad.get(key):
            parts.append(str(ad[key]).replace("\\/", "/"))
    cards = ad.get("cards")
    if isinstance(cards, str) and cards.strip().startswith("["):
        try:
            cards = json.loads(cards)
        except Exception:
            cards = []
    if isinstance(cards, list):
        for card in cards[:8]:
            if isinstance(card, dict):
                parts.extend(str(card.get(k) or "") for k in ("title", "body", "link_description"))
    text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    return text[:5000]


def _ad_id(ad: dict[str, Any]) -> str:
    return str(
        ad.get("ad_id")
        or ad.get("Ad_ID")
        or ad.get("ad_archive_id")
        or ad.get("archive_id")
        or ad.get("id")
        or ""
    ).strip()


def _landing_url(ad: dict[str, Any]) -> str:
    for key in (
        "landing_url", "Landing_URL", "landing_page_url",
        "link_url", "url", "destination_url", "ad_snapshot_url",
    ):
        url = normalize_url(str(ad.get(key) or ""))
        if url:
            return url
    return ""


def _imp_upper(ad: dict[str, Any]) -> int:
    for key in ("imp_upper", "impressions_upper", "impressions"):
        value = ad.get(key)
        if isinstance(value, dict):
            value = value.get("upper") or value.get("upper_bound")
        try:
            return max(0, int(value or 0))
        except Exception:
            pass
    return 0


def _extract_price(text: str, html: str = "") -> float | None:
    values = []
    for source in (text, html[:200_000]):
        for m in PRICE_RE.finditer(source or ""):
            try:
                v = float(m.group(1))
            except Exception:
                continue
            if 5 <= v <= 2000:
                values.append(v)
    return min(values) if values else None


def classify_price(price: float | None) -> str:
    if price is None:
        return "unknown"
    if price >= 120:
        return "high_ticket"
    if price >= 55:
        return "healthy_aov"
    if price >= 25:
        return "standard"
    return "low_margin"


def classify_volume(text: str, html: str = "") -> tuple[str, int]:
    blob = f"{text} {html[:80_000]}"
    if BULKY_RE.search(blob):
        return "bulky", 0
    if SMALL_SHIP_RE.search(blob):
        return "compact", 1
    return "unknown", 0


def score_2026_playbook(seed: V3AdSeed, html: str, *, active_days: int, price_band: str, volume_class: str, shipping_adv: int) -> tuple[int, list[str], list[str]]:
    raw = seed.raw or {}
    copy = (seed.ad_copy or "").lower()
    page = (html or "")[:180_000].lower()
    blob = f"{copy} {page}"
    score = 0
    hits: list[str] = []
    risks: list[str] = []

    if active_days >= 14:
        score += 10; hits.append("2026打法:广告存活≥14天")
    elif active_days < 7 and (seed.imp_upper >= 20_000 or seed.imp_upper == 0):
        score += 5; hits.append("2026打法:早期爆发观察")

    cards_count = 0
    try:
        cards_count = int(raw.get("cards_count") or 0)
    except Exception:
        cards_count = 0
    cards = raw.get("cards")
    if not cards_count and isinstance(cards, str) and cards.strip().startswith("["):
        try:
            cards_count = len(json.loads(cards))
        except Exception:
            cards_count = 0
    if cards_count >= 5:
        score += 8; hits.append("2026打法:多素材矩阵≥5")
    elif cards_count >= 3:
        score += 4; hits.append("2026打法:素材变体≥3")

    platforms = raw.get("publisher_platforms") or ""
    if not isinstance(platforms, str):
        platforms = json.dumps(platforms, ensure_ascii=False)
    plat = platforms.upper()
    platform_count = sum(1 for p in ("FACEBOOK", "INSTAGRAM", "MESSENGER", "AUDIENCE_NETWORK", "THREADS") if p in plat)
    if platform_count >= 3:
        score += 7; hits.append("2026打法:跨版位扩量")
    elif platform_count >= 2:
        score += 4; hits.append("2026打法:FB+IG双版位")

    cta = str(raw.get("cta_type") or raw.get("cta_text") or "").lower()
    if any(x in cta for x in ("shop", "buy", "offer", "learn_more", "learn more")):
        score += 5; hits.append("2026打法:强CTA")

    if re.search(r"\b(ugc|real customer|customer|review|before and after|before/after|testimonial|doctor|dentist|clinically|study|proven|demo|result)\b", blob):
        score += 8; hits.append("2026打法:UGC/证明型创意")
    if re.search(r"\b(struggle|tired of|sick of|problem|pain|bad breath|anxiety|sleep|relief|fix|without|alternative|hate|wish)\b", blob):
        score += 7; hits.append("2026打法:痛点开场")
    if re.search(r"\b(free shipping|bundle|save|discount|% off|limited|guarantee|warranty|risk-free|money back|subscribe)\b", blob):
        score += 7; hits.append("2026打法:Offer价值堆叠")
    if re.search(r"\b(reviews?|stars?|faq|shipping|returns?|add to cart|buy now|shop pay|paypal|apple pay|klarna)\b", page):
        score += 6; hits.append("2026打法:落地页信任/支付齐全")
    if price_band in {"healthy_aov", "high_ticket"} and volume_class != "bulky" and shipping_adv:
        score += 7; hits.append("2026打法:利润/物流结构适合冷流量")

    trend_terms = ("red light", "probiotic", "cortisol", "vagus", "somatic", "lymph", "creatine", "menopause", "perimenopause", "non toxic", "grounding", "sleep", "oral", "longevity")
    if any(t in blob for t in trend_terms):
        score += 6; hits.append("2026打法:健康/自我优化趋势词")

    if re.search(r"\b(cure|guaranteed results|miracle|fda approved|lose \d+|weight loss injection|ozempic|copyright|replica)\b", blob):
        score -= 15; risks.append("2026风险:合规/侵权/夸大承诺")
    if volume_class == "bulky":
        score -= 10; risks.append("2026风险:大件运费吃利润")
    if price_band == "low_margin":
        score -= 10; risks.append("2026风险:低客单利润不足")

    return max(-35, min(55, score)), hits, risks


@dataclass(slots=True)
class V3AdSeed:
    ad_id: str
    landing_url: str
    ad_copy: str
    first_seen_date: str
    last_seen_date: str
    is_active: int
    imp_upper: int = 0
    previous_imp_upper: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class V3Pick:
    ad_id: str
    landing_url: str
    landing_domain: str
    shop_engine: str
    ad_copy: str
    first_seen_date: str
    last_seen_date: str
    active_days: int
    is_active: int
    imp_upper: int
    previous_imp_upper: int
    impressions_growth_pct: float
    price_value: float | None
    price_band: str
    volume_class: str
    shipping_advantage: int
    status_label: str
    quality_score: int
    reject_reason: str
    reasons: list[str]
    raw_json: dict[str, Any]


def extract_seed(ad: dict[str, Any], previous_imp: int = 0) -> V3AdSeed | None:
    ad_id = _ad_id(ad)
    landing = _landing_url(ad)
    if not ad_id or not landing:
        return None
    now = datetime.now()
    first = (
        ad.get("first_seen_date")
        or ad.get("First_Seen_Date")
        or ad.get("first_seen_at")
        or ad.get("start_date_iso")
        or ad.get("start_date")
        or ad.get("scan_date")
        or ad.get("inserted_at")
    )
    last = (
        ad.get("last_seen_date")
        or ad.get("Last_Seen_Date")
        or ad.get("last_seen_at")
        or ad.get("updated_at")
        or ad.get("scan_date")
        or ad.get("inserted_at")
    )
    is_active = 1 if ad.get("is_active") in (1, True, "1", "true", "True", None) else 0
    return V3AdSeed(
        ad_id=ad_id,
        landing_url=landing,
        ad_copy=_copy(ad),
        first_seen_date=_iso_date(first, now),
        last_seen_date=_iso_date(last, now),
        is_active=is_active,
        imp_upper=_imp_upper(ad),
        previous_imp_upper=previous_imp,
        raw=dict(ad),
    )


class V3AsyncSelectionEngine:
    def __init__(
        self,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        batch_size: int = DEFAULT_BATCH_SIZE,
        user_agent: str | None = None,
    ) -> None:
        self.concurrency = max(4, min(int(concurrency), 300))
        self.timeout_sec = max(4, int(timeout_sec))
        self.batch_size = max(50, int(batch_size))
        self.sem = asyncio.Semaphore(self.concurrency)
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )

    async def _fetch_html(self, session: aiohttp.ClientSession, url: str) -> tuple[str, str, dict[str, str], str]:
        async with self.sem:
            try:
                async with session.get(url, allow_redirects=True) as resp:
                    final_url = normalize_url(str(resp.url))
                    headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                    if resp.status >= 400:
                        return final_url, "", headers, f"http_{resp.status}"
                    chunks = bytearray()
                    async for chunk in resp.content.iter_chunked(65536):
                        chunks.extend(chunk)
                        if len(chunks) >= MAX_HTML_BYTES:
                            break
                    text = bytes(chunks).decode(resp.charset or "utf-8", errors="replace")
                    return final_url, text, headers, ""
            except asyncio.TimeoutError:
                return url, "", {}, "timeout"
            except Exception as exc:
                return url, "", {}, f"{type(exc).__name__}: {exc}"

    def _judge(self, seed: V3AdSeed, final_url: str, html: str, headers: dict[str, str]) -> tuple[V3Pick | None, str]:
        ok, engine, reject = is_strict_dtc(final_url or seed.landing_url, html, headers)
        if not ok:
            return None, reject

        first_dt = _parse_dt(seed.first_seen_date) or datetime.now()
        last_dt = _parse_dt(seed.last_seen_date) or datetime.now()
        active_days = max(0, (last_dt - first_dt).days)
        if seed.is_active:
            active_days = max(active_days, (datetime.now() - first_dt).days)

        growth = 0.0
        if seed.previous_imp_upper > 0 and seed.imp_upper > seed.previous_imp_upper:
            growth = round((seed.imp_upper - seed.previous_imp_upper) / seed.previous_imp_upper * 100, 2)
        elif seed.previous_imp_upper == 0 and seed.imp_upper >= 5000:
            growth = 999.0

        price = _extract_price(seed.ad_copy, html)
        price_band = classify_price(price)
        volume_class, shipping_adv = classify_volume(seed.ad_copy, html)
        reasons: list[str] = [engine]
        playbook_score, playbook_hits, playbook_risks = score_2026_playbook(
            seed,
            html,
            active_days=active_days,
            price_band=price_band,
            volume_class=volume_class,
            shipping_adv=shipping_adv,
        )

        if active_days < 7 and (growth >= 120 or seed.imp_upper >= 20_000):
            label = "潜力爆发品"
            reasons.append("投放<7天且展现激增")
        elif active_days > 15 and seed.is_active:
            label = "已验证爆款"
            reasons.append("持续投放>15天且仍Active")
        else:
            label = "观察池"
            reasons.append("信号未完全确认")

        score = 0
        score += 35 if label == "已验证爆款" else 30 if label == "潜力爆发品" else 12
        score += min(20, active_days)
        score += 15 if engine in {"shopify", "woocommerce"} else 10
        score += 15 if price_band == "high_ticket" else 12 if price_band == "healthy_aov" else 6 if price_band == "standard" else 0
        score += 12 if shipping_adv else -10 if volume_class == "bulky" else 3
        score += min(8, int(math.log10(max(seed.imp_upper, 1)) * 2)) if seed.imp_upper else 0
        if growth >= 120:
            score += 10
        score += playbook_score
        quality_score = max(0, min(100, score))

        if price_band in {"high_ticket", "healthy_aov"}:
            reasons.append("高客单/健康AOV")
        if shipping_adv:
            reasons.append("低运费体积特征")
        if volume_class == "bulky":
            reasons.append("大件运费风险")
        reasons.extend(playbook_hits[:8])
        reasons.extend(playbook_risks[:4])

        return V3Pick(
            ad_id=seed.ad_id,
            landing_url=final_url or seed.landing_url,
            landing_domain=_host(final_url or seed.landing_url),
            shop_engine=engine,
            ad_copy=seed.ad_copy,
            first_seen_date=seed.first_seen_date,
            last_seen_date=seed.last_seen_date,
            active_days=active_days,
            is_active=seed.is_active,
            imp_upper=seed.imp_upper,
            previous_imp_upper=seed.previous_imp_upper,
            impressions_growth_pct=growth,
            price_value=price,
            price_band=price_band,
            volume_class=volume_class,
            shipping_advantage=shipping_adv,
            status_label=label,
            quality_score=quality_score,
            reject_reason="",
            reasons=reasons,
            raw_json=seed.raw,
        ), ""

    async def analyze(self, seeds: list[V3AdSeed]) -> dict[str, Any]:
        ensure_v3_schema()
        run_id = hashlib.sha1(f"{time.time()}:{len(seeds)}".encode()).hexdigest()[:16]
        started = time.perf_counter()
        await asyncio.to_thread(
            _insert_run_start,
            run_id,
            {
                "source": "v3_async_core",
                "requested": len(seeds),
                "config": {
                    "concurrency": self.concurrency,
                    "timeout_sec": self.timeout_sec,
                    "batch_size": self.batch_size,
                },
            },
        )

        timeout = ClientTimeout(total=self.timeout_sec, connect=min(5, self.timeout_sec))
        connector = aiohttp.TCPConnector(
            limit=self.concurrency * 2,
            limit_per_host=12,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        headers = {
            "user-agent": self.user_agent,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
        }
        accepted: list[V3Pick] = []
        rejected = 0
        failed = 0
        reject_buffer: list[tuple[str, str, str, str, str]] = []
        fetchable_seeds: list[V3AdSeed] = []
        for seed in seeds:
            clean_url = normalize_url(seed.landing_url)
            if not clean_url:
                rejected += 1
                reject_buffer.append((run_id, seed.ad_id, seed.landing_url, "", "empty_url"))
                continue
            seed.landing_url = clean_url
            if is_hard_rejected_domain(clean_url):
                rejected += 1
                reject_buffer.append((run_id, seed.ad_id, clean_url, _host(clean_url), "blocked_platform"))
                continue
            fetchable_seeds.append(seed)

        async with aiohttp.ClientSession(timeout=timeout, connector=connector, headers=headers) as session:
            async def fetch_seed(seed: V3AdSeed) -> tuple[V3AdSeed, str, str, dict[str, str], str]:
                final_url, html, resp_headers, err = await self._fetch_html(session, seed.landing_url)
                return seed, final_url, html, resp_headers, err

            tasks = [asyncio.create_task(fetch_seed(seed)) for seed in fetchable_seeds]
            buffer: list[V3Pick] = []
            for task in asyncio.as_completed(tasks):
                seed, final_url, html, resp_headers, err = await task
                if err:
                    failed += 1
                    reject_buffer.append((
                        run_id, seed.ad_id, final_url or seed.landing_url,
                        _host(final_url or seed.landing_url), f"fetch_{err}"[:120],
                    ))
                    continue
                pick, reason = self._judge(seed, final_url, html, resp_headers)
                if pick is None:
                    rejected += 1
                    reject_buffer.append((
                        run_id, seed.ad_id, final_url or seed.landing_url,
                        _host(final_url or seed.landing_url), reason or "rejected",
                    ))
                    if len(reject_buffer) >= self.batch_size:
                        await asyncio.to_thread(_bulk_insert_rejections, reject_buffer)
                        reject_buffer.clear()
                    continue
                accepted.append(pick)
                buffer.append(pick)
                if len(buffer) >= self.batch_size:
                    await asyncio.to_thread(_bulk_upsert_picks, buffer)
                    buffer.clear()
            if buffer:
                await asyncio.to_thread(_bulk_upsert_picks, buffer)
            if reject_buffer:
                await asyncio.to_thread(_bulk_insert_rejections, reject_buffer)

        elapsed = round(time.perf_counter() - started, 2)
        await asyncio.to_thread(_finish_run, run_id, len(accepted), rejected, failed, elapsed)
        accepted.sort(key=lambda p: (p.quality_score, p.active_days, p.impressions_growth_pct), reverse=True)
        return {
            "run_id": run_id,
            "requested": len(seeds),
            "accepted": len(accepted),
            "rejected": rejected,
            "failed": failed,
            "elapsed_sec": elapsed,
            "throughput_per_sec": round(len(seeds) / max(elapsed, 0.001), 2),
            "picks": [asdict(p) for p in accepted[:200]],
        }


def _connect() -> sqlite3.Connection:
    ensure_v3_schema()
    conn = sqlite3.connect(str(DB_FILE), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _insert_run_start(run_id: str, payload: dict[str, Any]) -> None:
    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO v3_fetch_runs(
                 run_id, started_at, status, source, requested, config_json
               ) VALUES (?,?,?,?,?,?)""",
            (
                run_id,
                datetime.now().isoformat(timespec="seconds"),
                "running",
                payload.get("source"),
                int(payload.get("requested") or 0),
                json.dumps(payload.get("config") or {}, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _finish_run(run_id: str, accepted: int, rejected: int, failed: int, elapsed: float) -> None:
    conn = _connect()
    try:
        conn.execute(
            """UPDATE v3_fetch_runs
               SET finished_at=?, status='done', accepted=?, rejected=?, failed=?, elapsed_sec=?
               WHERE run_id=?""",
            (datetime.now().isoformat(timespec="seconds"), accepted, rejected, failed, elapsed, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _bulk_insert_rejections(rows: list[tuple[str, str, str, str, str]]) -> int:
    if not rows:
        return 0
    conn = _connect()
    try:
        conn.executemany(
            """INSERT INTO v3_filter_rejections(
                 run_id, ad_id, landing_url, landing_domain, reject_reason
               ) VALUES (?,?,?,?,?)""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _bulk_upsert_picks(picks: list[V3Pick]) -> int:
    if not picks:
        return 0
    rows = [
        (
            p.ad_id,
            p.landing_url,
            p.landing_domain,
            p.shop_engine,
            p.ad_copy,
            p.first_seen_date,
            p.last_seen_date,
            p.active_days,
            p.is_active,
            p.imp_upper,
            p.previous_imp_upper,
            p.impressions_growth_pct,
            p.price_value,
            p.price_band,
            p.volume_class,
            p.shipping_advantage,
            p.status_label,
            p.quality_score,
            p.reject_reason,
            json.dumps(p.reasons, ensure_ascii=False),
            json.dumps(p.raw_json, ensure_ascii=False)[:120_000],
        )
        for p in picks
    ]
    conn = _connect()
    try:
        conn.executemany(
            """INSERT INTO v3_dtc_ads(
                 ad_id, landing_url, landing_domain, shop_engine, ad_copy,
                 first_seen_date, last_seen_date, active_days, is_active,
                 imp_upper, previous_imp_upper, impressions_growth_pct,
                 price_value, price_band, volume_class, shipping_advantage,
                 status_label, quality_score, reject_reason, reasons, raw_json
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(ad_id) DO UPDATE SET
                 landing_url=excluded.landing_url,
                 landing_domain=excluded.landing_domain,
                 shop_engine=excluded.shop_engine,
                 ad_copy=excluded.ad_copy,
                 last_seen_date=excluded.last_seen_date,
                 active_days=excluded.active_days,
                 is_active=excluded.is_active,
                 previous_imp_upper=v3_dtc_ads.imp_upper,
                 imp_upper=MAX(v3_dtc_ads.imp_upper, excluded.imp_upper),
                 impressions_growth_pct=excluded.impressions_growth_pct,
                 price_value=COALESCE(excluded.price_value, v3_dtc_ads.price_value),
                 price_band=excluded.price_band,
                 volume_class=excluded.volume_class,
                 shipping_advantage=excluded.shipping_advantage,
                 status_label=excluded.status_label,
                 quality_score=excluded.quality_score,
                 reject_reason=excluded.reject_reason,
                 reasons=excluded.reasons,
                 raw_json=excluded.raw_json,
                 updated_at=datetime('now')""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def load_recent_ad_archive(limit: int = 2000, domain: str | None = None) -> list[V3AdSeed]:
    conn = _connect()
    try:
        where = "WHERE a.link_url IS NOT NULL AND a.link_url != ''"
        params: list[Any] = []
        if domain:
            where += " AND a.domain = ?"
            params.append(domain.strip().lower())
        params.append(limit)
        rows = conn.execute(
            f"""SELECT a.*, COALESCE(v.imp_upper, 0) AS prev_imp_upper
                FROM ad_archive a
                LEFT JOIN v3_dtc_ads v ON v.ad_id = a.ad_archive_id
                {where}
                ORDER BY a.inserted_at DESC
                LIMIT ?""",
            params,
        ).fetchall()
        seeds: list[V3AdSeed] = []
        for row in rows:
            ad = dict(row)
            seed = extract_seed(ad, previous_imp=int(ad.get("prev_imp_upper") or 0))
            if seed:
                seeds.append(seed)
        return seeds
    finally:
        conn.close()


def list_v3_picks(
    *,
    status_label: str | None = None,
    engine: str | None = None,
    min_score: int = 0,
    limit: int = 100,
) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        sql = """
            SELECT d.*,
                   rv.action AS review_action,
                   rv.note AS review_note,
                   rv.reviewed_at,
                   CASE rv.action
                     WHEN 'approve' THEN d.quality_score + 12
                     WHEN 'watch' THEN d.quality_score + 3
                     WHEN 'skip' THEN d.quality_score - 50
                     ELSE d.quality_score
                   END AS adjusted_score
            FROM v3_dtc_ads d
            LEFT JOIN (
              SELECT r1.*
              FROM v3_ad_reviews r1
              JOIN (
                SELECT ad_id, MAX(reviewed_at) AS reviewed_at
                FROM v3_ad_reviews
                GROUP BY ad_id
              ) last
                ON last.ad_id = r1.ad_id
               AND last.reviewed_at = r1.reviewed_at
            ) rv ON rv.ad_id = d.ad_id
            WHERE d.quality_score >= ?
        """
        params: list[Any] = [min_score]
        if status_label:
            sql += " AND d.status_label = ?"
            params.append(status_label)
        if engine:
            sql += " AND d.shop_engine = ?"
            params.append(engine)
        sql += " ORDER BY adjusted_score DESC, d.active_days DESC, d.impressions_growth_pct DESC LIMIT ?"
        params.append(limit)
        out = []
        for row in conn.execute(sql, params):
            item = dict(row)
            try:
                item["reasons"] = json.loads(item.get("reasons") or "[]")
            except Exception:
                item["reasons"] = []
            item["test_advice"] = test_advice(item)
            out.append(item)
        return out
    finally:
        conn.close()


def test_advice(item: dict[str, Any]) -> dict[str, Any]:
    score = int(item.get("adjusted_score") or item.get("quality_score") or 0)
    label = item.get("status_label") or ""
    price_band = item.get("price_band") or "unknown"
    volume = item.get("volume_class") or "unknown"
    shipping = int(item.get("shipping_advantage") or 0)
    if item.get("review_action") == "skip":
        return {"level": "放弃", "budget": "$0", "reason": "人工跳过"}
    if label in {"已验证爆款", "潜力爆发品"} and score >= 75 and volume != "bulky":
        return {"level": "立即测", "budget": "$80-$150/day", "reason": "强投放信号 + 独立站确认"}
    if score >= 58 and price_band in {"high_ticket", "healthy_aov"} and (shipping or volume != "bulky"):
        return {"level": "小预算观察", "budget": "$30-$60/day", "reason": "利润结构可测, 需补创意/受众验证"}
    if volume == "bulky" or price_band == "low_margin" or score < 45:
        return {"level": "放弃", "budget": "$0", "reason": "运费/客单/信号不达标"}
    return {"level": "小预算观察", "budget": "$20-$40/day", "reason": "进入观察池, 等二次信号"}


def _layer(score: int, label: str, reasons: list[str] | None = None, risks: list[str] | None = None) -> dict[str, Any]:
    return {"score": max(0, min(100, int(score))), "label": label, "reasons": reasons or [], "risks": risks or []}


def build_executive_scorecard(item: dict[str, Any]) -> dict[str, Any]:
    reasons = [str(x) for x in item.get("reasons") or []]
    raw = item.get("raw_json") or {}
    if not isinstance(raw, dict):
        raw = {}
    copy = str(item.get("ad_copy") or "").lower()
    blob = f"{copy} {' '.join(reasons).lower()} {json.dumps(raw, ensure_ascii=False).lower()[:20_000]}"
    score = int(item.get("adjusted_score") or item.get("quality_score") or 0)
    price_band = str(item.get("price_band") or "unknown")
    volume = str(item.get("volume_class") or "unknown")
    shipping = int(item.get("shipping_advantage") or 0)
    growth = float(item.get("impressions_growth_pct") or 0)
    active_days = int(item.get("active_days") or 0)
    imp = int(item.get("imp_upper") or 0)
    reason_blob = " ".join(reasons)
    green_flags: list[str] = []
    red_flags: list[str] = []

    market = 35 + min(25, active_days) + (18 if growth >= 120 else 12 if growth >= 35 else 6 if growth >= 15 else 0) + (12 if imp >= 20000 else 6 if imp >= 5000 else 0)
    if any(x in reason_blob for x in ("跨版位", "趋势词", "广告存活")):
        market += 12
        green_flags.append("需求信号: 存活/趋势/跨版位证明市场在动")
    market_label = "大盘可打" if market >= 75 else "中等需求" if market >= 55 else "需求待验证"

    margin = 40 + (28 if price_band == "high_ticket" else 22 if price_band == "healthy_aov" else 10 if price_band == "standard" else -20 if price_band == "low_margin" else 0) + (12 if shipping else 0) + (-22 if volume == "bulky" else 8 if volume in {"compact", "small"} else 0)
    if re.search(r"\b(bundle|subscribe|subscription|refill|replacement|kit|set)\b", blob):
        margin += 10
        green_flags.append("利润结构: 可做套装/订阅/补充装")
    if price_band == "low_margin":
        red_flags.append("利润风险: 低客单可能吃不住冷流量 CPA")
    margin_label = "高毛利可投" if margin >= 75 else "毛利可测" if margin >= 55 else "毛利偏弱"

    creative = 35
    for key, pts in (("多素材矩阵", 18), ("素材变体", 10), ("UGC", 16), ("证明型创意", 14), ("痛点开场", 14), ("Offer价值堆叠", 10), ("跨版位", 10)):
        if key in reason_blob:
            creative += pts
    if re.search(r"\b(demo|before and after|before/after|review|testimonial|real customer|results?)\b", blob):
        creative += 12
    creative_label = "素材可规模化" if creative >= 75 else "素材可测试" if creative >= 55 else "素材角度不足"

    compliance = 86
    compliance_risks = []
    for pat, risk in ((r"\b(cure|miracle|fda approved|guaranteed results|reverse|permanent)\b", "夸大功效/医疗承诺"), (r"\b(ozempic|weight loss injection|lose \d+)\b", "减肥/药物敏感宣称"), (r"\b(replica|dupe|copyright|trademark)\b", "IP/仿牌风险")):
        if re.search(pat, blob):
            compliance -= 28
            compliance_risks.append(risk)
    if "2026风险" in reason_blob:
        compliance -= 18
        compliance_risks.append("已命中 2026 合规风险词")
    red_flags.extend(f"合规风险: {x}" for x in compliance_risks[:3])
    compliance_label = "低风险" if compliance >= 75 else "需改 claims" if compliance >= 50 else "高风险不碰"

    logistics = 55 + (18 if shipping else 0) + (12 if volume in {"compact", "small"} else -30 if volume == "bulky" else 0)
    logistics_risks = []
    if re.search(r"\b(liquid|glass|fragile|battery|oversized|heavy|furniture|mattress)\b", blob):
        logistics -= 18
        logistics_risks.append("运输/破损/限制品风险")
    if volume == "bulky":
        logistics_risks.append("大件运费吃利润")
    red_flags.extend(f"物流风险: {x}" for x in logistics_risks[:2])
    logistics_label = "供应链友好" if logistics >= 75 else "可报价验证" if logistics >= 50 else "物流风险高"

    saturation = 70
    saturation_risks = []
    if active_days > 180 and growth < 15:
        saturation -= 25
        saturation_risks.append("老品低增长，可能已进入红海")
    if "多素材矩阵" in reason_blob and growth < 10:
        saturation -= 12
        saturation_risks.append("素材多但增长弱，可能素材疲劳")
    if saturation_risks:
        red_flags.extend(f"饱和风险: {x}" for x in saturation_risks[:2])
    saturation_label = "竞争仍可切入" if saturation >= 70 else "需差异化切入" if saturation >= 45 else "红海谨慎"

    moat = 38
    if re.search(r"\b(patent|clinically|ingredient|formula|mechanism|community|quiz|routine|system|kit|bundle|faq|reviews?)\b", blob):
        moat += 24
    if price_band in {"healthy_aov", "high_ticket"}:
        moat += 8
    if re.search(r"\b(generic|cheap|gadget|viral product)\b", blob):
        moat -= 12
    moat_label = "可品牌化" if moat >= 70 else "快进快出" if moat >= 45 else "白牌同质"
    if moat < 45:
        red_flags.append("护城河弱: 适合快测快跑，不适合重仓囤货")

    urgency = 35 + (25 if 7 <= active_days <= 45 else 16 if 46 <= active_days <= 120 else 5 if active_days > 120 else 12) + (24 if growth >= 120 else 14 if growth >= 35 else 6 if growth >= 15 else 0)
    if any(x in reason_blob for x in ("早期爆发", "趋势词", "多素材矩阵")):
        urgency += 12
    urgency_label = "马上测试" if urgency >= 75 else "本周验证" if urgency >= 55 else "继续观察"

    layers = {
        "market_size": _layer(market, market_label, ["广告存活/增长/展现/趋势综合"]),
        "margin": _layer(margin, margin_label, [f"{price_band}/{volume}/shipping={shipping}"]),
        "creative_scalability": _layer(creative, creative_label, ["素材矩阵/UGC/痛点/证明/版位综合"]),
        "compliance_risk": _layer(compliance, compliance_label, risks=compliance_risks),
        "supply_chain_logistics": _layer(logistics, logistics_label, risks=logistics_risks),
        "saturation": _layer(saturation, saturation_label, risks=saturation_risks),
        "moat": _layer(moat, moat_label, ["机制教育/品牌化/套装/内容壁垒"]),
        "urgency": _layer(urgency, urgency_label, ["时效窗口/增长速度/趋势命中"]),
    }
    overall = round(sum(v["score"] for v in layers.values()) / len(layers))
    if compliance < 45 or logistics < 35 or margin < 35:
        operator_verdict = "不碰"
    elif overall >= 78 and urgency >= 65:
        operator_verdict = "立即上架"
    elif overall >= 62:
        operator_verdict = "小预算跟测"
    else:
        operator_verdict = "观察"
    if overall >= 70:
        green_flags.append("组合判断: 市场、毛利、素材和执行窗口具备赚钱测试条件")
    money_logic = f"{market_label} + {margin_label} + {creative_label}；{compliance_label}，建议{operator_verdict}。"
    return {"overall": overall, "operator_verdict": operator_verdict, "layers": layers, "green_flags": green_flags[:6], "red_flags": red_flags[:6], "money_making_logic": money_logic}


def auto_follow_decision(item: dict[str, Any]) -> dict[str, Any]:
    score = int(item.get("adjusted_score") or item.get("quality_score") or 0)
    label = item.get("status_label") or ""
    price_band = item.get("price_band") or "unknown"
    volume = item.get("volume_class") or "unknown"
    growth = float(item.get("impressions_growth_pct") or 0)
    active_days = int(item.get("active_days") or 0)
    shipping = int(item.get("shipping_advantage") or 0)
    reasons = list(item.get("reasons") or [])
    reason_blob = " ".join(str(x) for x in reasons)
    scorecard = item.get("executive_scorecard") or build_executive_scorecard(item)
    layers = scorecard.get("layers") or {}
    layer_score = lambda name: int((layers.get(name) or {}).get("score") or 0)
    if item.get("review_action") == "skip" or volume == "bulky" or price_band == "low_margin" or score < 58 or layer_score("compliance_risk") < 35 or layer_score("supply_chain_logistics") < 30:
        return {"decision": "放弃", "priority": 0, "budget": "$0", "confidence": min(score, 55), "why": "分数/利润/运输/合规结构不适合跟品", "next_step": "不进入跟品队列", "operator_verdict": scorecard.get("operator_verdict"), "money_making_logic": scorecard.get("money_making_logic")}
    priority = score + (12 if label == "已验证爆款" else 8 if label == "潜力爆发品" else 0)
    priority += 8 if growth >= 35 else 4 if growth >= 15 else 0
    priority += 6 if 14 <= active_days <= 120 else 0
    priority += 6 if price_band in {"healthy_aov", "high_ticket"} else 0
    priority += 4 if shipping else 0
    priority += 8 if "多素材矩阵" in reason_blob else 4 if "素材变体" in reason_blob else 0
    priority += 6 if "跨版位" in reason_blob or "FB+IG" in reason_blob else 0
    priority += 7 if "UGC" in reason_blob or "证明型创意" in reason_blob else 0
    priority += 6 if "痛点开场" in reason_blob else 0
    priority += 6 if "Offer价值堆叠" in reason_blob else 0
    priority += 5 if "落地页信任" in reason_blob else 0
    priority += 5 if "趋势词" in reason_blob else 0
    priority += 6 if layer_score("margin") >= 75 else 0
    priority += 6 if layer_score("creative_scalability") >= 75 else 0
    priority += 5 if layer_score("urgency") >= 75 else 0
    priority += 4 if layer_score("market_size") >= 75 else 0
    priority -= 25 if layer_score("compliance_risk") < 45 else 0
    priority -= 15 if layer_score("supply_chain_logistics") < 45 else 0
    priority -= 8 if layer_score("saturation") < 45 else 0
    priority -= 4 if layer_score("moat") < 35 else 0
    priority -= 18 if "2026风险" in reason_blob else 0
    priority = max(0, min(100, int(priority)))
    if priority >= 88:
        decision, budget, next_step = "立即跟品", "$80-$150/day", "直接拆落地页结构、素材钩子和 offer, 建 3-5 条变体广告测试"
    elif priority >= 74:
        decision, budget, next_step = "小预算跟测", "$30-$60/day", "先做低成本 MVP/落地页烟雾测试, 通过再放量"
    else:
        decision, budget, next_step = "观察候选", "$10-$30/day", "等待新增广告量、素材重复度或搜索趋势二次确认"
    why_parts = [f"{label or '未分层'} · {score}分", f"主理人分{scorecard.get('overall', 0)}", f"{active_days}天在投", f"增长{growth:.0f}%", f"{price_band}/{volume}"]
    why_parts.extend(str(x) for x in reasons[:2])
    return {"decision": decision, "priority": priority, "budget": budget, "confidence": priority, "why": " / ".join(why_parts), "next_step": next_step, "operator_verdict": scorecard.get("operator_verdict"), "money_making_logic": scorecard.get("money_making_logic")}


def infer_legacy_2026_hits(item: dict[str, Any]) -> list[str]:
    existing = {str(x) for x in item.get("reasons") or []}
    copy = str(item.get("ad_copy") or "").lower()
    raw = item.get("raw_json") or {}
    if not isinstance(raw, dict):
        raw = {}
    blob = f"{copy} {json.dumps(raw, ensure_ascii=False).lower()[:40_000]}"
    hits: list[str] = []

    def add(label: str) -> None:
        if label not in existing and label not in hits:
            hits.append(label)

    active_days = int(item.get("active_days") or 0)
    if active_days >= 14:
        add("2026打法:广告存活≥14天")
    cards_count = 0
    try:
        cards_count = int(raw.get("cards_count") or 0)
    except Exception:
        cards_count = 0
    cards = raw.get("cards")
    if not cards_count and isinstance(cards, list):
        cards_count = len(cards)
    if cards_count >= 5:
        add("2026打法:多素材矩阵≥5")
    elif cards_count >= 3:
        add("2026打法:素材变体≥3")
    platforms = json.dumps(raw.get("publisher_platforms") or raw.get("platforms") or "", ensure_ascii=False).upper()
    platform_count = sum(1 for p in ("FACEBOOK", "INSTAGRAM", "MESSENGER", "AUDIENCE_NETWORK", "THREADS") if p in platforms)
    if platform_count >= 3:
        add("2026打法:跨版位扩量")
    elif platform_count >= 2:
        add("2026打法:FB+IG双版位")
    cta = str(raw.get("cta_type") or raw.get("cta_text") or "").lower()
    if any(x in cta for x in ("shop", "buy", "offer", "learn_more", "learn more")):
        add("2026打法:强CTA")
    if re.search(r"\b(ugc|real customer|customer|review|before and after|before/after|testimonial|doctor|dentist|clinically|study|proven|demo|result)\b", blob):
        add("2026打法:UGC/证明型创意")
    if re.search(r"\b(struggle|tired of|sick of|problem|pain|bad breath|anxiety|sleep|relief|fix|without|alternative|hate|wish)\b", blob):
        add("2026打法:痛点开场")
    if re.search(r"\b(free shipping|bundle|save|discount|% off|limited|guarantee|warranty|risk-free|money back|subscribe)\b", blob):
        add("2026打法:Offer价值堆叠")
    if item.get("price_band") in {"healthy_aov", "high_ticket"} and item.get("volume_class") != "bulky" and int(item.get("shipping_advantage") or 0):
        add("2026打法:利润/物流结构适合冷流量")
    if any(t in blob for t in ("red light", "probiotic", "cortisol", "vagus", "somatic", "lymph", "creatine", "menopause", "perimenopause", "non toxic", "grounding", "longevity")):
        add("2026打法:健康/自我优化趋势词")
    if re.search(r"\b(cure|guaranteed results|miracle|fda approved|weight loss injection|ozempic|copyright|replica)\b", blob):
        add("2026风险:合规/侵权/夸大承诺")
    return hits


def build_ai_prompt_pack(payload: dict[str, Any]) -> dict[str, str]:
    payload_ref = json.dumps(payload, ensure_ascii=False, indent=2)[:60_000]
    profit_fields = (
        "必须给出利润模型：target_selling_price、compare_at_price、bundle_price、upsell_path、target_cogs、target_landed_cost_ceiling、gross_margin_pct、gross_profit_per_order、break_even_cpa、acceptable_cpa、scale_cpa、refund_allowance_pct、day1_budget_split、kill_rules。"
    )
    output_rule = "只输出 JSON，不要空话；所有建议必须能直接执行，不能写泛泛的提升品牌感/优化素材。"
    return {
        "master_operator": (
            "你是 V3 自动赚钱选品机器，同时是过亿 DTC 选品主理人、Meta Ads 投放负责人、UGC 素材导演、供应链负责人、合规审查负责人和老板。\n"
            f"产品数据：{payload_ref}\n"
            "目标不是判断热不热，而是判断这个品今天能不能用冷流量赚到钱、毛利能不能覆盖 CPA、素材能不能跑出胜率、供应链能不能承接、合规能不能过审。\n"
            f"{profit_fields}\n"
            "决策只能是：立即跟品 / 小预算跟测 / 观察候选 / 放弃不碰。必须按老板视角输出：final_decision、confidence、money_making_logic、profit_model、executive_scorecard、red_flags、green_flags、follow_method、target_customer、core_pain、winning_angle、offer_stack、creative_strategy、video_scripts、landing_page_structure、geo_strategy、supply_chain_checklist、compliance_safe_angle、budget_ladder、next_72h_action_plan、kill_criteria、ai_ingest_payload、prompt_for_future_learning。"
            f"{output_rule}"
        ),
        "product_decision": (
            f"请像过亿 DTC 选品主理人一样判断以下产品是否值得跟品。输入数据：{payload_ref}\n"
            f"{profit_fields}\n"
            "输出 JSON：final_decision、confidence、why_money、why_not、profit_model、best_follow_method、target_customer、core_pain、winning_angle、offer_stack、geo、first_test_budget、break_even_cpa、acceptable_cpa、kill_criteria、next_72h_plan。必须说明如果不能做到 60%+ 毛利或 CPA 没空间，应该如何改价格、bundle、upsell 或直接放弃。"
        ),
        "creative_director": (
            f"你是 Meta Ads / TikTok Ads 爆款素材导演。产品数据：{payload_ref}\n"
            "生成首轮 direct response 素材方案：8 个 hooks、3 条 15s UGC 脚本、1 条 30s 教育脚本、1 条 offer 强转化脚本、proof assets、shots_to_record、editing_notes、compliance_safe_rewrites。每条素材都要标注对应人群、痛点、卖点、首屏大字、CTA、预期指标、失败后如何重剪。前 2 秒必须强 hook，避免医疗/减肥/FDA/夸张承诺。输出 JSON。"
        ),
        "video_production": (
            f"你是短视频广告制作总监。产品信息：{payload_ref}\n"
            "输出拍摄清单、道具清单、场景清单、镜头顺序、口播文案、屏幕大字、B-roll、普通用户/专业解释/对比测评 3 个人设版本、15s/30s/45s 三种节奏、合规安全版本。每个脚本都要给出 hook、problem、mechanism、demo、proof、offer、CTA，并标注能支持的售价或 bundle。输出 JSON。"
        ),
        "landing_page": (
            f"你是 DTC 落地页转化率优化专家。产品数据：{payload_ref}\n"
            "生成冷流量转化落地页结构：hero、痛点放大、机制解释、产品演示、用户证明、对比表、FAQ、Offer、trust elements、risk reversal、bundle offer、upsell/cross-sell、checkout notes、compliance notes。必须给出页面如何支撑更高 AOV、如何解释价格、如何减少退款、如何提高 ATC/CVR。输出 JSON。"
        ),
        "geo_strategy": (
            f"你是国际 DTC 投放负责人。产品数据：{payload_ref}\n"
            "判断首轮测试 GEO，输出 primary_geo、secondary_geo、avoid_geo、geo_reasoning、budget_split、localization_notes、shipping_warning、compliance_warning、expected_cpm、expected_cpa_range、geo_profit_fit。预算不要平均分，优先能支撑售价、物流和合规的国家。输出 JSON。"
        ),
        "supply_chain": (
            f"你是 DTC 供应链负责人。产品数据：{payload_ref}\n"
            "判断是否适合冷流量后的快速履约。输出 supply_chain_verdict、target_cogs、target_landed_cost、landed_cost_ceiling、target_gross_margin、gross_profit_per_order、pricing_floor、supplier_checklist、shipping_risks、return_risks、quality_control_points、packaging_requirements、questions_for_supplier、backup_supplier_plan、go_no_go_rule。若毛利低于 60%，必须给出提价、bundle、轻量化、换供应商或放弃。输出 JSON。"
        ),
        "compliance": (
            f"你是 Meta Ads 合规审查负责人。产品数据：{payload_ref}\n"
            "检查医疗功效、减肥承诺、before/after、FDA/clinically/doctor claims、恐吓式文案、个人属性暗示、仿牌/IP/trademark。输出 compliance_verdict、risky_claims、safe_rewrites、banned_angles、allowed_angles、creative_dos、creative_donts、landing_page_claims_to_remove、final_ad_safe_angle。必须把高转化但危险的 claim 改写成可投放、安全但仍有销售力的表达。输出 JSON。"
        ),
        "learning_review": (
            f"你是 V3 自动选品系统的复盘学习模块。以下是初始产品判断与 AI 入库数据：{payload_ref}\n"
            "等我补充 ad_results、spend、ctr、cpc、cvr、cpa、aov、gross_margin、refund、final_result 后，请判断初始判断哪里对/错，哪些信号应加权/降权，失败或成功对未来选品的经验，应该写入 V3 哪些规则。输出 learning_summary、profit_gap_analysis、signals_to_increase_weight、signals_to_decrease_weight、new_red_flags、new_green_flags、category_learning、creative_learning、geo_learning、offer_learning、pricing_learning、rule_update_suggestion、future_prompt_memory。输出 JSON。"
        ),
    }


def build_follow_playbook(item: dict[str, Any]) -> dict[str, Any]:
    reasons = [str(x) for x in item.get("reasons") or []]
    blob = " ".join(reasons).lower()
    scorecard = item.get("executive_scorecard") or build_executive_scorecard(item)
    layers = scorecard.get("layers") or {}
    score = int(item.get("adjusted_score") or item.get("quality_score") or 0)
    priority = int((item.get("auto_decision") or {}).get("priority") or score)
    domain = str(item.get("landing_domain") or "")
    price_band = str(item.get("price_band") or "unknown")
    volume = str(item.get("volume_class") or "unknown")
    dedupe_key = _follow_pick_dedupe_key(item)
    layer_score = lambda name: int((layers.get(name) or {}).get("score") or 0)
    angle = "痛点解决 + 证明型演示" if "痛点" in blob or "ugc" in blob else "趋势教育 + 产品机制解释"
    if layer_score("compliance_risk") < 60:
        angle = "合规安全角度 + 机制教育，不碰夸张功效承诺"
    offer = "Bundle + free shipping + 30-day guarantee" if "offer" in blob else "首单折扣 + 免邮 + 无风险退换"
    geo = ["US", "CA", "AU", "UK"] if price_band in {"healthy_aov", "high_ticket"} else ["US", "CA"]
    if volume == "bulky":
        geo = ["US"]
    creative = [
        "拆 3 条 hook：痛点前 2 秒、结果对比、真人测评",
        "做 2 套 proof：before/after 或 demo + review 截图",
        "复刻竞品落地页结构但重写卖点和视觉，不复制素材",
    ]
    video = [
        "15s UGC：0-2s 痛点钩子，3-8s 产品演示，9-12s 结果/证据，13-15s offer CTA",
        "30s 教育型：问题机制 → 常见误区 → 产品方案 → 用户反馈 → 限时 offer",
        "拍摄素材包：手持开箱、近景使用、前后对比、包装/尺寸、真实评价口播",
    ]
    if "跨版位" in blob or "fb+ig" in blob:
        creative.append("同时裁 9:16 Reels、4:5 Feed、1:1 retargeting 版本")
    if "多素材" in blob or "素材变体" in blob or layer_score("creative_scalability") >= 75:
        creative.append("首轮至少 5 条素材变体，保持同 offer 测不同 hook")
    if layer_score("saturation") < 55:
        creative.append("不要照抄竞品主角度，改人群、机制或 offer 切入")
    next_72h = [
        {"window": "0-6h", "owner": "选品/供应链", "action": "确认同款供应链、到岸成本、毛利和发货时效", "output": "2 家供应商报价 + 目标毛利表"},
        {"window": "6-24h", "owner": "素材/建站", "action": "拆落地页结构，搭 MVP 页面，产出 5 条 hook 和 2 套 proof 素材", "output": "MVP LP + 5 条短视频脚本"},
        {"window": "24-48h", "owner": "投放", "action": "按 GEO 优先级小预算启动 Advantage+ / broad 测试", "output": "首轮 CTR/CPC/ATC/CPA 数据"},
        {"window": "48-72h", "owner": "复盘", "action": "砍掉弱 hook，保留胜出角度，决定放量/重剪/放弃", "output": "放量或 kill 决策"},
    ]
    if layer_score("compliance_risk") < 60:
        next_72h[1]["action"] = "先重写 claims，改成体验/机制/场景表达，再产出素材"
    if layer_score("margin") < 55:
        next_72h[0]["action"] = "先算 landed cost 和退款空间，毛利不够则不进入投放"
    budget_ladder = [
        {"stage": "Smoke", "budget": "$20-$50/day", "duration": "24h", "pass": "CTR ≥1.2% 或 ATC ≥2%，评论无重大合规质疑"},
        {"stage": "Validation", "budget": "$80-$150/day", "duration": "48h", "pass": "CPA ≤ 目标毛利 1.2 倍，至少 1 条素材明显胜出"},
        {"stage": "Scale", "budget": "$300+/day", "duration": "信号确认后", "pass": "连续 2 天 ROAS/CPA 稳定且供应链可承接"},
    ]
    kill_criteria = [
        "24h CTR < 1.0% 且无加购：停素材",
        "CPA 超目标毛利 1.5 倍：停 ad set",
        "评论集中质疑功效/安全：暂停该角度并改 claims",
        "供应链报价后毛利 < 60%：不进入放量",
    ]
    hooks = ["你是不是也遇到这个痛点？", "真实用户怎么解决这个问题", "别再用旧方法，先看这个机制"]
    if layer_score("urgency") >= 75:
        hooks.insert(0, "这个趋势正在放量，先测再说")
    ai_payload = {
        "ad_id": item.get("ad_id"),
        "domain": domain,
        "landing_url": item.get("landing_url"),
        "priority": priority,
        "decision": (item.get("auto_decision") or {}).get("decision"),
        "executive_scorecard": scorecard,
        "geo": geo,
        "offer": offer,
        "angle": angle,
        "reasons": reasons[:12],
    }
    prompt_pack = build_ai_prompt_pack(ai_payload)
    ai_prompt = prompt_pack["master_operator"]
    return {
        "product_follow": "立即拆品跟进" if priority >= 88 else "小预算烟雾测试" if priority >= 74 else "观察等二次信号",
        "angle": angle,
        "offer": offer,
        "creative_brief": creative,
        "video_brief": video,
        "geo": geo,
        "next_72h_action_plan": next_72h,
        "budget_ladder": budget_ladder,
        "kill_criteria": kill_criteria,
        "creative_video_geo": {
            "hooks": hooks,
            "ugc_scripts": video,
            "proof_assets": ["真实评价截图", "使用前后对比", "产品近景演示", "开箱和尺寸展示"],
            "landing_page_blocks": ["痛点 hero", "机制解释", "UGC proof", "对比表", "FAQ/Shipping/Returns", "bundle offer"],
            "geo_priority": geo,
        },
        "ai_ingest": {
            "status": "ready_for_chatgpt_ai_db" if priority >= 74 else "watchlist",
            "entity_type": "follow_pick",
            "dedupe_key": dedupe_key,
            "tags": [x.replace("2026打法:", "") for x in reasons if x.startswith("2026打法:")][:8],
            "payload": ai_payload,
            "prompt": ai_prompt,
            "prompt_pack": prompt_pack,
        },
    }


def build_follow_portfolio_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    immediate = sum(1 for x in items if (x.get("auto_decision") or {}).get("decision") == "立即跟品")
    test = sum(1 for x in items if (x.get("auto_decision") or {}).get("decision") == "小预算跟测")
    watch = sum(1 for x in items if (x.get("auto_decision") or {}).get("decision") == "观察候选")
    low, high = immediate * 80 + test * 30, immediate * 150 + test * 60
    risk_mix = {"compliance": 0, "logistics": 0, "saturation": 0, "low_margin": 0}
    geo_mix: dict[str, int] = {}
    category_mix = {"health_self_optimization": 0, "home_improvement": 0, "beauty": 0, "unknown": 0}
    top_actions = []
    for item in items:
        sc = item.get("executive_scorecard") or {}
        layers = sc.get("layers") or {}
        if int((layers.get("compliance_risk") or {}).get("score") or 100) < 60:
            risk_mix["compliance"] += 1
        if int((layers.get("supply_chain_logistics") or {}).get("score") or 100) < 60:
            risk_mix["logistics"] += 1
        if int((layers.get("saturation") or {}).get("score") or 100) < 60:
            risk_mix["saturation"] += 1
        if item.get("price_band") == "low_margin":
            risk_mix["low_margin"] += 1
        for geo in ((item.get("follow_playbook") or {}).get("geo") or []):
            geo_mix[str(geo)] = geo_mix.get(str(geo), 0) + 1
        blob = " ".join(str(x) for x in item.get("reasons") or []).lower()
        if any(x in blob for x in ("red light", "cortisol", "vagus", "somatic", "longevity", "menopause", "probiotic")):
            category_mix["health_self_optimization"] += 1
        elif any(x in blob for x in ("skin", "beauty", "hair", "skincare")):
            category_mix["beauty"] += 1
        elif any(x in blob for x in ("home", "kitchen", "clean", "storage")):
            category_mix["home_improvement"] += 1
        else:
            category_mix["unknown"] += 1
    for item in items[:3]:
        d = item.get("auto_decision") or {}
        top_actions.append({"domain": item.get("landing_domain"), "decision": d.get("decision"), "priority": d.get("priority"), "action": d.get("next_step")})
    verdict = "今天暂无强跟品，先继续跑 V3 Auto 扩词" if not items else f"今天可上 {immediate} 个，重点押 {min(immediate, 1)} 个，其余 {test} 个小预算验证"
    return {
        "immediate": immediate,
        "test": test,
        "watch": watch,
        "total_recommended_budget_day1": f"${low}-${high}",
        "portfolio_verdict": verdict,
        "risk_mix": risk_mix,
        "category_mix": category_mix,
        "geo_mix": geo_mix,
        "operator_focus": [
            "优先测 compact + healthy/high AOV + UGC proof 的品",
            "跳过 marketplace、大件、低客单和医疗承诺强的品",
            "今日预算不要平均分，优先 Top 3 的胜出素材角度",
        ],
        "top_3_actions": top_actions,
    }


def _follow_pick_dedupe_key(item: dict[str, Any]) -> str:
    landing_url = str(item.get("landing_url") or "").strip().lower()
    parsed = urlparse(landing_url)
    path = parsed.path.rstrip("/") or "/"
    if parsed.netloc:
        return f"url:{parsed.netloc}{path}"
    domain = str(item.get("landing_domain") or "").strip().lower()
    copy = re.sub(r"\s+", " ", str(item.get("ad_copy") or "").strip().lower())[:160]
    return f"domain:{domain}:copy:{copy}"


def list_auto_follow_picks(*, limit: int = 50, min_priority: int = 74) -> dict[str, Any]:
    items = list_v3_picks(min_score=45, limit=max(limit * 8, 240))
    out = []
    for item in items:
        if hard_reject_reason(str(item.get("landing_url") or item.get("landing_domain") or "")):
            continue
        inferred = infer_legacy_2026_hits(item)
        if inferred:
            item["reasons"] = list(item.get("reasons") or []) + inferred
        item["executive_scorecard"] = build_executive_scorecard(item)
        item["auto_decision"] = auto_follow_decision(item)
        if item["auto_decision"]["priority"] >= min_priority and item["auto_decision"]["decision"] != "放弃":
            out.append(item)
    out.sort(key=lambda x: (x["auto_decision"]["priority"], x.get("adjusted_score") or 0, x.get("impressions_growth_pct") or 0), reverse=True)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in out:
        key = _follow_pick_dedupe_key(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    for item in deduped:
        item["follow_playbook"] = build_follow_playbook(item)
    summary = build_follow_portfolio_summary(deduped)
    for item in deduped:
        item.pop("raw_json", None)
        if isinstance(item.get("ad_copy"), str) and len(item["ad_copy"]) > 420:
            item["ad_copy"] = item["ad_copy"][:420] + "..."
    return {"ok": True, "items": deduped, "count": len(deduped), "summary": summary}


class V3CoreRunRequest(BaseModel):
    limit: int = Field(1200, ge=1, le=20000)
    concurrency: int = Field(DEFAULT_CONCURRENCY, ge=4, le=300)
    batch_size: int = Field(DEFAULT_BATCH_SIZE, ge=50, le=5000)
    timeout: int = Field(DEFAULT_TIMEOUT_SEC, ge=4, le=45)
    domain: str | None = Field(None, max_length=255)

    @field_validator("domain")
    @classmethod
    def _domain(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else None


class V3CoreAdsRequest(BaseModel):
    ads: list[dict[str, Any]] = Field(..., min_length=1, max_length=20000)
    concurrency: int = Field(DEFAULT_CONCURRENCY, ge=4, le=300)
    batch_size: int = Field(DEFAULT_BATCH_SIZE, ge=50, le=5000)
    timeout: int = Field(DEFAULT_TIMEOUT_SEC, ge=4, le=45)


class V3ReviewRequest(BaseModel):
    action: Literal["approve", "watch", "skip"]
    note: str = Field("", max_length=1000)


def review_v3_pick(ad_id: str, action: str, note: str = "") -> dict[str, Any]:
    aid = (ad_id or "").strip()
    if not aid:
        raise ValueError("ad_id required")
    conn = _connect()
    try:
        exists = conn.execute("SELECT 1 FROM v3_dtc_ads WHERE ad_id = ?", (aid,)).fetchone()
        if not exists:
            raise KeyError(aid)
        conn.execute(
            "INSERT INTO v3_ad_reviews(ad_id, action, note) VALUES (?,?,?)",
            (aid, action, note.strip()),
        )
        conn.commit()
        return {"ok": True, "ad_id": aid, "action": action}
    finally:
        conn.close()


def v3_core_stats() -> dict[str, Any]:
    conn = _connect()
    try:
        labels = {
            row["status_label"]: row["n"]
            for row in conn.execute(
                "SELECT status_label, COUNT(*) AS n FROM v3_dtc_ads GROUP BY status_label"
            )
        }
        engines = [
            dict(row)
            for row in conn.execute(
                """SELECT shop_engine, COUNT(*) AS n, ROUND(AVG(quality_score), 1) AS avg_score
                   FROM v3_dtc_ads
                   GROUP BY shop_engine
                   ORDER BY n DESC"""
            )
        ]
        reject_top = [
            dict(row)
            for row in conn.execute(
                """SELECT reject_reason, COUNT(*) AS n
                   FROM v3_filter_rejections
                   GROUP BY reject_reason
                   ORDER BY n DESC
                   LIMIT 12"""
            )
        ]
        reviews = {
            row["action"]: row["n"]
            for row in conn.execute(
                "SELECT action, COUNT(*) AS n FROM v3_ad_reviews GROUP BY action"
            )
        }
        latest_run_row = conn.execute(
            "SELECT * FROM v3_fetch_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        latest_run = dict(latest_run_row) if latest_run_row else None
        total = conn.execute("SELECT COUNT(*) FROM v3_dtc_ads").fetchone()[0]
        high_score = conn.execute("SELECT COUNT(*) FROM v3_dtc_ads WHERE quality_score >= 75").fetchone()[0]
        pending = conn.execute(
            """SELECT COUNT(*)
               FROM v3_dtc_ads d
               WHERE NOT EXISTS (
                 SELECT 1 FROM v3_ad_reviews r WHERE r.ad_id = d.ad_id
               )"""
        ).fetchone()[0]
        return {
            "ok": True,
            "total_picks": total,
            "high_score": high_score,
            "pending_review": pending,
            "labels": labels,
            "engines": engines,
            "reject_top": reject_top,
            "reviews": reviews,
            "latest_run": latest_run,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
    finally:
        conn.close()


def build_v3_router() -> APIRouter:
    router = APIRouter(prefix="/api/v3/core", tags=["v3-selection-core"])

    @router.post("/run")
    async def run_from_archive(payload: V3CoreRunRequest):
        seeds = await asyncio.to_thread(load_recent_ad_archive, payload.limit, payload.domain)
        if not seeds:
            raise HTTPException(status_code=404, detail="no eligible ad_archive rows")
        engine = V3AsyncSelectionEngine(
            concurrency=payload.concurrency,
            batch_size=payload.batch_size,
            timeout_sec=payload.timeout,
        )
        return await engine.analyze(seeds)

    @router.post("/analyze")
    async def analyze_ads(payload: V3CoreAdsRequest):
        seeds: list[V3AdSeed] = []
        for ad in payload.ads:
            seed = extract_seed(ad)
            if seed:
                seeds.append(seed)
        if not seeds:
            raise HTTPException(status_code=400, detail="no valid ads with ad_id and landing_url")
        engine = V3AsyncSelectionEngine(
            concurrency=payload.concurrency,
            batch_size=payload.batch_size,
            timeout_sec=payload.timeout,
        )
        return await engine.analyze(seeds)

    @router.get("/picks")
    def picks(
        status_label: Literal["潜力爆发品", "已验证爆款", "观察池"] | None = None,
        engine: str | None = Query(None),
        min_score: int = Query(0, ge=0, le=100),
        limit: int = Query(100, ge=1, le=1000),
    ):
        return {"ok": True, "items": list_v3_picks(status_label=status_label, engine=engine, min_score=min_score, limit=limit)}

    @router.get("/follow-picks")
    def follow_picks(
        limit: int = Query(50, ge=1, le=200),
        min_priority: int = Query(74, ge=0, le=100),
    ):
        return list_auto_follow_picks(limit=limit, min_priority=min_priority)

    @router.get("/stats")
    def stats():
        return v3_core_stats()

    @router.post("/review/{ad_id}")
    def review(ad_id: str, payload: V3ReviewRequest):
        try:
            return review_v3_pick(ad_id, payload.action, payload.note)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"ad_id not found: {ad_id}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @router.get("/domain-check")
    async def domain_check(url: str = Query(..., min_length=4)):
        clean = normalize_url(url)
        if is_hard_rejected_domain(clean):
            return {
                "ok": False,
                "url": clean,
                "domain": _host(clean),
                "shop_engine": "",
                "reject_reason": "blocked_platform",
            }
        engine = V3AsyncSelectionEngine(concurrency=4)
        timeout = ClientTimeout(total=DEFAULT_TIMEOUT_SEC)
        async with aiohttp.ClientSession(timeout=timeout, headers={"user-agent": engine.user_agent}) as session:
            final_url, html, headers, err = await engine._fetch_html(session, clean)
        if err:
            return {"ok": False, "url": url, "error": err}
        ok, shop_engine, reject = is_strict_dtc(final_url, html, headers)
        return {"ok": ok, "url": final_url, "domain": _host(final_url), "shop_engine": shop_engine, "reject_reason": reject}

    return router


def register_v3_selection_core(app: FastAPI) -> None:
    ensure_v3_schema()
    app.include_router(build_v3_router())

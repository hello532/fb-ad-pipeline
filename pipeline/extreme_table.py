"""Extreme FB table field expansion + Reddit money-case matching.

Used by memory_scan.write_excel to push Excel toward operator-grade detail
without inventing ROAS/spend dollars when FB only exposes bounds.
"""
from __future__ import annotations
import os

import re
from collections import Counter, defaultdict
from typing import Any, Optional

# Lazy import paths resolved by caller sys.path (NEW/)

_OFFER_RE = re.compile(
    r"(bundle|bogo|buy\s*\d\s*get|guarantee|money[\s-]?back|free shipping|"
    r"\d{1,3}\s*%\s*off|limited time|flash sale|today only|risk[\s-]?free)",
    re.I,
)
_HOOK_RE = re.compile(
    r"(did you know|stop scrolling|nobody talks|doctors hate|before you|"
    r"if you('re| are) still|i was skeptical|watch this|pov:|story time)",
    re.I,
)
_PAIN_RE = re.compile(
    r"(pain|ache|sore|relief|sleep|snore|posture|knee|neck|back|sciatica|"
    r"plantar|anxiety|bloated|gut|hair|wrinkle)",
    re.I,
)


def _num_bound(obj: Any, which: str = "upper") -> int:
    if obj is None:
        return 0
    if isinstance(obj, dict):
        for k in ((which + "_bound"), which, "upper" if which == "upper" else "lower"):
            if k in obj and obj[k] not in (None, ""):
                try:
                    return int(float(obj[k]))
                except Exception:
                    pass
        return 0
    try:
        return int(float(obj))
    except Exception:
        return 0


def enrich_ad(ad: dict[str, Any]) -> dict[str, Any]:
    """Add derived operator fields onto a normalized ad dict."""
    out = dict(ad)
    platforms = ad.get("publisher_platforms") or ad.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [p.strip() for p in platforms.split(",") if p.strip()]
    if not isinstance(platforms, list):
        platforms = []
    platforms = [str(p) for p in platforms]

    spend = ad.get("spend") or {}
    imps = ad.get("impressions") or {}
    spend_lo = _num_bound(spend, "lower")
    spend_hi = _num_bound(spend, "upper")
    imp_lo = _num_bound(imps, "lower")
    imp_hi = _num_bound(imps, "upper")

    body = str(ad.get("body") or "")
    title = str(ad.get("title") or "")
    url = str(ad.get("link_url") or "")
    text = f"{title} {body} {url}"

    fmt = str(ad.get("display_format") or ad.get("format") or "")
    has_video = bool(ad.get("video_url") or ad.get("carousel_videos") or "VIDEO" in fmt.upper())
    has_image = bool(ad.get("image_url") or ad.get("carousel_images") or "IMAGE" in fmt.upper())
    collation = 0
    try:
        collation = int(ad.get("collation_count") or 1)
    except Exception:
        collation = 1

    offer = bool(_OFFER_RE.search(text))
    hook = bool(_HOOK_RE.search(text))
    pain = bool(_PAIN_RE.search(text))

    out.update({
        "platforms": platforms,
        "platform_str": ",".join(platforms[:6]),
        "spend_lower": spend_lo,
        "spend_upper": spend_hi,
        "imp_lower": imp_lo,
        "imp_upper": imp_hi,
        "display_format": fmt,
        "has_video": has_video,
        "has_image": has_image,
        "collation_count": max(1, collation),
        "page_id": str(ad.get("page_id") or ""),
        "ig_handle": str(ad.get("ig_handle") or ""),
        "ig_followers": int(ad.get("ig_followers") or 0) if str(ad.get("ig_followers") or "").isdigit() or isinstance(ad.get("ig_followers"), (int, float)) else 0,
        "image_url": str(ad.get("image_url") or ""),
        "video_url": str(ad.get("video_url") or ""),
        "snapshot_url": str(ad.get("ad_snapshot_url") or ad.get("snapshot_url") or ""),
        "link_caption": str(ad.get("link_caption") or "")[:120],
        "link_description": str(ad.get("link_description") or "")[:160],
        "currency": str(ad.get("currency") or ""),
        "offer_flag": offer,
        "hook_flag": hook,
        "pain_flag": pain,
        "media": ("video" if has_video else "") + ("+image" if has_image else ("image" if has_image and not has_video else "")) or "unknown",
    })
    try:
        out["ig_followers"] = int(ad.get("ig_followers") or 0)
    except Exception:
        out["ig_followers"] = 0
    return out


def aggregate_domain_stats(store_ads: dict[str, dict], aid_kws: dict[str, set[str]]) -> dict[str, dict[str, Any]]:
    """Rich per-domain aggregates for extreme competitor sheet."""
    from memory_scan import _clean_text, _clean_url, _to_iso_date, _active_days, _ad_fields

    domains: dict[str, dict[str, Any]] = {}
    for aid, ad0 in store_ads.items():
        base = ad0 if "link_url" in ad0 else _ad_fields(ad0)
        ad = enrich_ad(base)
        domain = (ad.get("domain") or "").lower().lstrip("www.")
        if not domain and ad.get("link_url"):
            from pipeline.domain import extract_domain_from_url
            domain = extract_domain_from_url(ad["link_url"])
        key = domain or f"page:{(ad.get('page_name') or 'unknown').lower()}"
        row = domains.setdefault(key, {
            "domain": domain,
            "page_names": set(),
            "page_ids": set(),
            "ads": 0,
            "active_ads": 0,
            "landings": set(),
            "keywords": set(),
            "ctas": Counter(),
            "formats": Counter(),
            "platforms": Counter(),
            "max_days": 0,
            "min_start": "",
            "max_end": "",
            "likes": 0,
            "ig_followers": 0,
            "spend_lower_sum": 0,
            "spend_upper_sum": 0,
            "imp_upper_sum": 0,
            "collation_max": 1,
            "video_ads": 0,
            "image_ads": 0,
            "offer_ads": 0,
            "hook_ads": 0,
            "pain_ads": 0,
            "sample_url": "",
            "sample_body": "",
            "snapshot_url": "",
            "start_dates": [],
            "hook_angles": Counter(),
            "offer_depth": 0,
            "product_hints": set(),
        })
        row["ads"] += 1
        if ad.get("is_active"):
            row["active_ads"] += 1
        if ad.get("page_name"):
            row["page_names"].add(ad["page_name"])
        if ad.get("page_id"):
            row["page_ids"].add(str(ad["page_id"]))
        if ad.get("link_url"):
            row["landings"].add(ad["link_url"])
            if not row["sample_url"]:
                row["sample_url"] = ad["link_url"]
        row["keywords"] |= aid_kws.get(aid, set())
        if ad.get("cta"):
            row["ctas"][ad["cta"]] += 1
        if ad.get("display_format"):
            row["formats"][ad["display_format"]] += 1
        for p in ad.get("platforms") or []:
            row["platforms"][p] += 1
        days = int(ad.get("active_days") or 0)
        row["max_days"] = max(row["max_days"], days)
        sd = ad.get("start_date") or ""
        if sd:
            row["start_dates"].append(sd)
            if not row["min_start"] or sd < row["min_start"]:
                row["min_start"] = sd
        row["likes"] = max(row["likes"], int(ad.get("page_like_count") or 0))
        row["ig_followers"] = max(row["ig_followers"], int(ad.get("ig_followers") or 0))
        row["spend_lower_sum"] += int(ad.get("spend_lower") or 0)
        row["spend_upper_sum"] += int(ad.get("spend_upper") or 0)
        row["imp_upper_sum"] += int(ad.get("imp_upper") or 0)
        row["collation_max"] = max(row["collation_max"], int(ad.get("collation_count") or 1))
        if ad.get("has_video"):
            row["video_ads"] += 1
        if ad.get("has_image"):
            row["image_ads"] += 1
        if ad.get("offer_flag"):
            row["offer_ads"] += 1
        if ad.get("hook_flag"):
            row["hook_ads"] += 1
        if ad.get("pain_flag"):
            row["pain_ads"] += 1
        if not row["sample_body"] and ad.get("body"):
            row["sample_body"] = str(ad["body"])[:160]
        if not row["snapshot_url"] and ad.get("snapshot_url"):
            row["snapshot_url"] = ad["snapshot_url"]
        # round-2 enrichment
        row["hook_angles"][hook_angle(str(ad.get("body") or ""))] += 1
        if not row["offer_depth"]:
            dep, _l = offer_depth_grade(str(ad.get("body") or "") + " " + str(ad.get("title") or ""))
            row["offer_depth"] = max(row["offer_depth"], dep)
        ph = product_name_hint(ad)
        if ph:
            row["product_hints"].add(ph)
    return domains


def domain_start_dates(st: dict[str, Any]) -> list[str]:
    return st.get("start_dates") or []

def local_seed_match(text: str) -> list[dict[str, Any]]:
    blob = text.lower()
    out = []
    for c in _LOCAL_SEED_CASES:
        if any(t in blob for t in c["terms"]):
            out.append({
                "id": c["id"], "money_angle": c["money_angle"],
                "boost": c["boost"], "route": "dtc",
                "source": "local-niche-seed", "structure_hint": c["structure"],
                "flags": ["local_seed"], "strong": c["boost"] >= 6,
            })
    return out


# L3: 本地 DTC 常青 niche 种子兜底（补 gshopping 案例没覆盖的品）
_LOCAL_SEED_CASES: list[dict[str, Any]] = [
    {"terms": ("leg pillow", "knee pillow", "side sleeper"), "id": "case_leg_pillow",
     "money_angle": "sleep pain grouping: 常青床品, 复购/加购", "boost": 6, "structure": "sleep"},
    {"terms": ("cooling pillow", "sleep pillow"), "id": "case_sleep_pillow",
     "money_angle": "睡眠健康 + 恒温钩子", "boost": 6, "structure": "sleep"},
    {"terms": ("posture corrector",), "id": "case_posture", 
     "money_angle": "上班族痛点 + WFH", "boost": 4, "structure": "pain_device"},
    {"terms": ("red light", "therapy light"), "id": "case_red_light",
     "money_angle": "biohack 高客单 + 订阅/复购", "boost": 6, "structure": "health_device"},
    {"terms": ("glp-1", "metabolic", "blood sugar"), "id": "case_metabolic",
     "money_angle": "2026 减重/血糖概念带", "boost": 5, "structure": "supplement"},
    {"terms": ("recovery", "compression"), "id": "case_recovery",
     "money_angle": "健身恢复 + 运动社群", "boost": 5, "structure": "pain_device"},
]


def ad_quality_flag(ad: dict[str, Any]) -> str:
    """L8: 广告质量档——强=视频+单品+Offer / 中=单卖货要素 / 弱=无卖货要素。"""
    s = 0
    if ad.get("has_video"):
        s += 2
    if re.search(r"/products/[^/?#]+", str(ad.get("link_url") or "")):
        s += 2
    if ad.get("offer_flag"):
        s += 2
    if ad.get("category") is None and ad.get("page_like_count"):
        s += 1
    if str(ad.get("cta") or "") in _SELLING_CTA:
        s += 1
    if s >= 6:
        return "A-强(视频+单品+Offer)"
    if s >= 4:
        return "B-中"
    if s >= 2:
        return "C-弱"
    return "D-无卖货要素"


_SELLING_CTA = {"SHOP_NOW", "BUY_NOW", "ORDER_NOW", "ADD_TO_CART", "GET_OFFER", "VIEW_PRODUCT"}
def match_money_cases(text: str, domain: str = "") -> list[dict[str, Any]]:
    """Match gshopping reddit money cases + local niche seeds."""
    hits: list[dict[str, Any]] = []
    blob = f"{text} {domain}".lower()
    # local seed fallback (L3)
    try:
        from pipeline.extreme_table import local_seed_match

        for c in local_seed_match(blob):
            if not any(h.get("id") == c["id"] for h in hits):
                hits.append(c)
    except Exception:
        pass

    try:
        import sys
        from pathlib import Path
        gsp = Path(os.getenv("GSHOPPING_SCRIPTS",
        str(Path(__file__).resolve().parents[1] / "scripts")))
        if str(gsp) not in sys.path:
            sys.path.insert(0, str(gsp))
        from reddit_money_cases import REDDIT_MONEY_CASES  # type: ignore

        for case in REDDIT_MONEY_CASES:
            pats = case.get("patterns") or []
            ok = False
            for pat in pats:
                try:
                    if re.search(pat, blob, re.I):
                        ok = True
                        break
                except re.error:
                    if pat.lower() in blob:
                        ok = True
                        break
            if not ok:
                for term in case.get("seed_terms") or []:
                    if term.lower() in blob:
                        ok = True
                        break
            if ok:
                hits.append({
                    "id": case.get("id"),
                    "money_angle": case.get("money_angle"),
                    "source": case.get("source"),
                    "boost": int(case.get("score_boost") or 0),
                    "route": case.get("route"),
                    "structure_hint": case.get("structure_hint"),
                    "flags": case.get("flags") or [],
                    "strong": bool(case.get("strong")),
                })
    except Exception:
        pass
    hits.sort(key=lambda h: (-int(h.get("boost") or 0), h.get("id") or ""))
    return hits


def money_case_boost(hits: list[dict[str, Any]]) -> tuple[int, str, str]:
    if not hits:
        return 0, "", ""
    boost = min(12, sum(int(h.get("boost") or 0) for h in hits[:3]))
    ids = ",".join(h.get("id") or "" for h in hits[:4])
    angles = " | ".join(str(h.get("money_angle") or "") for h in hits[:3] if h.get("money_angle"))
    return boost, ids, angles


def creative_density_label(ads: int) -> str:
    if ads >= 60:
        return "饱和投放60+"
    if ads >= 20:
        return "重仓20+"
    if ads >= 10:
        return "密创意10+"
    if ads >= 5:
        return "多创意5+"
    if ads >= 3:
        return "测试矩阵3+"
    return "单薄1-2"


def longevity_label(days: int) -> str:
    if days >= 180:
        return "长青180d+"
    if days >= 90:
        return "长寿90d+"
    if days >= 30:
        return "验证期30d+"
    if days >= 14:
        return "两周观察"
    if days >= 7:
        return "一周新"
    return "极新<7d"


# ═══════════════════════════════════════════════════════════════════════════
# Round-2: L1-L9 optimization helpers
# ═══════════════════════════════════════════════════════════════════════════

# L1: product name from caption/title (fallback chain)
def product_name_hint(ad: dict[str, Any]) -> str:
    cap = str(ad.get("link_caption") or "").strip()
    if cap:
        # drop shop name prefix "BrandName - " / "Buy X -"
        cap = re.sub(r"^[^|•\-—]{2,40}\s*[|\-—•]\s*", "", cap).strip()
        if 2 < len(cap) <= 80:
            return cap
    t = str(ad.get("title") or "").strip()
    if t and len(t) <= 80:
        return t
    return ""


# L2: offer depth grading — count distinct offer signals
_OFFER_DEPTH_PATTERNS = [
    ("free shipping", r"free\s+shipping"),
    ("%off", r"\d{1,3}\s*%\s*off"),
    ("BOGO", r"\bbogo\b|buy\s*\d\s*get\s*\d"),
    ("bundle", r"\bbundle\b"),
    ("guarantee", r"money[\s-]?back|guarantee|risk[\s-]?free|\d+\s*days?\s+(?:guarantee|return)"),
    ("urgency", r"limited\s*time|today\s*only|flash\s*sale|while\s+supplies"),
    ("subscribe", r"subscribe|save\s+\d+\s*%"),
]


def offer_depth_grade(text: str) -> tuple[int, str]:
    blob = (text or "").lower()
    found: list[str] = []
    for name, pat in _OFFER_DEPTH_PATTERNS:
        if re.search(pat, blob, re.I):
            found.append(name)
    n = len(found)
    if n >= 5:
        return n, f"强offer({n}类: {'/'.join(found[:5])})"
    if n >= 3:
        return n, f"中offer({n}类: {'/'.join(found[:4])})"
    if n >= 1:
        return n, f"弱offer({n}类: {'/'.join(found[:3])})"
    return 0, "无offer结构"


# L3: copycat cluster — same path_key across different domains
def copycat_clusters(prod_rows: list[list[Any]]) -> dict[str, dict[str, Any]]:
    """prod_rows: header + data rows (列: 排名,产品/落地,域名,品牌,...落地页).
    Returns {path_key: {domains, count, sample_url}}."""
    clusters: dict[str, dict[str, Any]] = {}
    if not prod_rows:
        return clusters
    try:
        path_i = prod_rows[0].index("产品/落地")
        dom_i = prod_rows[0].index("域名")
        url_i = prod_rows[0].index("落地页")
    except ValueError:
        return clusters
    for row in prod_rows[1:]:
        path = str(row[path_i] if path_i < len(row) else "").strip()
        if not path or path.startswith("page:"):
            continue
        key = re.sub(r"[/\s_-]+", "-", path.lower())[:60]
        cl = clusters.setdefault(key, {"domains": set(), "count": 0, "sample_url": ""})
        cl["domains"].add(str(row[dom_i] if dom_i < len(row) else ""))
        cl["count"] += 1
        u = str(row[url_i] if url_i < len(row) else "")
        if u and not cl["sample_url"]:
            cl["sample_url"] = u
    return {k: v for k, v in clusters.items() if len(v["domains"]) >= 2}


# L4: fresh page risk — young page + few ads + low likes
def fresh_page_risk(likes: int, max_days: int, ads: int) -> str:
    if likes < 500 and max_days < 21 and ads < 5:
        return "⚠️新页风险(粉<500/投放<21d/创意<5)"
    if likes < 2000 and max_days < 45 and ads < 3:
        return "新页观察(粉少/测试期)"
    return ""


# L5: spend confidence — based on bound presence in stats
def spend_confidence(st: dict[str, Any]) -> str:
    lo = int(st.get("spend_lower_sum") or 0)
    hi = int(st.get("spend_upper_sum") or 0)
    n = int(st.get("ads") or 0)
    if n == 0:
        return "未知"
    if lo and hi:
        return f"高置信${lo//1000}k-${hi//1000}k"
    if lo:
        return f"中置信≥${lo//1000}k"
    return "无spend数据(未披露)"


# L6: freshness wave — median start within 14d of scan
def freshness_wave(st: dict[str, Any]) -> str:
    starts = st.get("start_dates") or []
    if not starts:
        return ""
    from datetime import datetime, timedelta
    try:
        parsed = sorted(datetime.strptime(s[:10], "%Y-%m-%d") for s in starts if s)
        if not parsed:
            return ""
        median = parsed[len(parsed) // 2]
        age = (datetime.utcnow() - median).days
        if age <= 14:
            return "🌊新潮(近14天起量)"
        if age <= 45:
            return "上升期(近45天)"
        if age <= 120:
            return "平台期(近4个月)"
        return "老品(>4个月)"
    except Exception:
        return ""


# L7: hook angle classification
_HOOK_ANGLES = [
    ("pain/痛点", r"pain|hurt|ache|sore|suffer|relief|struggl"),
    ("problem/solution", r"problem|fix|solve|did you know|stop (?:scrolling|suffering)"),
    ("social proof", r"\d[\d,.]*\+?\s*(?:reviews|customers|sold|people)|as seen|trusted|rated"),
    ("urgency/scarcity", r"limited|today only|last chance|while supplies|sale ends"),
    ("price/offer", r"\$\d+|free shipping|off\b|discount|save"),
    ("curiosity", r"nobody|don't|secret|revealed|most people|nobody talks"),
    ("how-to/educate", r"how to|guide|steps|3 (?:ways|reasons)|tips"),
    ("identity/lifestyle", r"as a (?:mom|dad|runner|dog owner)|for (?:men|women|seniors|kids)"),
]


def hook_angle(body: str) -> str:
    blob = (body or "").lower()[:600]
    if not blob:
        return ""
    for label, pat in _HOOK_ANGLES:
        if re.search(pat, blob, re.I):
            return label
    return "generic"


# L9: reddit thread mapping for a money-case id
def reddit_thread_for_case(case_id: str) -> str:
    try:
        from pipeline.reddit_fb_rules import load_catalog
        cat = load_catalog()
        for p in cat.get("posts") or []:
            blob = f"{p.get('title')} {p.get('selftext')}".lower()
            if case_id.replace("case_", "").replace("rd_", "") in blob or any(
                w in blob for w in case_id.split("_")[-2:]
            ):
                return str(p.get("url") or "")
    except Exception:
        pass
    return ""


def offer_depth_for_stats(st: dict[str, Any]) -> tuple[int, str]:
    """Domain-level offer depth from sample body."""
    return offer_depth_grade(str(st.get("sample_body") or ""))

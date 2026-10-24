"""domain.py — extracted from fb-scan-pipeline-v7.py:118-238"""
import re

from core.logger import get_logger
log = get_logger(__name__)

# ── 品牌→域名映射 ──────────────────────────────────────────────────────────────
# ── 品牌→域名映射 ──────────────────────────────────────────────────────────────
# The mapping is data, not code. Ship it as a JSON file the user owns:
#
#   data/known_domains.json   {"Brand Name": "brand.com", ...}
#
# Override the path with KNOWN_DOMAINS_FILE. Missing file = empty map, and the
# code falls back to the heuristic resolver below. This keeps each install's
# competitive intelligence out of the source tree.
KNOWN_DOMAINS: dict[str, str] = {}


def _load_known_domains() -> dict[str, str]:
    """Load the brand→domain seed map from disk, if present."""
    import json
    import os

    path = os.getenv("KNOWN_DOMAINS_FILE") or ""
    candidates = []
    if path:
        candidates.append(path)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "..", "data", "known_domains.json"))
    candidates.append(os.path.join(here, "known_domains.json"))

    for c in candidates:
        try:
            if c and os.path.isfile(c):
                with open(c, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    log.info("known_domains: loaded %d entries from %s", len(data), c)
                    return {str(k): str(v) for k, v in data.items()}
        except Exception as exc:  # noqa: BLE001
            log.warning("known_domains: failed to load %s (%s)", c, exc)
    log.info("known_domains: no seed file found; using heuristic resolver only")
    return {}


KNOWN_DOMAINS.update(_load_known_domains())

# ── 工具函数 ───────────────────────────────────────────────────────────────────

def resolve_domain(brand_name: str) -> str:
    if not brand_name:
        return ''
    brand_name = brand_name.strip()
    if brand_name in KNOWN_DOMAINS:
        return KNOWN_DOMAINS[brand_name]
    brand_lower = brand_name.lower()
    for known, domain in KNOWN_DOMAINS.items():
        if known.lower() in brand_lower or brand_lower in known.lower():
            return domain
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', brand_name).lower().replace(' ', '')
    return f"{clean}.com" if clean else ''

def extract_domain_from_url(url: str) -> str:
    if not url:
        return ''
    try:
        domain = re.sub(r'https?://', '', url)
        domain = domain.split('/')[0].split('?')[0]
        domain = re.sub(r'^www\.', '', domain).split(':')[0]
        return domain.strip()
    except Exception:
        return ''

# Common social/redirect domains that are NOT the advertiser's real domain
_NON_MERCHANT_DOMAINS = frozenset({
    'facebook.com', 'www.facebook.com', 'fb.com', 'fb.me',
    'instagram.com', 'www.instagram.com', 'instagr.am',
    'youtu.be', 'youtube.com', 'www.youtube.com',
    'twitter.com', 'x.com', 'tiktok.com', 'www.tiktok.com',
    'linkedin.com', 'www.linkedin.com',
    'l.facebook.com', 'l.instagram.com', 'lm.facebook.com',
    'm.facebook.com',
})

def extract_domain_from_caption(text: str) -> str:
    """Extract a merchant domain from link_caption or page_name.

    Many FB ads have link_url=instagram.com but the link_caption or page_name
    contains the real domain like "brand.com" or "brand.myshopify.com".
    """
    if not text:
        return ''
    # Look for domain-like patterns: word.tld
    m = re.search(r'(?<![/\w@])([a-zA-Z0-9][\w-]{0,61}[a-zA-Z0-9]\.[a-zA-Z]{2,}(?:\.[a-zA-Z]{2})?)', text)
    if m:
        dom = m.group(1).lower()
        if dom not in _NON_MERCHANT_DOMAINS and not dom.startswith('www.'):
            return dom
    return ''

def extract_domain_from_page_name(name: str) -> str:
    """Attempt to extract a domain from a Facebook page name.

    Page names often contain the domain like "BrandName - brand.com" or just "BrandName.com".
    """
    if not name:
        return ''
    # Check if page_name ends with or contains a domain
    return extract_domain_from_caption(name)

def is_social_or_redirect_domain(domain: str) -> bool:
    """Return True if domain is a social/redirect platform, not a merchant."""
    if not domain:
        return False
    d = domain.lower().strip()
    if d in _NON_MERCHANT_DOMAINS:
        return True
    # Subdomains of social platforms
    for nd in _NON_MERCHANT_DOMAINS:
        if d.endswith('.' + nd) or d == nd:
            return True
    return False

def canonical_domain(domain: str) -> str:
    """去掉 try./www./offers. 等前缀, 得到规范域名"""
    if not domain:
        return ''
    return re.sub(r'^(try\.|www\.|offers\.|shop\.|store\.)', '', domain.lower()).strip()

def validate_domain(domain: str) -> bool:
    if not domain:
        return False
    pattern = r'^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, domain))

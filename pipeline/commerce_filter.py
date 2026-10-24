"""pipeline/commerce_filter.py — 只放行独立站卖货广告 (v10).

v10 vs v9.2 核心变化:
- learn_more CTA + 独立域名 (非黑名单) = 默认放行 (v9.2 误杀 60%+ D2C 品牌)
- 新增 D2C 域名白名单 (d2c_domains.txt, 4500+ 已知独立站)
- UTM 参数检测: 含 utm_source/utm_medium = 付费导流, 放行
- page_like_count 信号: 1k-500k 粉丝 + ambiguous CTA = D2C brand

仍然 drop:
- 政治/就业/住房/信贷/宗教/选举类
- Lead-gen/Apps/Messaging/Media CTA
- Marketplace / FB 自家 / App Store / Link Aggregator 域名
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs


# ── archive_type 黑名单 ─────────────────────────────────────────────────────
# FB 在 ad_library 里给政治/就业/住房/信贷类广告打这些 archive_type. 一律不要.
ARCHIVE_TYPE_BLACKLIST = {
    'POLITICAL_AND_ISSUE',
    'POLITICAL',
    'ISSUES_ELECTIONS_POLITICS',
    'EMPLOYMENT',
    'HOUSING',
    'CREDIT',
    'RELIGIOUS',
    'SOCIAL_ISSUES',
}


# ── CTA 黑名单 ──────────────────────────────────────────────────────────────
# 这些 CTA 100% 不是独立站卖货.
CTA_BLACKLIST = {
    # Lead gen
    'SIGN_UP', 'APPLY_NOW', 'GET_QUOTE', 'CONTACT_US', 'REQUEST_TIME',
    'BOOK_NOW', 'BOOK_TRAVEL',
    # Apps
    'DOWNLOAD', 'INSTALL_MOBILE_APP', 'USE_APP', 'PLAY_GAME',
    'INSTALL_NOW', 'INSTALL_APP',
    # Messaging / contact
    'MESSAGE_PAGE', 'WHATSAPP_MESSAGE', 'SEND_MESSAGE', 'CALL_NOW', 'CALL',
    # Brand awareness
    'LIKE_PAGE', 'FOLLOW_PAGE', 'FOLLOW_NEWS', 'FOLLOW_USER',
    # Local biz
    'GET_DIRECTIONS', 'GET_SHOWTIMES',
    # Media
    'WATCH_MORE', 'WATCH_VIDEO', 'LISTEN_MUSIC', 'LISTEN_NOW',
    'OPEN_LINK',  # 通常是非购物
    # Nonprofit / political
    'DONATE', 'DONATE_NOW', 'VOTE_NOW', 'REGISTER_NOW',
}


# ── CTA 白名单 (definitely commerce) ────────────────────────────────────────
CTA_COMMERCE_WHITELIST = {
    'SHOP_NOW', 'BUY_NOW', 'GET_OFFER', 'ORDER_NOW',
    'VIEW_PRODUCT', 'ADD_TO_CART', 'SEE_MENU',
    'GET_PROMOTIONS',  # 边缘但通常电商促销
    'SUBSCRIBE',       # subscription commerce 算独立站
}


# CTA 灰名单: LEARN_MORE / GET_OFFER / NO_BUTTON / 空值 — 看 link_url 决定
CTA_AMBIGUOUS = {
    'LEARN_MORE', 'NO_BUTTON', '', None,
    'GET_INFO',  # 时常 LP 导流
}


# ── Domain 黑名单 ──────────────────────────────────────────────────────────
# Marketplace (非独立站):
MARKETPLACE_DOMAINS = {
    'amazon.com', 'amazon.ca', 'amazon.co.uk', 'amazon.com.au',
    'amazon.de', 'amazon.fr', 'amazon.es', 'amazon.it', 'amazon.nl',
    'amazon.com.mx', 'amazon.com.br', 'amzn.to', 'a.co',
    'walmart.com', 'target.com', 'bestbuy.com', 'costco.com',
    'homedepot.com', 'lowes.com', 'kroger.com', 'wayfair.com',
    'etsy.com', 'ebay.com', 'ebay.co.uk', 'ebay.de',
    'mercari.com', 'poshmark.com', 'depop.com',
    'aliexpress.com', 'aliexpress.us', 'temu.com', 'shein.com',
    'tiktok.com', 'tiktokshop.com',  # TikTok shop = marketplace
    'rakuten.com', 'jet.com',
    'newegg.com', 'b-h.com', 'bhphotovideo.com',
}

# FB 自家 (lead-gen 或品牌主页):
FB_OWN_DOMAINS = {
    'facebook.com', 'fb.com', 'fb.me', 'm.me', 'messenger.com',
    'instagram.com', 'whatsapp.com', 'wa.me', 'l.wl.co',
}

# App stores:
APP_STORE_DOMAINS = {
    'apps.apple.com', 'itunes.apple.com', 'apple.co',
    'play.google.com', 'goo.gl',
    'microsoft.com',
}

# Link aggregators (通常是 lead 或个人 bio link, 不是直购):
LINK_AGGREGATORS = {
    'linktr.ee', 'beacons.ai', 'bio.link', 'lnk.bio', 'msha.ke',
    'linkin.bio', 'allmylinks.com', 'tap.bio', 'campsite.bio',
}

# 平台导流类 — 这些域名永远不是独立站, 无论 path 深浅都 reject:
PLATFORM_HOMES = {
    'youtube.com', 'youtu.be', 'twitter.com', 'x.com',
    'tiktok.com',
    'pinterest.com', 'reddit.com', 'snapchat.com', 'twitch.tv',
}


# ── Commerce URL signal (positive — 几乎肯定是独立站卖货 path) ──────────────
COMMERCE_URL_PATTERNS = [
    re.compile(r'/products?/', re.I),       # Shopify / WooCommerce 标配
    re.compile(r'/collections?/', re.I),     # Shopify
    re.compile(r'/shop/', re.I),             # 通用
    re.compile(r'/store/', re.I),
    re.compile(r'\?variant=', re.I),         # Shopify variant
    re.compile(r'add-to-cart', re.I),
    re.compile(r'/checkout', re.I),
    re.compile(r'\.myshopify\.com', re.I),  # 未上自定义域名的 Shopify
    re.compile(r'/buy[/?]', re.I),
    re.compile(r'/cart[/?]?', re.I),
    re.compile(r'/p/[^/]+', re.I),           # 通用产品 path 简写 (如 /p/widget-x)
    re.compile(r'/sale[/?]', re.I),
    re.compile(r'/offers?[/?]', re.I),
]


# ── D2C 域名白名单 (v10) ──────────────────────────────────────────────────
# 从 d2c_domains.txt 加载已知独立站域名, 命中即放行
_D2C_DOMAINS: set[str] = set()
_d2c_env = os.getenv('D2C_DOMAINS_FILE', '')
_D2C_FILE = Path(_d2c_env) if _d2c_env else (Path(__file__).resolve().parent.parent / 'd2c_domains.txt')


def _load_d2c_domains() -> set[str]:
    global _D2C_DOMAINS
    if _D2C_DOMAINS:
        return _D2C_DOMAINS
    if _D2C_FILE.exists():
        for line in _D2C_FILE.read_text(encoding='utf-8').splitlines():
            d = line.strip().lower()
            if d and not d.startswith('#'):
                _D2C_DOMAINS.add(d)
    return _D2C_DOMAINS


def _is_known_d2c(domain: str) -> bool:
    domains = _load_d2c_domains()
    return domain in domains


# ── UTM 参数检测 (v10) ─────────────────────────────────────────────────────
UTM_PARAMS = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term'}


def _has_utm_params(url: str) -> bool:
    try:
        qs = parse_qs(urlparse(url).query, keep_blank_values=False)
        return bool(UTM_PARAMS & set(qs.keys()))
    except Exception:
        return False


def _norm_domain(url: str) -> str:
    """提取 host, lowercase, 去 www. 失败返回空."""
    if not url or not isinstance(url, str):
        return ''
    try:
        p = urlparse(url if '://' in url else f'https://{url}')
        host = (p.netloc or '').lower().split(':')[0]
        if host.startswith('www.'):
            host = host[4:]
        return host
    except Exception:
        return ''


def _is_marketplace(domain: str) -> bool:
    if not domain:
        return False
    if domain in MARKETPLACE_DOMAINS:
        return True
    # subdomain match (sellercentral.amazon.com 等)
    for m in MARKETPLACE_DOMAINS:
        if domain.endswith('.' + m):
            return True
    return False


def _is_fb_own(domain: str) -> bool:
    if not domain:
        return False
    if domain in FB_OWN_DOMAINS:
        return True
    for m in FB_OWN_DOMAINS:
        if domain.endswith('.' + m):
            return True
    return False


def _is_app_store(domain: str) -> bool:
    return domain in APP_STORE_DOMAINS or any(
        domain.endswith('.' + d) for d in APP_STORE_DOMAINS
    )


def _is_link_aggregator(domain: str) -> bool:
    return domain in LINK_AGGREGATORS or any(
        domain.endswith('.' + d) for d in LINK_AGGREGATORS
    )


def _is_platform_home(url: str, domain: str) -> bool:
    """社交/内容平台域名 — 无论 path 深浅都不是独立站卖货."""
    if domain in PLATFORM_HOMES or any(domain.endswith('.' + d) for d in PLATFORM_HOMES):
        return True
    return False


def _has_commerce_url_signal(url: str) -> bool:
    if not url:
        return False
    return any(rx.search(url) for rx in COMMERCE_URL_PATTERNS)


def is_commerce_ad(ad: dict) -> tuple[bool, str]:
    """主决策函数 (v10). 返回 (是否独立站卖货, 原因 tag).

    v10 核心变化: learn_more + 独立域名 = 放行 (不再要求 URL pattern).
    """
    if not isinstance(ad, dict):
        return False, 'not_dict'

    # 1. archive_type 黑名单
    archive_type = (ad.get('archive_type') or '').upper().strip()
    if archive_type in ARCHIVE_TYPE_BLACKLIST:
        return False, f'archive:{archive_type.lower()}'

    # 2. 政治广告补充信号
    if ad.get('eu_political_ad'):
        return False, 'archive:eu_political'
    funding = (ad.get('funding_entity') or '').strip()
    if funding:
        return False, 'archive:funding_disclosed'
    disclaimer = (ad.get('disclaimer_label') or '').strip()
    if disclaimer and disclaimer.lower() not in ('', 'none', 'n/a'):
        return False, 'archive:has_disclaimer'

    # 3. CTA 检查
    cta = (ad.get('cta_type') or ad.get('cta') or '').upper().strip()
    if cta in CTA_BLACKLIST:
        return False, f'cta_blacklist:{cta.lower()}'

    # 4. URL 检查
    url = ad.get('link_url') or ''
    if not url:
        return False, 'no_link_url'

    domain = _norm_domain(url)
    if not domain or len(domain) < 4:
        return False, 'invalid_url'

    # 5. domain 黑名单
    if _is_fb_own(domain):
        return False, 'fb_own_domain'
    if _is_marketplace(domain):
        return False, f'marketplace:{domain}'
    if _is_app_store(domain):
        return False, 'app_store'
    if _is_link_aggregator(domain):
        return False, 'link_aggregator'
    if _is_platform_home(url, domain):
        return False, 'platform_home'

    # 6. 决策矩阵 (v10: 大幅放宽 ambiguous CTA)
    has_url_signal = _has_commerce_url_signal(url)
    has_utm = _has_utm_params(url)
    is_d2c = _is_known_d2c(domain)
    page_likes = ad.get('page_like_count') or 0

    if cta in CTA_COMMERCE_WHITELIST:
        return True, f'commerce_cta:{cta.lower()}'

    if cta in CTA_AMBIGUOUS:
        # v10: 多信号放行 (任一命中即通过)
        if has_url_signal:
            return True, 'ambiguous_cta+url_signal'
        if is_d2c:
            return True, 'ambiguous_cta+d2c_domain'
        if has_utm:
            return True, 'ambiguous_cta+utm_params'
        # v10 核心: 独立域名 (非黑名单) + learn_more = 大概率 D2C, 放行
        # 只有极端情况才 drop: 零粉丝 + 无任何信号
        if 1000 <= page_likes <= 500000:
            return True, 'ambiguous_cta+page_likes_d2c'
        # 最后兜底: 域名不在任何黑名单 = 独立站, 放行
        return True, 'ambiguous_cta+independent_domain'

    # 未知 CTA: 同样放宽
    if has_url_signal:
        return True, 'unknown_cta+url_signal'
    if is_d2c:
        return True, 'unknown_cta+d2c_domain'
    if has_utm:
        return True, 'unknown_cta+utm_params'
    # 未知 CTA + 独立域名: 放行 (v10)
    return True, 'unknown_cta+independent_domain'


def filter_commerce_ads(ads: list) -> tuple[list, dict]:
    """批量过滤. 返回 (kept_ads, drop_stats: {reason: count}).
    """
    kept: list = []
    stats: dict = {}
    for a in ads or []:
        ok, reason = is_commerce_ad(a)
        if ok:
            kept.append(a)
        else:
            stats[reason] = stats.get(reason, 0) + 1
    return kept, stats

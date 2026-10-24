"""lp_prober.py — Landing Page 技术栈指纹探针.

读 recommendations.landing_url, 抓 HTML 首 200KB, 检测 Shopify/Klaviyo/
Judge.me/Recharge/等 D2C 关键技术栈,缓存结果到 lp_fingerprints 表.

用法:
  • 首次 probe: probe_domains(limit=50) → 后台批跑
  • 查缓存:    get_fingerprint(domain) → dict or None
  • 清旧:      fingerprints >30d 标记 stale, 允许 repobe

架构:
  • ThreadPoolExecutor 并发 (默认 8 线程 · GIL 下 I/O 绑定 OK)
  • 不用 playwright (太重) · urllib.request + User-Agent 轮换
  • 失败不拉黑, 只标记 last_error, 下次再试
"""
from __future__ import annotations

import ipaddress
import html
import json
import random
import re
import socket
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib import request as _req
from urllib.error import URLError, HTTPError
from urllib.parse import urlparse

from core.logger import get_logger

log = get_logger(__name__)

# 轮换 UA, 降低被 Shopify WAF 识别为 bot 的概率
_UA_POOL = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Mobile/15E148 Safari/604.1",
]

# 技术栈识别规则 (name -> (regex, points)) — 与 insights_2026_v2.LP_TECH_SIGNATURES 对齐
TECH_RULES = {
    'shopify':      (re.compile(r'cdn\.shopify\.com|shopify_stats|Shopify\.theme|x-shopify-stage', re.I), 10),
    'klaviyo':      (re.compile(r'klaviyo\.js|kla\.js|_learnq|klaviyo_identify|klaviyo\.com/onsite', re.I), 8),
    'reviews':      (re.compile(r'judge\.me|loox\.io|stamped\.io|yotpo|okendo|reviews\.io', re.I), 6),
    'subscription': (re.compile(r'rechargepayments|bold_subscription|smartrr|skio|recharge\.com', re.I), 7),
    'upsell':       (re.compile(r'zipify|reconvert|honeycomb-upsell|rebuy\.io|aftersell', re.I), 5),
    'analytics':    (re.compile(r'triplewhale|northbeam\.io|hotjar|clarity\.ms|polarapp', re.I), 4),
    'support':      (re.compile(r'gorgias|zendesk|intercom|tidio|re\:amaze', re.I), 4),
    'payment':      (re.compile(r'shop[-_\s]?pay|apple.?pay|afterpay|klarna|\.stripe\.com', re.I), 3),
    'sms':          (re.compile(r'postscript\.io|attentive\.com|sms_bump|omnisend|onesignal', re.I), 6),  # Reddit 2026: 顶级 D2C 必有 SMS 复购
    'meta_pixel':   (re.compile(r'fbq\(|facebook\.net/.*/fbevents\.js|tr=PageView', re.I), 2),
    'tiktok_pixel': (re.compile(r'analytics\.tiktok\.com|ttq\.load|ttq\.track', re.I), 3),
    'ga4':          (re.compile(r'gtag\(.?config.?,.?.G-[A-Z0-9]+|googletagmanager\.com/gtag', re.I), 1),
}

# ═══════════════════════════════════════════════════════════════════════════
# Reddit 2026 Q2 信号 — r/dropship / r/FacebookAds / r/ecommerce 实测总结
# ═══════════════════════════════════════════════════════════════════════════

# Trust badge stack — 顶级品牌必有 (每命中 +2, 封顶 +8)
TRUST_BADGES = re.compile(
    r'(?i)('
    r'ships?\s+from\s+us'
    r'|money[-\s]?back\s+guarantee'
    r'|as\s+seen\s+(on|in)'
    r'|\d{1,3}[,.]?\d{3}\+?\s+(customers|reviews|happy|sold)'
    r'|secure\s+checkout'
    r'|\d+[-\s]?day\s+(money[-\s]?back|guarantee|return)'
    r'|verified\s+reviews'
    r'|bbb\s+accredited'
    r'|30[-\s]?day\s+trial'
    r')')

# Single-product LP 识别 — 高转化
_SINGLE_PRODUCT_URL_HINT = re.compile(r'/products/[^/?#]+(?:/|\?|$|#)', re.I)

# Discount stacking — dropship 常见"60% OFF + Free Ship + BOGO"三叠价
DISCOUNT_STACK = re.compile(
    r'(?i)(?:'
    r'\b(\d{2,3})%\s*off\b'
    r'|free\s+shipping'
    r'|buy\s+\d\s+get\s+\d'
    r'|bogo'
    r'|limited[-\s]time'
    r'|today\s+only'
    r'|flash\s+sale'
    r')')

# Return policy 深度 — 真 D2C 30+ 天, dropship 常 7 天
RETURN_POLICY_RE = re.compile(
    r'(?i)(\d{1,3})[-\s]?day\s+(?:money[-\s]?back|return|refund|guarantee)')

# Dropship 死招牌 (反向信号, 命中直接降分)
DROPSHIP_FLAGS = re.compile(
    r'powered[-_\s]by[-_\s]dsers|cjdropshipping|aliexpress\.com/item|dropified|oberlo'
    r'|spocket\.co|zendrop|dropshipman', re.I)

# 价格提取 (粗,仅作信号)
_PRICE_RE = re.compile(r'["\']price["\']\s*:\s*["\']?(\d{2,5}(?:\.\d{2})?)', re.I)
_MIN_MAX_PRICE_RE = re.compile(r'"price_min"\s*:\s*(\d+)\D+"price_max"\s*:\s*(\d+)', re.I)


# ═══════════════════════════════════════════════════════════════════════════
# Cache table
# ═══════════════════════════════════════════════════════════════════════════

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS lp_fingerprints (
        domain        TEXT PRIMARY KEY,
        tags_json     TEXT,
        bonus         INTEGER DEFAULT 0,
        price_hint    REAL,
        has_dropship  INTEGER DEFAULT 0,
        probed_at     TEXT,
        last_error    TEXT,
        http_status   INTEGER,
        reddit_bonus  INTEGER DEFAULT 0,
        trust_badges  INTEGER DEFAULT 0,
        single_product INTEGER DEFAULT 0,
        discount_stack INTEGER DEFAULT 0,
        return_days   INTEGER DEFAULT 0
    )""")
    # 幂等添加新字段 (旧库升级)
    for col, typ in [('reddit_bonus','INTEGER DEFAULT 0'), ('trust_badges','INTEGER DEFAULT 0'),
                     ('single_product','INTEGER DEFAULT 0'), ('discount_stack','INTEGER DEFAULT 0'),
                     ('return_days','INTEGER DEFAULT 0')]:
        try: conn.execute(f'ALTER TABLE lp_fingerprints ADD COLUMN {col} {typ}')
        except Exception: pass
    conn.commit()


def _ua() -> str:
    return random.choice(_UA_POOL)


def _normalize_probe_url(url: str) -> tuple[Optional[str], Optional[str]]:
    if not url:
        return None, 'empty-url'
    raw = html.unescape(url.strip().strip('"\''))
    raw = raw.replace('\\/', '/')
    if not raw.startswith(('http://', 'https://')):
        raw = 'https://' + raw
    parsed = urlparse(raw)
    if parsed.scheme not in {'http', 'https'}:
        return None, 'blocked-scheme'
    host = (parsed.hostname or '').strip().lower()
    if not host:
        return None, 'missing-host'
    if host in {'localhost', '127.0.0.1', '0.0.0.0'} or host.endswith('.local'):
        return None, 'blocked-localhost'
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return None, 'blocked-private-ip'
    except ValueError:
        try:
            for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP):
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                    return None, 'blocked-private-dns'
        except Exception:
            pass
    return raw, None


class _NoRedirect(_req.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = _req.build_opener(_NoRedirect)


def _fetch(url: str, timeout: int = 10, max_bytes: int = 200_000) -> tuple[Optional[str], int, Optional[str]]:
    """返回 (body_text, http_status, error)."""
    safe_url, blocked = _normalize_probe_url(url)
    if blocked:
        return None, 0, blocked
    try:
        req = _req.Request(safe_url, headers={
            'User-Agent': _ua(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'identity',
        })
        with _OPENER.open(req, timeout=timeout) as resp:
            status = resp.status
            content_type = (resp.headers.get('Content-Type') or '').lower()
            if content_type and not any(t in content_type for t in ('text/html', 'application/xhtml+xml', 'text/plain')):
                return None, status, 'non-html'
            data = resp.read(max_bytes)
            try:
                text = data.decode('utf-8', errors='ignore')
            except Exception:
                text = data.decode('latin-1', errors='ignore')
            return text, status, None
    except HTTPError as e:
        if 300 <= e.code < 400:
            return None, e.code, 'redirect-blocked'
        return None, e.code, f'HTTP{e.code}'
    except URLError as e:
        return None, 0, f'URLError:{e.reason}'[:200]
    except Exception as e:
        return None, 0, f'{type(e).__name__}:{str(e)[:150]}'


def _parse(html: str, url: str = '') -> dict:
    """从 HTML 抽出 tags + bonus + price_hint + Reddit 洞见信号."""
    if not html:
        return {'tags': [], 'bonus': 0, 'price_hint': None, 'has_dropship': False,
                'trust_badges': 0, 'is_single_product': False, 'discount_stack_count': 0,
                'return_policy_days': 0, 'reddit_bonus': 0}
    tags: list[str] = []
    bonus = 0
    for name, (regex, pts) in TECH_RULES.items():
        if regex.search(html):
            tags.append(f'lp:{name}')
            bonus += pts
    bonus = min(bonus, 35)  # 新签名 (sms) 加入后稍微放宽

    # price_hint
    price_hint = None
    m = _MIN_MAX_PRICE_RE.search(html)
    if m:
        try:
            lo = int(m.group(1)) / 100.0
            hi = int(m.group(2)) / 100.0
            price_hint = (lo + hi) / 2
        except Exception:
            pass
    if price_hint is None:
        m2 = _PRICE_RE.search(html)
        if m2:
            try:
                raw = m2.group(1)
                # Shopify 的 "price" 是整数分；带小数点的主题已是元。
                # 不做区分会把 5795 分($57.95)当成 $5795 写库，
                # 进而污染 insights_2026_v2.price_band_score()。
                price_hint = float(raw) if '.' in raw else int(raw) / 100.0
            except Exception:
                pass

    has_dropship = bool(DROPSHIP_FLAGS.search(html))

    # ── Reddit 洞见信号 ──
    # Trust badges: 每命中 +2, 封顶 +8
    trust_matches = TRUST_BADGES.findall(html)
    trust_badges = min(len(trust_matches), 4)  # 最多 4 类, 对应 +8

    # Single-product LP (URL + product JSON 双重)
    is_single_product = bool(url and _SINGLE_PRODUCT_URL_HINT.search(url))

    # Discount stacking: 3+ 种折扣叠加 = dropship
    discount_stack_count = len(set(m.group(0).lower() for m in DISCOUNT_STACK.finditer(html)))

    # Return policy: 30+ 天 = 真 D2C, <14 天 = 可疑
    return_policy_days = 0
    for m in RETURN_POLICY_RE.finditer(html):
        try:
            d = int(m.group(1))
            return_policy_days = max(return_policy_days, d)
        except Exception:
            pass

    # Reddit bonus 汇总
    reddit_bonus = 0
    reddit_bonus += trust_badges * 2                      # +0~8
    reddit_bonus += 5 if is_single_product else 0          # +5 (高转化 LP)
    if discount_stack_count >= 3: reddit_bonus -= 10       # 3+ 叠加 dropship
    elif discount_stack_count == 2: reddit_bonus -= 3
    if 30 <= return_policy_days <= 365: reddit_bonus += 5  # 30d+ 真 D2C
    elif 1 <= return_policy_days < 14: reddit_bonus -= 5   # <14d 可疑

    if trust_badges >= 2: tags.append(f'reddit:trust+{trust_badges}')
    if is_single_product: tags.append('reddit:single-product-LP')
    if discount_stack_count >= 3: tags.append(f'reddit:DISCOUNT-STACK×{discount_stack_count}')
    if return_policy_days >= 30: tags.append(f'reddit:return-{return_policy_days}d')
    elif 0 < return_policy_days < 14: tags.append(f'reddit:short-return-{return_policy_days}d')

    return {
        'tags': tags,
        'bonus': bonus,
        'price_hint': price_hint,
        'has_dropship': has_dropship,
        'trust_badges': trust_badges,
        'is_single_product': is_single_product,
        'discount_stack_count': discount_stack_count,
        'return_policy_days': return_policy_days,
        'reddit_bonus': reddit_bonus,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════

def probe_one(domain: str, url: str, conn: sqlite3.Connection | None = None) -> dict:
    """conn=None → 本函数内部开连接 (线程安全)."""
    own_conn = conn is None
    own_cm = None
    if own_conn:
        from .persistence import get_persistence
        db = get_persistence()
        # BUGFIX 2026-05-05: keep cm in local var so Py3.14 GC doesn't finalize
        # it (which would close conn prematurely). Old code: conn = db._conn().__enter__()
        own_cm = db._conn()
        conn = own_cm.__enter__()
        ensure_schema(conn)
    html, status, err = _fetch(url)
    parsed = _parse(html or '', url=url or '')
    now_iso = datetime.now().isoformat(timespec='seconds')
    try:
        conn.execute("""INSERT INTO lp_fingerprints
            (domain, tags_json, bonus, price_hint, has_dropship, probed_at, last_error, http_status,
             reddit_bonus, trust_badges, single_product, discount_stack, return_days)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(domain) DO UPDATE SET
              tags_json=excluded.tags_json, bonus=excluded.bonus,
              price_hint=excluded.price_hint, has_dropship=excluded.has_dropship,
              probed_at=excluded.probed_at, last_error=excluded.last_error,
              http_status=excluded.http_status, reddit_bonus=excluded.reddit_bonus,
              trust_badges=excluded.trust_badges, single_product=excluded.single_product,
              discount_stack=excluded.discount_stack, return_days=excluded.return_days
        """, (
            domain, json.dumps(parsed['tags']), parsed['bonus'],
            parsed['price_hint'], 1 if parsed['has_dropship'] else 0,
            now_iso, err, status or 0,
            parsed['reddit_bonus'], parsed['trust_badges'],
            1 if parsed['is_single_product'] else 0,
            parsed['discount_stack_count'], parsed['return_policy_days'],
        ))
        conn.commit()
    finally:
        if own_cm is not None:
            own_cm.__exit__(None, None, None)
    return {
        'domain': domain, 'url': url, **parsed,
        'http_status': status, 'error': err, 'probed_at': now_iso,
    }


def probe_domains(
    limit: int = 50,
    max_workers: int = 8,
    force_refresh: bool = False,
    stale_days: int = 30,
    dry_run: bool = False,
) -> dict:
    """批量探针: 选 recommendations 里 landing_url 未 probed 或已 stale 的."""
    from .persistence import get_persistence
    db = get_persistence()
    # BUGFIX 2026-05-05: hold cm in local var (was `db._conn().__enter__()` which leaked).
    cm = db._conn()
    conn = cm.__enter__()
    try:
        ensure_schema(conn)

        if force_refresh:
            cur = conn.execute("""
                SELECT domain, landing_url FROM recommendations
                WHERE landing_url IS NOT NULL AND landing_url != ''
                ORDER BY score DESC LIMIT ?
            """, (limit,))
        else:
            cur = conn.execute(f"""
                SELECT r.domain, r.landing_url
                FROM recommendations r
                LEFT JOIN lp_fingerprints f ON f.domain = r.domain
                WHERE r.landing_url IS NOT NULL AND r.landing_url != ''
                  AND (f.probed_at IS NULL
                       OR f.probed_at < datetime('now', '-{int(stale_days)} days')
                       OR (f.http_status = 0 AND f.probed_at < datetime('now', '-1 days')))
                ORDER BY r.score DESC
                LIMIT ?
            """, (limit,))

        targets = [(r['domain'], r['landing_url']) for r in cur.fetchall()]
        if not targets:
            return {'ok': True, 'probed': 0, 'note': 'no stale targets'}
        if dry_run:
            return {
                'ok': True,
                'dry_run': True,
                'would_probe': len(targets),
                'workers': max_workers,
                'force_refresh': force_refresh,
                'stale_days': stale_days,
                'sample': [{'domain': d, 'url': u} for d, u in targets[:10]],
            }

        max_workers = max(1, min(int(max_workers or 8), 16))
        log.info(f'[lp_prober] probing {len(targets)} domains, workers={max_workers}')
        done = []
        ok = 0
        t0 = time.time()
        # 每个 worker 内部开自己的 sqlite connection (线程安全)
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(probe_one, dom, url, None): dom for dom, url in targets}
            for fut in as_completed(futures):
                dom = futures[fut]
                try:
                    res = fut.result()
                    done.append({'domain': dom, 'tags': res['tags'], 'bonus': res['bonus'],
                                 'status': res['http_status']})
                    if res['http_status'] == 200:
                        ok += 1
                except Exception as e:
                    done.append({'domain': dom, 'error': str(e)[:150]})

        elapsed = time.time() - t0
        return {
            'ok': True,
            'probed': len(done),
            'success_200': ok,
            'elapsed_sec': round(elapsed, 1),
            'throughput_per_sec': round(len(done) / max(elapsed, 0.1), 2),
            'sample': done[:10],
        }
    finally:
        cm.__exit__(None, None, None)


def get_fingerprint(domain: str) -> Optional[dict]:
    from .persistence import get_persistence
    db = get_persistence()
    # BUGFIX 2026-05-05: hold cm in local var so Py3.14 GC doesn't close conn
    # via leaked-cm finalize before fetchone() runs.
    cm = db._conn()
    conn = cm.__enter__()
    try:
        ensure_schema(conn)
        r = conn.execute(
            "SELECT tags_json, bonus, price_hint, has_dropship, probed_at, http_status "
            "FROM lp_fingerprints WHERE domain = ?",
            (domain,),
        ).fetchone()
    finally:
        cm.__exit__(None, None, None)
    if not r:
        return None
    return {
        'tags': json.loads(r['tags_json'] or '[]'),
        'bonus': r['bonus'] or 0,
        'price_hint': r['price_hint'],
        'has_dropship': bool(r['has_dropship']),
        'probed_at': r['probed_at'],
        'http_status': r['http_status'],
    }


def get_fingerprints_bulk(domains: list[str]) -> dict[str, dict]:
    """一次性查多个域名,供 rerank-v2 endpoint 批量使用."""
    if not domains:
        return {}
    from .persistence import get_persistence
    db = get_persistence()
    # BUGFIX 2026-05-05: hold cm in local var (was `db._conn().__enter__()` leak).
    cm = db._conn()
    conn = cm.__enter__()
    try:
        ensure_schema(conn)
        q = ','.join('?' * len(domains))
        rows = conn.execute(
            f"SELECT domain, tags_json, bonus, price_hint, has_dropship, probed_at, "
            f"reddit_bonus, trust_badges, single_product, discount_stack, return_days "
            f"FROM lp_fingerprints WHERE domain IN ({q})",
            domains,
        ).fetchall()
    finally:
        cm.__exit__(None, None, None)
    out: dict[str, dict] = {}
    for r in rows:
        out[r['domain']] = {
            'tags': json.loads(r['tags_json'] or '[]'),
            'bonus': r['bonus'] or 0,
            'price_hint': r['price_hint'],
            'has_dropship': bool(r['has_dropship']),
            'probed_at': r['probed_at'],
            'reddit_bonus': r['reddit_bonus'] or 0,
            'trust_badges': r['trust_badges'] or 0,
            'single_product': bool(r['single_product']),
            'discount_stack': r['discount_stack'] or 0,
            'return_days': r['return_days'] or 0,
        }
    return out

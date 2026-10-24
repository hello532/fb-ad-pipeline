"""pipeline/signals.py — 从广告文本中提取商业信号.

这些信号用于判断产品可行性:
- price_points: 价格出现 (e.g. $49, $19.99, 50% off)
- review_mentions: 社会证明 (10000+ reviews, 5-star, verified)
- urgency_markers: 紧迫感 (limited time, hurry, today only)
- shipping_signals: 履约信息 (free shipping, 2-day delivery)
- guarantee_signals: 保障 (money back, 30-day return, lifetime warranty)
"""
from __future__ import annotations

import re
from typing import Any

# ── 价格正则 ─────────────────────────────────────────────────────────
PRICE_PATTERNS = [
    # $39.99 / $ 39 / $39
    (re.compile(r'\$\s?(\d{1,4}(?:\.\d{1,2})?)', re.I), 'USD'),
    # only $29 / starting at $49 / from $19
    (re.compile(r'(?:only|starting\s+at|from|just|now)\s+\$?\s?(\d{1,4}(?:\.\d{1,2})?)', re.I), 'USD'),
    # 29.99 USD / 49 dollars
    (re.compile(r'\b(\d{1,4}(?:\.\d{1,2})?)\s*(?:USD|dollars?)\b', re.I), 'USD'),
    (re.compile(r'£\s?(\d{1,4}(?:\.\d{1,2})?)', re.I), 'GBP'),
    (re.compile(r'€\s?(\d{1,4}(?:\.\d{1,2})?)', re.I), 'EUR'),
]
DISCOUNT_PATTERNS = [
    re.compile(r'(\d{1,2})\s?%\s?(?:off|discount|sale|savings?)', re.I),
    re.compile(r'(?:save|discount|markdown)\s+(\d{1,2})\s?%', re.I),
    re.compile(r'(?:buy\s+\d+\s+get\s+\d+)', re.I),
    re.compile(r'\bhalf\s+price\b', re.I),
    re.compile(r'\b(\d{1,2})\s?%\s+price\s+drop\b', re.I),
]

# ── 社会证明 ─────────────────────────────────────────────────────────
REVIEW_COUNT = re.compile(r'(\d[\d,\.]+)\+?\s*(?:reviews?|customers?|sold|happy\s+customers?|orders?|buyers?|users?|people)', re.I)
STAR_RATING = re.compile(r'([45](?:\.\d)?)\s*(?:star|/\s*5|★|⭐)', re.I)
VERIFIED_MARKERS = re.compile(r'\b(verified|trusted|certified|clinically\s+proven|fda(?:\s+approved)?|doctor\s+recommended|#1\s+rated|top\s+rated|best\s+seller|as\s+seen\s+on\s+tv|patented|award\s+winning)\b', re.I)

# ── 紧迫感 / 稀缺 ──────────────────────────────────────────────────────
URGENCY_PHRASES = re.compile(
    r'\b(limited\s+time|today\s+only|hurry|last\s+chance|act\s+now|while\s+supplies\s+last|'
    r'ends\s+(?:today|soon|tonight)|\d{1,2}\s*left\s+in\s+stock|selling\s+fast|flash\s+sale)\b',
    re.I
)

# ── 履约 ──────────────────────────────────────────────────────────────
SHIPPING_SIGNALS = re.compile(
    r'\b(free\s+(?:shipping|delivery)|same\s+day|next\s+day|\d+[-\s]day\s+shipping|'
    r'ships?\s+from\s+(?:us|usa|uk|canada)|ships?\s+worldwide)\b', re.I
)

# ── 保障 / 风险消除 ────────────────────────────────────────────────────
GUARANTEE_SIGNALS = re.compile(
    r'\b(money[-\s]?back|\d{1,3}[-\s]day\s+(?:return|guarantee|trial)|lifetime\s+(?:warranty|guarantee)|'
    r'satisfaction\s+guarantee(?:d)?|risk[-\s]?free|no[-\s]?risk)\b', re.I
)

# ── 情感钩子 ───────────────────────────────────────────────────────────
BEFORE_AFTER = re.compile(r'\b(before\s*(?:and|&|\+|\s*\/\s*)\s*after|transformation|results?\s+in\s+\d+\s+(?:days?|weeks?))\b', re.I)
TESTIMONIAL = re.compile(r'"[^"]{20,200}"', re.I)  # 引号包起来的长句 可能是见证


def extract_signals(text: str) -> dict[str, Any]:
    """返回一个信号字典. 空字段用 [] / None 留白, 不填噪声."""
    text = text or ''
    if not text.strip():
        return {}

    # 价格 — 允许多种写法, 取所有匹配再去重
    prices = []
    for pat, cur in PRICE_PATTERNS:
        for m in pat.finditer(text):
            try:
                v = float(m.group(1))
                if 1 <= v <= 9999:
                    prices.append({'value': v, 'currency': cur})
            except Exception:
                pass
    # 去重
    seen = set()
    uniq_prices = []
    for p in prices:
        key = (p['value'], p['currency'])
        if key not in seen:
            seen.add(key)
            uniq_prices.append(p)
    uniq_prices.sort(key=lambda x: x['value'])

    # 折扣
    discounts = []
    for pat in DISCOUNT_PATTERNS:
        for m in pat.finditer(text):
            try:
                discounts.append(int(m.group(1)))
            except Exception:
                pass
    discounts = sorted(set(discounts), reverse=True)[:3]

    # 评论数
    review_counts = []
    for m in REVIEW_COUNT.finditer(text):
        try:
            v = int(m.group(1).replace(',', '').replace('.', ''))
            if v >= 10:
                review_counts.append(v)
        except Exception:
            pass
    max_reviews = max(review_counts) if review_counts else None

    # 星级
    stars = []
    for m in STAR_RATING.finditer(text):
        try:
            stars.append(float(m.group(1)))
        except Exception:
            pass
    max_stars = max(stars) if stars else None

    # 其他
    verified = VERIFIED_MARKERS.findall(text)
    urgency = URGENCY_PHRASES.findall(text)
    shipping = SHIPPING_SIGNALS.findall(text)
    guarantee = GUARANTEE_SIGNALS.findall(text)
    before_after = bool(BEFORE_AFTER.search(text))
    testimonials = TESTIMONIAL.findall(text)

    signals: dict[str, Any] = {}
    if uniq_prices:
        signals['prices'] = uniq_prices[:5]
        signals['price_main'] = uniq_prices[0]['value']
    if discounts:
        signals['discount_pct'] = discounts
        signals['has_discount'] = True
    if max_reviews:
        signals['review_count_max'] = max_reviews
    if max_stars:
        signals['star_rating'] = max_stars
    if verified:
        signals['authority_markers'] = list(set(v.lower() for v in verified))[:5]
    if urgency:
        signals['urgency_phrases'] = list(set(u.lower() for u in urgency))[:3]
        signals['has_urgency'] = True
    if shipping:
        signals['shipping_markers'] = list(set(s.lower() for s in shipping))[:3]
    if guarantee:
        signals['guarantee_markers'] = list(set(g.lower() for g in guarantee))[:3]
    if before_after:
        signals['has_before_after'] = True
    if testimonials:
        signals['has_testimonial'] = True
        signals['testimonial_sample'] = testimonials[0][:200]

    return signals


def combined_signal_score(signals: dict) -> int:
    """综合商业信号分 (0-30), 供推荐引擎加权."""
    if not signals:
        return 0
    s = 0
    if signals.get('price_main'):
        s += 5  # 透明定价
    if signals.get('has_discount'):
        s += 3
    if signals.get('review_count_max', 0) >= 1000:
        s += 6
    elif signals.get('review_count_max', 0) >= 100:
        s += 3
    if signals.get('star_rating', 0) >= 4.5:
        s += 3
    if signals.get('authority_markers'):
        s += 3
    if signals.get('has_urgency'):
        s += 2
    if signals.get('guarantee_markers'):
        s += 4
    if signals.get('has_before_after'):
        s += 2
    if signals.get('has_testimonial'):
        s += 2
    return min(s, 30)

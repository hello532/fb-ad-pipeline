"""keyword_harvester.py — auto-fetch trending product keywords when FB queue empties.

Sources:
  • TikTok Creative Center top keywords (via tiktok_ads CLI)
  • Reddit r/dropship / r/shopify / r/Entrepreneur titles (public JSON, no auth)
  • Google Trends daily trending (RSS, no auth)
  • Amazon Best Sellers / Movers & Shakers titles (lightweight HTML)

Strict quality gate: english, 2-4 words, no stopwords, alphanumeric, dedupe vs
SCAN_FILE + processed history. Output appended to facebook-ad-keywords-scan.txt.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Iterable, Set
from xml.etree import ElementTree as ET

from core.logger import get_logger

from .paths import SCAN_FILE, NEW_DIR, RESULTS_DIR

log = get_logger(__name__)


def _http_get(url: str, timeout: float = 15.0) -> bytes | None:
    """stdlib HTTP GET. Returns bytes or None on failure."""
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/json,application/xml,*/*',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError) as e:
        log.debug(f"http get {url[:60]} fail: {e}")
        return None
    except Exception as e:
        log.debug(f"http get {url[:60]} unexpected: {e}")
        return None

VENV_PY = NEW_DIR / ".venv-tiktok" / "bin" / "python3"
TIKTOK_DIR = NEW_DIR / "tiktok"

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"

STOPWORDS = frozenset({
    'the','a','an','of','for','and','or','to','in','on','with','by','from','at',
    'is','are','was','were','be','been','being','have','has','had','do','does','did',
    'this','that','these','those','i','you','he','she','it','we','they','them','his',
    'her','their','our','your','my','me','us','him','its','as','if','but','not','no',
    'so','too','very','can','will','would','should','could','may','might','must',
    'just','only','also','more','most','some','any','all','one','two','new','best',
    'top','your','how','what','when','where','why','who','which','about','out','up',
    'down','off','over','under','then','than','because','while','after','before',
    'now','here','there','each','every','both','few','many','much','other','same',
    'such','own','still','yet','though','even','well','away','ever','make','made',
    'get','got','use','used','using','need','want','like','love','watch','see','look',
    'go','come','take','give','find','feel','help','say','said','tell','told',
    'free','sale','off','today','only','limited','time','review','reviews','vs',
    'amazon','tiktok','google','facebook','reddit','etsy','ebay','shop','online',
    'cheap','best','top','great','awesome','easy','simple','perfect','official',
    'app','site','website','com','www','http','https','video','videos','channel',
    'reels','tweet','tweets','post','posts','live','news','latest','update',
    'subscribe','follow','share','comment','watch','playlist',
    'usa','us','uk','eu','china','india','canada','japan','australia','europe',
    'reddit','dropship','dropshipping','ecommerce','marketing','advertise','ads',
    'guide','tips','tricks','tutorial','blog','article','story','stories',
    'price','prices','deal','deals','discount','coupon','offer','offers',
    'review','rated','rating',
    'quality','luxury','premium','cheap','budget',
    'gift','gifts','holiday','christmas','valentine','easter','halloween',
    'tricks','hack','hacks','trick',
    'people','person','someone','something','anything','everything','nothing',
})

# kept as "high-signal" product nouns even if generic; bypasses stopword filter
GOOD_PRODUCT_HINTS = re.compile(
    r'\b(serum|cream|gel|patch|mask|brush|comb|trimmer|shaver|razor|massager|'
    r'pillow|cushion|blanket|towel|robe|slipper|sandal|sneaker|boot|jacket|'
    r'leggings|shorts|tee|hoodie|dress|skirt|swimsuit|bikini|earring|necklace|'
    r'bracelet|ring|watch|wallet|backpack|tote|crossbody|sunglass|hat|cap|'
    r'mug|bottle|tumbler|kettle|pan|pot|grill|oven|fryer|blender|mixer|'
    r'lamp|light|candle|diffuser|humidifier|purifier|fan|heater|cooler|'
    r'organizer|storage|hanger|hook|drawer|shelf|rack|stand|holder|'
    r'tool|gadget|device|gizmo|opener|peeler|chopper|slicer|grater|'
    r'collar|leash|harness|bowl|toy|cage|bed|treat|brush|'
    r'protein|vitamin|supplement|powder|tablet|capsule|tincture|'
    r'speaker|earbud|headphone|charger|cable|adapter|hub|tracker|monitor|'
    r'keyboard|mouse|stand|mount|tripod|camera|lens|filter|'
    r'planter|seed|fertilizer|sprayer|sprinkler|hose|trimmer|mower|'
    r'helmet|knee|elbow|wrist|ankle|brace|support|sleeve|wrap|'
    r'bag|case|cover|sleeve|pouch|holster|strap|belt)\b',
    re.IGNORECASE)


def _good_token(t: str) -> bool:
    """token quality gate: lowercase 2-3 word phrase, alphanum, not pure stopword."""
    t = t.strip().lower()
    if not (2 <= len(t) <= 40):
        return False
    if not re.match(r'^[a-z][a-z0-9 \-]*[a-z0-9]$', t):
        return False
    words = t.split()
    if not (1 <= len(words) <= 4):
        return False
    # drop if ALL words are stopwords
    if all(w in STOPWORDS for w in words):
        return False
    # drop if first word is stopword AND no product hint
    if words[0] in STOPWORDS and not GOOD_PRODUCT_HINTS.search(t):
        return False
    return True


def _existing_keywords() -> Set[str]:
    """gather all currently-known keywords for dedupe."""
    seen: Set[str] = set()
    if SCAN_FILE.exists():
        for line in SCAN_FILE.read_text(encoding='utf-8', errors='ignore').splitlines():
            kw = line.strip().split('#', 1)[0].strip().lower()
            if kw:
                seen.add(kw)
    # processed history (from RESULTS_DIR file names)
    try:
        for jf in RESULTS_DIR.iterdir():
            stem = jf.stem
            parts = stem.split('_', 1)
            if len(parts) == 2:
                kw = parts[1].replace('-', ' ').replace('_', ' ').lower()
                seen.add(kw)
    except Exception:
        pass
    return seen


# ─── source: TikTok ────────────────────────────────────────────────────────

def harvest_tiktok(country: str = 'US', period: int = 30, limit: int = 100) -> list[str]:
    """Run tiktok_ads keywords subcommand, parse output."""
    if not VENV_PY.exists():
        log.warning("tiktok venv missing, skip tiktok harvest")
        return []
    out_dir = TIKTOK_DIR / 'output' / 'keywords'
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [str(VENV_PY), '-m', 'tiktok_ads',
           '-o', str(TIKTOK_DIR / 'output'),
           'keywords',
           '--country', country, '--period', str(period),
           '--limit', str(limit)]
    try:
        r = subprocess.run(cmd, cwd=str(TIKTOK_DIR), capture_output=True, text=True,
                           timeout=180)
        if r.returncode != 0:
            log.warning(f"tiktok keywords rc={r.returncode}: {r.stderr[-300:]}")
            return []
        # find latest CSV
        csvs = sorted(out_dir.glob('*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
        if not csvs:
            return []
        import csv
        kws = []
        with open(csvs[0], encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                k = (row.get('keyword') or row.get('Keyword') or '').strip()
                if k:
                    kws.append(k)
        return kws[:limit]
    except Exception as e:
        log.warning(f"tiktok harvest failed: {e}")
        return []


# ─── source: Reddit ────────────────────────────────────────────────────────

REDDIT_SUBS = ['dropship', 'shopify', 'Entrepreneur', 'BuyItForLife',
               'ProductPorn', 'amazonsellers', 'EcommerceMarketing']


def harvest_reddit(per_sub: int = 50) -> list[str]:
    """Pull recent post titles from select subreddits, extract product nouns."""
    out = []
    for sub in REDDIT_SUBS:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit={per_sub}"
        body = _http_get(url, timeout=15.0)
        if not body:
            continue
        try:
            data = json.loads(body)
            for child in data.get('data', {}).get('children', []):
                title = (child.get('data', {}).get('title') or '').strip()
                if not title:
                    continue
                words = re.findall(r"[A-Za-z][A-Za-z0-9\-']{1,20}", title)
                for n in (2, 3):
                    for i in range(len(words) - n + 1):
                        gram = ' '.join(words[i:i+n]).lower()
                        if _good_token(gram):
                            out.append(gram)
        except Exception as e:
            log.debug(f"reddit r/{sub} parse fail: {e}")
        time.sleep(1.2)
    return out


def harvest_google_trends(geo: str = 'US') -> list[str]:
    url = f"https://trends.google.com/trending/rss?geo={geo}"
    body = _http_get(url, timeout=15.0)
    if not body:
        return []
    try:
        root = ET.fromstring(body)
        out = []
        for item in root.iter('item'):
            t = item.find('title')
            if t is not None and t.text:
                kw = t.text.strip().lower()
                if _good_token(kw):
                    out.append(kw)
        return out
    except Exception as e:
        log.debug(f"google trends parse fail: {e}")
        return []


AMAZON_BS_URLS = [
    'https://www.amazon.com/gp/movers-and-shakers/',
    'https://www.amazon.com/Best-Sellers/zgbs/',
    'https://www.amazon.com/Best-Sellers-Beauty-Personal-Care/zgbs/beauty/',
    'https://www.amazon.com/Best-Sellers-Health-Personal-Care/zgbs/hpc/',
    'https://www.amazon.com/Best-Sellers-Home-Kitchen/zgbs/home-garden/',
    'https://www.amazon.com/Best-Sellers-Sports-Outdoors/zgbs/sporting-goods/',
]


def harvest_amazon() -> list[str]:
    out = []
    for url in AMAZON_BS_URLS:
        body = _http_get(url, timeout=20.0)
        if not body:
            continue
        try:
            text = body.decode('utf-8', errors='ignore')
            titles = re.findall(r'<span[^>]*class="[^"]*a-text-normal[^"]*"[^>]*>([^<]{8,80})</span>', text)
            titles += re.findall(r'<div[^>]*class="[^"]*p13n-sc-truncate[^"]*"[^>]*>([^<]{8,80})</div>', text)
            for t in titles:
                t = t.strip().lower()
                words = re.findall(r"[a-z][a-z0-9\-']{1,20}", t)
                for n in (2, 3):
                    if len(words) >= n:
                        gram = ' '.join(words[:n])
                        if _good_token(gram):
                            out.append(gram)
        except Exception as e:
            log.debug(f"amazon {url[:60]} parse fail: {e}")
        time.sleep(1.5)
    return out


# ─── orchestrator ──────────────────────────────────────────────────────────

def harvest_all(target: int = 200) -> dict:
    """Aggregate all sources, dedupe, filter, append to SCAN_FILE.

    Returns summary dict: {sources: {name: count}, candidates, added, target}.
    """
    log.info(f"harvest start (target={target})")
    seen = _existing_keywords()
    log.info(f"existing keywords: {len(seen)}")

    by_source = {}
    pool: list[tuple[str, str]] = []  # (kw, source)

    sources = [
        ('tiktok',  lambda: harvest_tiktok(limit=100)),
        ('reddit',  lambda: harvest_reddit(per_sub=40)),
        ('google',  lambda: harvest_google_trends('US')),
        ('amazon',  lambda: harvest_amazon()),
    ]
    for name, fn in sources:
        try:
            kws = fn()
            by_source[name] = len(kws)
            for k in kws:
                pool.append((k.strip().lower(), name))
            log.info(f"  {name}: +{len(kws)} raw")
        except Exception as e:
            log.warning(f"  {name} crashed: {e}")
            by_source[name] = 0

    # dedupe + quality filter, preserve insertion order
    try:
        from .insights_2026 import is_dead_keyword
    except Exception:
        is_dead_keyword = lambda k: False
    fresh: list[tuple[str, str]] = []
    fresh_set: Set[str] = set()
    dead_dropped = 0
    for kw, src in pool:
        if kw in seen or kw in fresh_set:
            continue
        if not _good_token(kw):
            continue
        if is_dead_keyword(kw):
            dead_dropped += 1
            continue
        fresh.append((kw, src))
        fresh_set.add(kw)
        if len(fresh) >= target:
            break
    if dead_dropped:
        log.info(f"  dropped {dead_dropped} dead/saturated keywords (2026 insights)")

    if not fresh:
        log.warning("harvest produced 0 fresh keywords")
        return {'sources': by_source, 'candidates': len(pool),
                'added': 0, 'target': target}

    # append to SCAN_FILE
    today = datetime.now().strftime('%Y-%m-%d')
    SCAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SCAN_FILE, 'a', encoding='utf-8') as f:
        f.write(f"\n# === auto-harvest {today} (sources: {by_source}) ===\n")
        for kw, src in fresh:
            f.write(f"{kw}  # auto-{src}\n")

    log.info(f"harvest added {len(fresh)} fresh keywords to {SCAN_FILE.name}")
    return {
        'sources': by_source,
        'candidates': len(pool),
        'added': len(fresh),
        'target': target,
        'sample': [kw for kw, _ in fresh[:20]],
    }


def harvest_if_empty(min_queue: int = 50, target: int = 200) -> dict | None:
    """Trigger harvest only if queue has fewer than `min_queue` items."""
    try:
        from .scan_instructions import scan_queue_loader
        items = scan_queue_loader()
    except Exception:
        items = []
    if len(items) >= min_queue:
        return {'skipped': True, 'queue_size': len(items), 'min_queue': min_queue}
    log.info(f"queue={len(items)} < {min_queue}, triggering external harvest")
    return harvest_all(target=target)

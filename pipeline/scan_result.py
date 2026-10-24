"""scan_result.py — save_scan_result, refactored for process-pool safety.

Worker-safe variant: writes only the per-keyword _corrected.json inside the worker.
Shared-file writes (SCAN_FILE read-modify-write, LEARNED_FILE append) are returned
as a side-effect payload for the main process to apply serially.
"""
import json, re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from collections import Counter, defaultdict
from urllib.parse import urlparse

from core.logger import get_logger

from .paths import RESULTS_DIR, SCAN_FILE, LEARNED_FILE
from .domain import resolve_domain, extract_domain_from_url, validate_domain
from .utils import normalize_ad, calculate_ad_age, analyze_market_trend, extract_cta_patterns, categorize_competition_level
from .text_analyzer import AdTextAnalyzer
from .keyword_enricher import KeywordEnricher
from .opportunity import ProductOpportunityAnalyzer
from .scoring import calculate_brand_score_v7, _resolve_brand_identity
from .trend import estimate_trend
from .signals import extract_signals
from .defaults import DASH, UNKNOWN, NONE_LABEL, safe_str, safe_int

try:
    from .insights_2026 import is_dead_keyword
    _HAS_DEAD = True
except ImportError:
    _HAS_DEAD = False
    def is_dead_keyword(kw: str) -> bool:
        return False

try:
    from .fb_fitness import _policy_risk
    _HAS_POLICY = True
except ImportError:
    _HAS_POLICY = False

log = get_logger(__name__)

# Module-level instances (replaces v7 globals, pickle-safe)
_text_analyzer = AdTextAnalyzer()
_keyword_enricher = KeywordEnricher()
_opportunity_analyzer = ProductOpportunityAnalyzer()


def _fb_policy_flag(keyword: str, title: str = '', body: str = '') -> str:
    """Early-stage FB policy risk detection. Returns flag string or ''."""
    if _HAS_DEAD and is_dead_keyword(keyword):
        return 'dead-keyword'
    if _HAS_POLICY:
        _, tags = _policy_risk(keyword, title, body)
        if tags:
            return 'policy-risk'
    return ''


def save_scan_result(keyword: str, raw_ads: List[dict], source_raw: Optional[dict] = None,
                     apply_side_effects: bool = True) -> Tuple[str, List[dict], dict]:
    """v7.1 + v8: 归一化→文本分析→关键词增强→品牌去重→产品机会→增强JSON+兔子洞.

    当 apply_side_effects=False 时 (并发 worker 用), 只写 _corrected.json,
    返回的 side_effects 由主进程统一落盘 SCAN_FILE 和 LEARNED_FILE.
    """
    today = datetime.now().strftime('%Y-%m-%d')
    now   = datetime.now().strftime('%H:%M')

    ads_data = [normalize_ad(a) for a in raw_ads]

    brand_stats: Dict[str, dict] = {}
    for ad in ads_data:
        name = ad['page_name']
        url  = ad['link_url']
        domain = extract_domain_from_url(url) or resolve_domain(name)
        if domain and not validate_domain(domain):
            domain = ''

        if name not in brand_stats:
            brand_stats[name] = {
                'count': 0, 'domain': domain, 'url': url,
                'likes': ad.get('page_likes') or 0,
                'page_id': ad['page_id'],
                'page_profile_uri': ad['page_profile_uri'],
                'ad_age': calculate_ad_age(ad.get('start_date')),
                'ctas': [], 'formats': [],
                'bodies': [], 'titles': [], 'link_descs': [],
                'snapshot_urls': [],
                'platforms_set': set(),
                'any_active': False,
                'variant_total': 0,
                'spend_lower': 0, 'spend_upper': 0,
                'imp_lower': 0, 'imp_upper': 0,
                'currency': '',
                'target_locations': [],
                'target_ages': '',
                'target_genders': '',
                'media_types': [],
                'same_domain_brands': [],
            }
        bs = brand_stats[name]
        bs['count'] += 1
        if domain and not bs['domain']:
            bs['domain'] = domain
        if url and not bs['url']:
            bs['url'] = url
        if ad.get('cta'):
            bs['ctas'].append(ad['cta'])
        if ad.get('format'):
            bs['formats'].append(ad['format'])
        if ad.get('body'):
            bs['bodies'].append(ad['body'])
        if ad.get('title'):
            bs['titles'].append(ad['title'])
        if ad.get('link_description'):
            bs['link_descs'].append(ad['link_description'])
        if ad.get('ad_snapshot_url'):
            bs['snapshot_urls'].append(ad['ad_snapshot_url'])
        for p in (ad.get('platforms') or []):
            bs['platforms_set'].add(p)
        if ad.get('is_active'):
            bs['any_active'] = True
        bs['variant_total'] += ad.get('variants') or 0
        if ad.get('spend') and isinstance(ad['spend'], dict):
            bs['spend_lower'] += (ad['spend'].get('lower') or 0)
            bs['spend_upper'] += (ad['spend'].get('upper') or 0)
            if ad.get('currency'):
                bs['currency'] = ad['currency']
        if ad.get('impressions') and isinstance(ad['impressions'], dict):
            bs['imp_lower'] += (ad['impressions'].get('lower') or 0)
            bs['imp_upper'] += (ad['impressions'].get('upper') or 0)
        if ad.get('target_locations'):
            bs['target_locations'] = ad['target_locations']
        if ad.get('target_ages'):
            bs['target_ages'] = ad['target_ages']
        if ad.get('target_genders'):
            bs['target_genders'] = ad['target_genders']
        if ad.get('video_url'):
            bs['media_types'].append('VIDEO')
        elif ad.get('image_url'):
            bs['media_types'].append('IMAGE')
        elif ad.get('format'):
            bs['media_types'].append(ad['format'])

    brand_stats = _resolve_brand_identity(brand_stats)

    competitors = []
    all_extracted = defaultdict(list)

    for name, stats in sorted(brand_stats.items(), key=lambda x: -x[1]['count'])[:20]:
        domain = stats['domain'] or resolve_domain(name)
        age = stats['ad_age']
        brand_score = calculate_brand_score_v7(stats)

        combined_title = ' '.join(stats['titles'])
        combined_body  = ' '.join(stats['bodies'])
        combined_desc  = ' '.join(stats['link_descs'])
        text_result = _text_analyzer.analyze_text(combined_title, combined_body, combined_desc)

        if not any(text_result.get(k) for k in ['pain_keywords', 'scene_keywords', 'product_keywords']):
            kw_enrich = _keyword_enricher.enrich_from_keyword(keyword)
            text_result = KeywordEnricher.merge_enrichments(text_result, kw_enrich)

        for cat, kws in text_result.items():
            all_extracted[cat].extend(kws)

        platform_dist = dict(Counter(stats['platforms_set']))
        media_mix = dict(Counter(stats['media_types']))

        spend_est = {}
        if stats['spend_lower'] or stats['spend_upper']:
            cur = stats['currency'] or 'USD'
            spend_est = {'lower': stats['spend_lower'], 'upper': stats['spend_upper'], 'currency': cur}

        imp_est = {}
        if stats['imp_lower'] or stats['imp_upper']:
            imp_est = {'lower': stats['imp_lower'], 'upper': stats['imp_upper']}

        target_profile = {}
        if stats['target_locations']:
            target_profile['locations'] = stats['target_locations'][:5]
        if stats['target_ages']:
            target_profile['ages'] = stats['target_ages']
        if stats['target_genders']:
            target_profile['genders'] = stats['target_genders']

        competitors.append({
            'brand':              name or UNKNOWN,
            'likes':              stats['likes'] or 0,
            'ads':                stats['count'] or 0,
            'landing':            domain or DASH,
            'landing_url':        stats['url'] or DASH,
            'ad_age_days':        age or 0,
            'ad_age_category':    '爆品' if age >= 90 else '验证中' if age >= 30 else '测试中',
            'top_cta':            Counter(stats['ctas']).most_common(1)[0][0] if stats['ctas'] else NONE_LABEL,
            'formats':            list(set(stats['formats'])) or [],
            'brand_score':        brand_score or 0,
            'page_id':            stats['page_id'] or '',
            'page_profile_uri':   stats['page_profile_uri'] or '',
            'ad_snapshot_urls':   stats['snapshot_urls'][:3] or [],
            'platforms':          platform_dist or {},
            'spend_estimate':     spend_est,
            'impressions_estimate': imp_est,
            'target_profile':     target_profile or {},
            'is_active':          stats['any_active'],
            'media_mix':          media_mix or {},
            'avg_body_length':    round(sum(len(b) for b in stats['bodies']) / max(len(stats['bodies']), 1)),
            'text_analysis': {
                'top_pain_keywords':   text_result['pain_keywords'][:5] or [],
                'top_scene_keywords':  text_result['scene_keywords'][:5] or [],
                'top_value_keywords':  text_result['value_keywords'][:5] or [],
                'hook_patterns':       text_result['hook_patterns'][:3] or [],
                'price_signals':       text_result['price_signals'][:3] or [],
                'authority_signals':   text_result['authority_signals'][:3] or [],
                'top_product_keywords': text_result['product_keywords'][:5] or [],
            },
            'commercial_signals':  extract_signals(' '.join(stats['bodies'] + stats['titles'] + stats['link_descs'])),
            'related_brands':     stats['same_domain_brands'] or [],
            'fb_policy_flag':     _fb_policy_flag(keyword, combined_title, combined_body),
        })

    market_trend = analyze_market_trend(ads_data)
    cta_patterns = extract_cta_patterns(ads_data)
    avg_likes = sum(c['likes'] for c in competitors) / len(competitors) if competitors else 0

    extracted_keywords = {}
    for cat, kws in all_extracted.items():
        deduped = list(dict.fromkeys(kw.lower().strip() for kw in kws if len(kw) > 2))
        extracted_keywords[cat] = deduped[:20]

    kw_global_enrich = _keyword_enricher.enrich_from_keyword(keyword)
    for cat, kws in kw_global_enrich.items():
        if cat.startswith('_'):
            continue
        if cat not in extracted_keywords:
            extracted_keywords[cat] = []
        for kw_item in kws:
            kw_lower = kw_item.lower().strip()
            if kw_lower and kw_lower not in extracted_keywords[cat]:
                extracted_keywords[cat].append(kw_lower)

    for c in competitors:
        url = c.get('landing_url', '')
        if url:
            path = urlparse(url).path
            for segment in re.findall(r'/(?:products?|collections?)/([a-z0-9-]+)', path.lower()):
                product_name = segment.replace('-', ' ').strip()
                if len(product_name) > 3:
                    extracted_keywords.setdefault('product_keywords', [])
                    if product_name not in extracted_keywords['product_keywords']:
                        extracted_keywords['product_keywords'].append(product_name)

    recovered_learned = []
    if source_raw and isinstance(source_raw, dict):
        learned_arr = source_raw.get('learned', [])
        if learned_arr and isinstance(learned_arr, list):
            recovered_learned = learned_arr
            learned_enrich = _keyword_enricher.enrich_from_learned_array(learned_arr)
            for cat, kws in learned_enrich.items():
                if cat not in extracted_keywords:
                    extracted_keywords[cat] = []
                for kw_item in kws:
                    if kw_item and kw_item.lower() not in extracted_keywords[cat]:
                        extracted_keywords[cat].append(kw_item.lower())

    for cat in extracted_keywords:
        extracted_keywords[cat] = extracted_keywords[cat][:20]

    all_scanned_kws = set()
    if SCAN_FILE.exists():
        with open(SCAN_FILE, encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                if parts:
                    all_scanned_kws.add(parts[0].strip().lower().lstrip('#').strip())
    product_opportunities = _opportunity_analyzer.analyze_opportunities(keyword, competitors, all_scanned_kws)

    trend_est = estimate_trend(keyword, competitors, len(brand_stats))

    overall_platforms = Counter()
    overall_formats = Counter()
    active_count = 0
    for c in competitors:
        for p, cnt in c.get('platforms', {}).items():
            overall_platforms[p] += 1
        for f in c.get('formats', []):
            overall_formats[f] += 1
        if c.get('is_active'):
            active_count += 1

    rabbit_suggestions = []
    for cat in ['product_keywords', 'pain_keywords', 'scene_keywords']:
        for kw in extracted_keywords.get(cat, [])[:5]:
            if len(kw) > 5:
                rabbit_suggestions.append(kw)

    # 数据完整度评分: 有多少顶级竞品具备 landing/likes/ad_age/text_analysis
    def _dq_score():
        if not competitors:
            return 0
        checks = 0
        total = 0
        for c in competitors[:10]:
            for k in ('landing', 'likes', 'ad_age_days'):
                total += 1
                if c.get(k):
                    checks += 1
            ta = c.get('text_analysis') or {}
            total += 1
            if ta.get('top_pain_keywords') or ta.get('top_scene_keywords'):
                checks += 1
        return int(round(checks / max(total, 1) * 100))

    dq_pct = _dq_score()
    dq_level = 'high' if dq_pct >= 70 else 'medium' if dq_pct >= 40 else 'low'

    result = {
        'keyword':    keyword,
        'time':       f'{today} {now}',
        'count':      len(ads_data),
        'ads':        len(ads_data),
        'brands':     len(brand_stats),
        'scan_version': '9.0',
        'top_competitors': competitors,
        'market_analysis': {
            'competition_level': categorize_competition_level(len(competitors), avg_likes),
            'avg_brand_likes':   round(avg_likes),
            'market_trend':      market_trend,
            'top_cta_patterns':  cta_patterns,
            'platform_distribution': dict(overall_platforms.most_common(10)),
            'format_distribution':   dict(overall_formats.most_common(10)),
            'active_rate':       round(active_count / max(len(competitors), 1) * 100, 1),
            'trend_estimate':    trend_est,
        },
        'extracted_keywords': extracted_keywords,
        'product_opportunities': product_opportunities,
        'rabbit_hole': {
            'suggested_keywords': rabbit_suggestions[:15],
        },
        'learned': recovered_learned,
        'metadata': {
            'scan_version': '9.0',
            'data_quality': dq_level,
            'data_quality_pct': dq_pct,
        }
    }

    # Per-file output — safe to write inside worker
    safe = re.sub(r'[\\/:*?"<>|]', '_', keyword.replace(' ', '-').replace('+', 'plus'))
    filepath = RESULTS_DIR / f'{today}_{safe}_corrected.json'
    with open(filepath, 'wb') as f:
        f.write(json.dumps(result, indent=2, ensure_ascii=False).encode('utf-8', errors='replace'))

    # Prepare side effects for main-process application
    new_learned = [f"{n}|{n}|brand|竞品|{keyword}|{today}" for n in list(brand_stats)[:5]]
    for pain_kw in extracted_keywords.get('pain_keywords', [])[:5]:
        new_learned.append(f"{pain_kw}|{pain_kw}|pain|痛点词|{keyword}|{today}")
    for scene_kw in extracted_keywords.get('scene_keywords', [])[:3]:
        new_learned.append(f"{scene_kw}|{scene_kw}|scene|场景词|{keyword}|{today}")
    for prod_kw in extracted_keywords.get('product_keywords', [])[:3]:
        new_learned.append(f"{prod_kw}|{prod_kw}|product|产品词|{keyword}|{today}")

    side_effects = {
        'keyword': keyword,
        'today': today,
        'learned_entries': new_learned,
        'ads_count': len(ads_data),
        'brands_count': len(brand_stats),
        'corrected_file': str(filepath),
        # v9.1: 把 ad-level rich payload 一并带回主进程, apply_scan_side_effects
        # 会把它们写进 ad_archive 表 (insights_v3 / V2 dashboard 依赖)
        'raw_ads': raw_ads,
    }

    if apply_side_effects:
        apply_scan_side_effects([side_effects])

    return str(filepath), competitors, side_effects


def apply_scan_side_effects(payloads: List[dict]) -> None:
    """Apply SCAN_FILE and LEARNED_FILE updates serially in main process."""
    if not payloads:
        return

    # SCAN_FILE: mark all payload keywords as scanned in one read-modify-write
    if SCAN_FILE.exists():
        with open(SCAN_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        new_lines = []
        keywords_by_date = [(p['keyword'], p['today']) for p in payloads]
        for line in lines:
            matched = False
            if not line.startswith('#') and '已扫描' not in line:
                for kw, today in keywords_by_date:
                    if f'{kw}|' in line:
                        new_lines.append(f'# {line.strip()}  ← 已扫描 {today}\n')
                        matched = True
                        break
            if not matched:
                new_lines.append(line)
        with open(SCAN_FILE, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

    # LEARNED_FILE: append all entries
    with open(LEARNED_FILE, 'a', encoding='utf-8') as f:
        for p in payloads:
            f.write(f"\n# === 从 {p['keyword']} 搜索学习 ({p['today']}) ===\n")
            for kw in p['learned_entries']:
                f.write(f'{kw}\n')

    # SQLite 镜像 — 容错, 不阻塞主流程
    try:
        from .persistence import get_persistence
        p_db = get_persistence()
        for p in payloads:
            p_db.upsert_scan(
                keyword=p['keyword'], scan_date=p['today'],
                ads=p.get('ads_count'), brands=p.get('brands_count'),
                corrected_file=p.get('corrected_file'),
            )
            learned_rows = []
            for entry in p['learned_entries']:
                parts = entry.split('|')
                if len(parts) >= 6:
                    learned_rows.append({
                        'keyword': parts[0].strip(),
                        'kw_type': parts[2].strip(),
                        'subtype': parts[3].strip(),
                        'source_kw': parts[4].strip(),
                        'learned_date': parts[5].strip(),
                    })
            if learned_rows:
                p_db.upsert_learned(learned_rows)
            # v9.1: ad-level archive
            raw_ads = p.get('raw_ads') or []
            if raw_ads:
                # v9.4: 先 enrich (补 ad_snapshot_url / publisher_platforms 推断 /
                # cards 重建 / body fallback / ig_handle 推断), 再 resolve domain
                from .ad_enricher import enrich_ads
                from .domain import resolve_domain, extract_domain_from_url, validate_domain
                from .domain import extract_domain_from_caption, extract_domain_from_page_name, is_social_or_redirect_domain
                raw_ads, enrich_stats = enrich_ads(raw_ads)
                if enrich_stats:
                    log.debug(f"ad_enricher [{p['keyword']}]: " + ", ".join(
                        f"{k}={v}" for k, v in sorted(enrich_stats.items(), key=lambda kv: -kv[1])
                    ))
                enriched = []
                for ad in raw_ads:
                    if not isinstance(ad, dict):
                        continue
                    d = ad.get('domain') or ''
                    if d and validate_domain(d) and not is_social_or_redirect_domain(d):
                        pass  # already has valid merchant domain
                    else:
                        # Try link_url first (skip social/redirect)
                        url = ad.get('link_url') or ad.get('landing_url') or ''
                        d2 = extract_domain_from_url(url) if url else ''
                        if d2 and validate_domain(d2) and not is_social_or_redirect_domain(d2):
                            d = d2
                        else:
                            # Try link_caption — often contains real domain like "brand.com"
                            d3 = extract_domain_from_caption(ad.get('link_caption') or '')
                            if d3 and validate_domain(d3):
                                d = d3
                            else:
                                # Try page_name — sometimes "BrandName.com"
                                d4 = extract_domain_from_page_name(ad.get('page_name') or '')
                                if d4 and validate_domain(d4):
                                    d = d4
                                else:
                                    # Fallback: resolve via KNOWN_DOMAINS or heuristic
                                    d5 = resolve_domain(ad.get('page_name') or '') or ''
                                    d = d5 if (d5 and validate_domain(d5) and not is_social_or_redirect_domain(d5)) else ''
                    ad['domain'] = d or None
                    enriched.append(ad)
                try:
                    n_ads = p_db.upsert_ad_archive_bulk(
                        enriched, keyword=p['keyword'], scan_date=p['today']
                    )
                    log.debug(f"ad_archive upsert: {n_ads} rows for keyword={p['keyword']}")
                except Exception as e:
                    log.warning(f"ad_archive upsert failed for {p['keyword']}: {e}")
    except Exception as e:
        log.warning(f"SQLite 镜像失败: {e}")

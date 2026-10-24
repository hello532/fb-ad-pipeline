"""scoring.py — extracted from fb-scan-pipeline-v7.py:923-971"""
import copy
from collections import defaultdict
from typing import Dict
from .domain import resolve_domain, extract_domain_from_url, canonical_domain

from core.logger import get_logger
log = get_logger(__name__)

# ── 品牌评分 v7 ──────────────────────────────────────────────────────────────

def calculate_brand_score_v7(stats: dict) -> int:
    score = 0
    score += min((stats.get('count') or 0) * 8, 30)
    score += min((stats.get('likes') or 0) // 5000, 15)
    score += min((stats.get('ad_age') or 0) // 10, 20)
    score += min(len(stats.get('platforms_set') or set()) * 5, 15)
    score += 10 if stats.get('any_active') else 0
    score += min((stats.get('variant_total') or 0) * 2, 10)
    return min(score, 100)

# ── 品牌去重 ─────────────────────────────────────────────────────────────────

def _resolve_brand_identity(brand_stats: dict) -> dict:
    """按落地页域名合并同一品牌的多个主页名"""
    domain_groups = defaultdict(list)
    for name, stats in brand_stats.items():
        domain = stats.get('domain') or resolve_domain(name)
        canon = canonical_domain(domain) if domain else name.lower()
        domain_groups[canon].append((name, stats))

    merged = {}
    for canon, group in domain_groups.items():
        if len(group) == 1:
            merged[group[0][0]] = group[0][1]
            merged[group[0][0]]['same_domain_brands'] = []
        else:
            group.sort(key=lambda x: -x[1].get('likes', 0))
            primary_name = group[0][0]
            primary = copy.deepcopy(group[0][1])
            primary['same_domain_brands'] = [n for n, _ in group[1:]]
            for _, other in group[1:]:
                primary['count'] = primary.get('count', 0) + other.get('count', 0)
                for list_key in ('ctas', 'formats', 'bodies', 'titles', 'link_descs', 'snapshot_urls'):
                    primary.setdefault(list_key, []).extend(other.get(list_key, []))
                primary['platforms_set'] = primary.get('platforms_set', set()) | other.get('platforms_set', set())
                primary['variant_total'] = primary.get('variant_total', 0) + other.get('variant_total', 0)
                if other.get('any_active'):
                    primary['any_active'] = True
                # 花费累加
                for field in ['spend_lower', 'spend_upper', 'imp_lower', 'imp_upper']:
                    primary[field] = primary.get(field, 0) + other.get(field, 0)
            merged[primary_name] = primary
    return merged

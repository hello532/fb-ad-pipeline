"""utils.py — extracted from fb-scan-pipeline-v7.py:152-293"""
import re
from datetime import datetime
from typing import List
from collections import Counter
from .domain import resolve_domain, extract_domain_from_url, canonical_domain, validate_domain
from .defaults import DASH, UNKNOWN, NONE_LABEL, safe_str, safe_int, nn

from core.logger import get_logger
log = get_logger(__name__)

# ── 工具函数 ───────────────────────────────────────────────────────────────────

def _safe_int(val, default=0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default

def normalize_ad(raw: dict) -> dict:
    """v9.5: 归一化浏览器JSON到统一格式. 保留所有富字段, 不丢数据."""
    return {
        # 核心身份
        'id':               raw.get('ad_archive_id') or raw.get('id', '') or '',
        'page_name':        safe_str(raw.get('page_name') or raw.get('page'), UNKNOWN),
        'page_id':          raw.get('page_id', '') or '',
        'page_profile_uri': raw.get('page_profile_uri', '') or '',
        'page_profile_picture_url': raw.get('page_profile_picture_url', '') or '',
        'page_likes':       safe_int(raw.get('page_like_count') or raw.get('page_likes') or raw.get('fans', 0)),
        'page_categories':  raw.get('page_categories', []) or [],
        # 日期和状态
        'start_date':       raw.get('start_date', 0),
        'end_date':         raw.get('end_date') or raw.get('ad_delivery_stop_time', '') or '',
        'is_active':        raw.get('is_active', True),
        # 创意内容
        'display_format':   raw.get('display_format') or raw.get('format', '') or '',
        'format':           raw.get('display_format') or raw.get('format', '') or '',
        'archive_type':     raw.get('archive_type', '') or '',
        'cta':              raw.get('cta_type') or raw.get('cta', '') or '',
        'cta_type':         raw.get('cta_type') or raw.get('cta', '') or '',
        'cta_text':         raw.get('cta_text', '') or '',
        'link_url':         raw.get('link_url') or raw.get('landing_url', '') or '',
        'link_description': raw.get('link_description', '') or '',
        'link_caption':     raw.get('link_caption', '') or '',
        'title':            raw.get('title', '') or '',
        'body':             raw.get('body', '') or '',
        'byline':           raw.get('byline', '') or '',
        'ad_snapshot_url':  raw.get('ad_snapshot_url', '') or '',
        'variants':         safe_int(raw.get('collation_count') or raw.get('variants', 1)),
        'collation_count':  safe_int(raw.get('collation_count') or raw.get('variants', 1)),
        # 轮播 / 卡片
        'cards':            raw.get('cards', []) or [],
        'cards_count':      safe_int(raw.get('cards_count') or 0),
        'carousel_images':  raw.get('carousel_images', []) or [],
        'carousel_videos':  raw.get('carousel_videos', []) or [],
        # 分发
        'platforms':        raw.get('publisher_platforms') or raw.get('platforms', []) or [],
        'publisher_platforms': raw.get('publisher_platforms') or raw.get('platforms', []) or [],
        # IG
        'ig_handle':        raw.get('ig_handle') or raw.get('instagram_actor_name', '') or '',
        'ig_followers':      safe_int(raw.get('ig_followers') or raw.get('instagram_followers', 0)),
        # 预算
        'spend':            raw.get('spend') or {},
        'impressions':      raw.get('impressions') or {},
        'currency':         raw.get('currency', '') or '',
        # 受众
        'target_locations': raw.get('target_locations', []) or [],
        'target_ages':      raw.get('target_ages', '') or '',
        'target_genders':   raw.get('target_genders', '') or '',
        'languages':        raw.get('languages', []) or [],
        # EU 透明度 (DSA)
        'regional_distribution':    raw.get('regional_distribution') or raw.get('regional_distribution_raw', '') or '',
        'demographic_distribution': raw.get('demographic_distribution') or raw.get('demographic_distribution_raw', '') or '',
        'eu_total_reach':   safe_int(raw.get('eu_total_reach') or 0),
        'eu_political_ad':  raw.get('eu_political_ad', False),
        'funding_entity':   raw.get('funding_entity', '') or '',
        # 媒体
        'image_url':        raw.get('image_url') or raw.get('original_image_url', '') or '',
        'video_url':        raw.get('video_url') or raw.get('video_hd_url', '') or '',
        'video_duration_sec': raw.get('video_duration_sec') or 0,
        # 合规
        'disclaimer_label': raw.get('disclaimer_label', '') or '',
        # 活跃时长
        'total_active_time': raw.get('total_active_time') or 0,
    }

def calculate_ad_age(start_date_val) -> int:
    """同时处理 Unix 时间戳(int)、ISO日期字符串(str)、数字字符串"""
    if not start_date_val:
        return 0
    try:
        if isinstance(start_date_val, str) and start_date_val.isdigit():
            start_date_val = int(start_date_val)
        if isinstance(start_date_val, (int, float)) and start_date_val > 1_000_000:
            start = datetime.fromtimestamp(start_date_val)
        elif isinstance(start_date_val, str) and start_date_val:
            start = datetime.fromisoformat(start_date_val.replace('Z', '').split('T')[0])
        else:
            return 0
        return max(0, (datetime.now() - start).days)
    except Exception:
        return 0

def categorize_competition_level(brand_count: int, avg_likes: float) -> str:
    if brand_count >= 20 and avg_likes >= 50000:
        return "高竞争"
    elif brand_count >= 10 and avg_likes >= 10000:
        return "中竞争"
    return "低竞争"

def analyze_market_trend(ads_data: List[dict]) -> dict:
    if not ads_data:
        return {'new_entrants': 0, 'established_players': 0, 'avg_ad_age': 0, 'trend': UNKNOWN}
    new_e, estab, total_age = 0, 0, 0
    for ad in ads_data:
        age = calculate_ad_age(ad.get('start_date', 0))
        total_age += age
        if age <= 30:
            new_e += 1
        elif age >= 90:
            estab += 1
    avg_age = total_age / len(ads_data)
    if new_e > estab:
        trend = "新兴市场"
    elif estab > new_e * 2:
        trend = "成熟市场"
    else:
        trend = "稳定市场"
    return {'new_entrants': new_e, 'established_players': estab,
            'avg_ad_age': round(avg_age, 1), 'trend': trend}

def extract_cta_patterns(ads_data: List[dict]) -> List[dict]:
    cta_counter = Counter(
        ad.get('cta', '').strip() for ad in ads_data if ad.get('cta', '').strip()
    )
    n = len(ads_data) or 1
    return [
        {'cta': cta, 'count': cnt, 'percentage': round(cnt / n * 100, 1)}
        for cta, cnt in cta_counter.most_common(10)
    ]

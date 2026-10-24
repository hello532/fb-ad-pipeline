"""trend.py — extracted from fb-scan-pipeline-v7.py:854-921"""
import re
from datetime import datetime
from typing import List

from core.logger import get_logger
log = get_logger(__name__)

# ── 趋势估算 ─────────────────────────────────────────────────────────────────

SEASONAL_PATTERNS = {
    'back pain': {'peak': [1, 2, 9, 10], 'note': '新年决心+秋季运动'},
    'neck pain': {'peak': [1, 2, 9, 10], 'note': '冬季僵硬+开学季'},
    'heating pad': {'peak': [10, 11, 12, 1], 'note': '寒冷季节'},
    'cold therapy': {'peak': [5, 6, 7, 8], 'note': '夏季运动'},
    'compression': {'peak': [3, 4, 5, 9, 10], 'note': '马拉松季'},
    'massage gun': {'peak': [11, 12, 1], 'note': '送礼季'},
    'red light': {'peak': [1, 2, 3], 'note': '新年护肤'},
    'posture': {'peak': [1, 2, 9], 'note': '新年+开学'},
    'foot pain': {'peak': [5, 6, 7, 8], 'note': '夏季活动'},
    'knee': {'peak': [3, 4, 5, 9, 10], 'note': '运动季'},
    'sciatica': {'peak': [10, 11, 12, 1, 2], 'note': '冬季加重'},
    'arthritis': {'peak': [10, 11, 12, 1, 2], 'note': '寒冷加重'},
    'eye massager': {'peak': [11, 12], 'note': '送礼季'},
    'scalp massager': {'peak': [11, 12], 'note': '送礼季'},
}

def estimate_trend(keyword: str, competitors: list, brand_count: int) -> dict:
    """从广告数据和季节性参考推算趋势"""
    # 广告年龄分布
    ages = [int(c.get('ad_age_days') or 0) for c in competitors if isinstance(c, dict)]
    ages = [a for a in ages if a > 0]
    new_count = sum(1 for a in ages if 0 < a < 30)
    old_count = sum(1 for a in ages if a >= 90)
    avg_age = sum(ages) / max(len(ages), 1) if ages else 0

    # 趋势方向
    if new_count > old_count * 2 and new_count >= 3:
        direction = '上升趋势'
    elif old_count > new_count * 2 and old_count >= 3:
        direction = '成熟市场'
    elif brand_count >= 10:
        direction = '稳定市场'
    elif brand_count <= 3:
        direction = '新兴/小众'
    else:
        direction = '稳定市场'

    # 搜索量估算
    if brand_count >= 15:
        volume = '高搜索量'
    elif brand_count >= 6:
        volume = '中搜索量'
    else:
        volume = '低搜索量'

    # 季节性
    seasonal_note = ''
    kw_lower = keyword.lower()
    for pattern_kw, info in SEASONAL_PATTERNS.items():
        if pattern_kw in kw_lower:
            current_month = datetime.now().month
            is_peak = current_month in info['peak']
            seasonal_note = f"{'当前旺季' if is_peak else '非旺季'}: {info['note']}"
            break

    return {
        'trend_direction': direction,
        'volume_estimate': volume,
        'seasonal_note': seasonal_note or '全年稳定',
        'new_ads_ratio': round(new_count / max(len(ages), 1) * 100, 1) if ages else 0,
        'avg_ad_age': round(avg_age, 1),
    }

# 全局实例在 scan_result.py 中初始化, 避免循环依赖

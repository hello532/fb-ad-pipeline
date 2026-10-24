"""pipeline/quality.py — 数据质量门.

raw JSON 入 pipeline 之前先过这里. 空值率过高的文件直接跳过,
避免污染 brand_intelligence / learned_keywords.
"""
from __future__ import annotations

from typing import Optional

REQUIRED_FIELDS = [
    'page_name',
]
# 至少命中 N 个"丰富内容"字段
RICH_FIELDS = [
    'body', 'title', 'link_description',
    'cta_type', 'display_format',
    'publisher_platforms', 'image_url', 'video_url',
]


def ad_quality_score(ad: dict) -> float:
    """0.0 - 1.0, 越高越完整."""
    if not isinstance(ad, dict):
        return 0.0
    score = 0.0
    # required 字段 — 任一缺失直接 0
    for f in REQUIRED_FIELDS:
        v = ad.get(f)
        if not v or (isinstance(v, str) and not v.strip()):
            return 0.0
    # rich 字段 — 按比例给分
    rich_hit = 0
    for f in RICH_FIELDS:
        v = ad.get(f)
        if v and (not isinstance(v, (list, str)) or len(v) > 0):
            rich_hit += 1
    score += rich_hit / len(RICH_FIELDS)
    return min(score, 1.0)


def filter_ads(ads: list[dict], min_quality: float = 0.35) -> tuple[list[dict], int]:
    """过滤质量不足的 ad. 返回 (passed, rejected_count)."""
    passed = []
    rejected = 0
    for ad in ads:
        if ad_quality_score(ad) >= min_quality:
            passed.append(ad)
        else:
            rejected += 1
    return passed, rejected


def scan_file_worth_processing(ads: list[dict], min_count: int = 1,
                                min_avg_quality: float = 0.2) -> Optional[str]:
    """判断整个文件是否值得处理. 返回 None 通过, 否则返回拒绝原因."""
    if not ads:
        return "空广告列表"
    if len(ads) < min_count:
        return f"广告数 {len(ads)} < {min_count}"
    scores = [ad_quality_score(a) for a in ads]
    avg = sum(scores) / len(scores)
    if avg < min_avg_quality:
        return f"平均质量 {avg:.2f} < {min_avg_quality}"
    return None

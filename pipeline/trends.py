"""pipeline/trends.py — 品牌投放趋势 / 动能计算.

扫 RESULTS_DIR 下的 *_corrected.json, 聚合每个品牌按扫描日期的广告量,
计算 7 天动能 + 14 天对比 + sparkline 数据.
用于 TOP5 精选推荐的专业判断.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.logger import get_logger

from .paths import RESULTS_DIR

log = get_logger(__name__)

_cache: dict[str, Any] = {'series': None, 'built_at': None}
_CACHE_TTL_SEC = 600  # 10 分钟


def _build_series() -> dict[str, dict[str, int]]:
    """返回 {domain: {YYYY-MM-DD: max_ad_count_in_that_day}}."""
    now = datetime.now()
    if _cache['series'] and _cache['built_at'] and (now - _cache['built_at']).total_seconds() < _CACHE_TTL_SEC:
        return _cache['series']

    series: dict[str, dict[str, int]] = defaultdict(dict)
    cutoff = (now - timedelta(days=45)).strftime('%Y-%m-%d')
    for jf in RESULTS_DIR.glob('*_corrected.json'):
        if '汇总' in jf.name:
            continue
        # 文件名通常是 2026-04-18_keyword_corrected.json
        date_part = jf.name[:10]
        if not date_part.startswith('2') or date_part < cutoff:
            continue
        try:
            data = json.loads(jf.read_text(encoding='utf-8'))
        except Exception:
            continue
        # 可能 corrected 里的 time 更精确
        t = data.get('time', '')
        d = t.split(' ')[0] if t else date_part
        for c in data.get('top_competitors', []):
            if not isinstance(c, dict):
                continue
            dom = c.get('landing', '')
            ads = int(c.get('ads', 0) or 0)
            if dom and ads > 0:
                # 同一天多次扫到: 取最大
                if ads > series[dom].get(d, 0):
                    series[dom][d] = ads

    _cache['series'] = dict(series)
    _cache['built_at'] = now
    return _cache['series']


def brand_momentum(domain: str) -> dict[str, Any]:
    """返回 {sparkline, ads_7d, ads_prior_7d, momentum_pct, trend_direction, days_observed}."""
    series = _build_series()
    s = series.get(domain, {})
    if not s:
        return {'sparkline': [], 'ads_7d': 0, 'ads_prior_7d': 0,
                'momentum_pct': 0, 'trend_direction': 'unknown', 'days_observed': 0}

    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    # 最近 14 天 sparkline
    spark = []
    for i in range(13, -1, -1):
        d = (now - timedelta(days=i)).strftime('%Y-%m-%d')
        spark.append(s.get(d, 0))

    ads_7d = sum(spark[7:])
    ads_prior_7d = sum(spark[:7])
    if ads_prior_7d == 0:
        momentum_pct = 100 if ads_7d > 0 else 0
    else:
        momentum_pct = round((ads_7d - ads_prior_7d) / ads_prior_7d * 100)

    if momentum_pct >= 30:
        direction = '🚀 加速'
    elif momentum_pct >= 10:
        direction = '📈 上升'
    elif momentum_pct >= -10:
        direction = '➡️ 稳定'
    elif momentum_pct >= -30:
        direction = '📉 放缓'
    else:
        direction = '⬇️ 撤退'

    return {
        'sparkline': spark,
        'ads_7d': ads_7d,
        'ads_prior_7d': ads_prior_7d,
        'momentum_pct': momentum_pct,
        'trend_direction': direction,
        'days_observed': sum(1 for v in spark if v > 0),
    }


def top_recommendations(n: int = 5, min_score: int = 70,
                       only_shopify: bool = True) -> list[dict]:
    """TOP N 精选推荐 + 趋势数据. 用组合分排序:
    combined = (score × 0.4) + (signal_score × 1.5) + (expert_score × 3) + momentum_adjustment
    """
    from .recommendations import list_recommendations
    from .insights import compute_insights
    from .playbook import analyze as playbook_analyze

    # 拉 top 100 候选(只要高分, 有 signal 优先, shopify)
    candidates = list_recommendations(limit=200, min_score=min_score,
                                       only_shopify=only_shopify, days=0)

    for r in candidates:
        try:
            ins = compute_insights(r)
            r['expert_score'] = ins.get('expert_score', 0)
            r['insight_tags'] = ins.get('tags', [])
            r['verdict'] = ins.get('verdict', '')
        except Exception:
            r['expert_score'] = 0
            r['insight_tags'] = []
            r['verdict'] = ''
        try:
            pb = playbook_analyze(r)
            r.update(pb)
        except Exception:
            pass
        try:
            mom = brand_momentum(r['domain'])
            r.update(mom)
        except Exception:
            r['momentum_pct'] = 0
            r['trend_direction'] = 'unknown'
            r['sparkline'] = []

    # 组合分排序
    def combined(r):
        s = (r.get('score', 0) * 0.4) + (r.get('signal_score', 0) * 1.5) + (r.get('expert_score', 0) * 3)
        # 加速品牌加分, 撤退品牌减分
        mp = r.get('momentum_pct', 0)
        if mp >= 30: s += 15
        elif mp >= 10: s += 8
        elif mp <= -30: s -= 15
        elif mp <= -10: s -= 8
        # 有稳定 data_points > 3 天的加分 (有持续投放证据)
        if r.get('days_observed', 0) >= 3:
            s += 5
        return s

    candidates.sort(key=lambda r: -combined(r))
    top = candidates[:n]
    for r in top:
        r['combined_score'] = round(combined(r), 1)
    return top

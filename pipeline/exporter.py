"""exporter.py — V9.0 HTML / Excel 导出器.

- export_html_report(): 生成自包含单文件 HTML (无外部依赖, 可邮件/归档)
- xlsx file path 直接用 EXCEL_FILE (已由 excel_writer 生成), 由 FastAPI StreamingResponse 下载
"""
from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Iterable, Optional

from core.logger import get_logger

from . import __version__, RELEASE_NAME
from .paths import RESULTS_DIR, EXCEL_FILE
from .defaults import (
    DASH, NONE_LABEL, UNKNOWN,
    safe_str, safe_int, safe_pct, safe_money, safe_date,
    safe_list_join, safe_dict_keys_join, completeness_score,
)

log = get_logger(__name__)


def _load_recent_scans(limit: int = 50) -> list[dict]:
    """读取最新的 corrected.json (按 mtime 排序, 取 limit 个)."""
    files = sorted(
        (f for f in RESULTS_DIR.glob('*_corrected.json') if '汇总' not in f.name),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]
    out = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
            out.append(data)
        except Exception:
            continue
    return out


def _td(v) -> str:
    return f"<td>{escape(str(v)) if v is not None else DASH}</td>"


def _row(*cells) -> str:
    return "<tr>" + "".join(_td(c) for c in cells) + "</tr>"


_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  margin:0;background:#0f1419;color:#e0e3e8;padding:20px;}
h1{font-size:22px;margin:0 0 4px 0;color:#4a90ff;}
h2{font-size:16px;margin:28px 0 10px 0;color:#fbbf24;border-bottom:1px solid #2a3142;padding-bottom:6px;}
.meta{font-size:12px;color:#8a93a6;margin-bottom:20px;}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0;}
.card{background:#1a2030;padding:12px 16px;border-radius:8px;border:1px solid #2a3142;}
.card .v{font-size:20px;font-weight:700;color:#4ade80;}
.card .l{font-size:11px;color:#8a93a6;text-transform:uppercase;letter-spacing:0.8px;margin-top:3px;}
table{width:100%;border-collapse:collapse;font-size:12px;background:#141824;border-radius:6px;overflow:hidden;}
thead th{background:#2a3142;color:#d5d8de;padding:8px 10px;text-align:left;font-weight:600;position:sticky;top:0;}
tbody td{padding:7px 10px;border-top:1px solid #1e2433;color:#c4c8d0;vertical-align:top;}
tbody tr:hover{background:#1a2030;}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;}
.badge.high{background:#15803d;color:#fff;} .badge.med{background:#d97706;color:#fff;}
.badge.low{background:#6a7080;color:#fff;}
.foot{margin-top:30px;font-size:11px;color:#6a7080;text-align:center;border-top:1px solid #2a3142;padding-top:14px;}
a{color:#4a90ff;text-decoration:none;} a:hover{text-decoration:underline;}
"""


def _dq_badge(level: str) -> str:
    lvl = (level or UNKNOWN).lower()
    cls = 'high' if lvl == 'high' else 'med' if lvl == 'medium' else 'low'
    return f'<span class="badge {cls}">{escape(safe_str(level, UNKNOWN))}</span>'


def export_html_report(out_file: Optional[Path] = None, limit: int = 50) -> Path:
    """生成自包含 HTML 报告, 返回文件路径.
    若 out_file 为 None, 写到 RESULTS_DIR/report_YYYY-MM-DD_HHMM.html.
    """
    scans = _load_recent_scans(limit=limit)
    now = datetime.now()
    out_file = out_file or (RESULTS_DIR / f"report_{now.strftime('%Y-%m-%d_%H%M')}.html")

    # 统计卡片
    total_scans = len(scans)
    total_brands = sum(s.get('brands', 0) for s in scans)
    total_ads = sum(s.get('ads', 0) for s in scans)
    high_q = sum(1 for s in scans if (s.get('metadata') or {}).get('data_quality') == 'high')

    html = [
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>",
        f"<title>FB Pipeline 报告 · {RELEASE_NAME}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>FB Ad Library Pipeline · {escape(RELEASE_NAME)} 报告</h1>",
        f"<div class='meta'>生成时间: {escape(safe_date(now.isoformat()))} "
        f"&nbsp;·&nbsp; 版本 v{escape(__version__)} "
        f"&nbsp;·&nbsp; 数据源: 最近 {total_scans} 次扫描</div>",
        "<div class='grid'>",
        f"<div class='card'><div class='v'>{total_scans}</div><div class='l'>扫描次数</div></div>",
        f"<div class='card'><div class='v'>{total_ads:,}</div><div class='l'>广告总量</div></div>",
        f"<div class='card'><div class='v'>{total_brands:,}</div><div class='l'>品牌总数</div></div>",
        f"<div class='card'><div class='v'>{high_q}</div><div class='l'>高质量扫描</div></div>",
        "</div>",
    ]

    # 关键词汇总表
    html.append("<h2>关键词汇总</h2>")
    html.append("<table><thead><tr>"
                "<th>关键词</th><th>扫描时间</th><th>广告</th><th>品牌</th>"
                "<th>竞争</th><th>趋势</th><th>活跃率</th><th>完整度</th>"
                "</tr></thead><tbody>")
    for s in scans:
        ma = s.get('market_analysis') or {}
        md = s.get('metadata') or {}
        dq = md.get('data_quality') or UNKNOWN
        dq_pct = md.get('data_quality_pct') or 0
        trend = safe_str(
            (ma.get('market_trend') or {}).get('trend') or
            (ma.get('trend_estimate') or {}).get('trend_direction'),
            UNKNOWN,
        )
        html.append(
            "<tr>"
            f"<td>{escape(safe_str(s.get('keyword'), UNKNOWN))}</td>"
            f"<td>{escape(safe_date(s.get('time')))}</td>"
            f"<td>{safe_int(s.get('ads'))}</td>"
            f"<td>{safe_int(s.get('brands'))}</td>"
            f"<td>{escape(safe_str(ma.get('competition_level'), UNKNOWN))}</td>"
            f"<td>{escape(trend)}</td>"
            f"<td>{escape(safe_pct(ma.get('active_rate')))}</td>"
            f"<td>{_dq_badge(dq)} {safe_int(dq_pct)}%</td>"
            "</tr>"
        )
    html.append("</tbody></table>")

    # Top 竞品表
    html.append("<h2>Top 竞品 (按品牌评分)</h2>")
    html.append("<table><thead><tr>"
                "<th>关键词</th><th>品牌</th><th>独立站</th><th>粉丝</th><th>广告数</th>"
                "<th>广告龄</th><th>CTA</th><th>评分</th><th>平台</th>"
                "</tr></thead><tbody>")
    all_comps: list[tuple] = []
    for s in scans:
        for c in (s.get('top_competitors') or [])[:5]:
            all_comps.append((s.get('keyword', ''), c))
    all_comps.sort(key=lambda x: -safe_int((x[1] or {}).get('brand_score')))
    for kw, c in all_comps[:100]:
        landing = safe_str(c.get('landing'), DASH)
        landing_html = (
            f'<a href="https://{escape(landing)}" target="_blank">{escape(landing)}</a>'
            if landing != DASH else DASH
        )
        html.append(
            "<tr>"
            f"<td>{escape(safe_str(kw, UNKNOWN))}</td>"
            f"<td>{escape(safe_str(c.get('brand'), UNKNOWN))}</td>"
            f"<td>{landing_html}</td>"
            f"<td>{safe_int(c.get('likes'))}</td>"
            f"<td>{safe_int(c.get('ads'))}</td>"
            f"<td>{safe_int(c.get('ad_age_days'))} 天</td>"
            f"<td>{escape(safe_str(c.get('top_cta'), DASH))}</td>"
            f"<td>{safe_int(c.get('brand_score'))}</td>"
            f"<td>{escape(safe_dict_keys_join(c.get('platforms'), limit=5))}</td>"
            "</tr>"
        )
    html.append("</tbody></table>")

    html.append(
        f"<div class='foot'>本报告由 FB Ad Library Pipeline {escape(__version__)} 生成 · "
        f"文件: {escape(out_file.name)}</div>"
        "</body></html>"
    )

    out_file.write_text("\n".join(html), encoding='utf-8')
    log.info(f"HTML 报告: {out_file}")
    return out_file

"""excel_writer.py — write_only 流式版, 支持 10M+ 量级.

策略:
1. 如果 EXCEL_FILE 已存在, 用 read_only 读出非托管 sheet 的数据(仅值, 丢格式)
2. 用 Workbook(write_only=True) 新建 workbook, 不保留任何样式
3. 迭代每个 _corrected.json 文件, 解析后立即写入对应 sheet (不蓄积)
4. 仅 header / landing 超链接 / 高分行高亮用 WriteOnlyCell 保留样式
5. FB_EXCEL_ROW_CAP (默认 50000) 控制每 sheet 最大行数
"""
import json, os, re
from datetime import datetime
from pathlib import Path
from typing import Tuple
from collections import Counter

from .paths import RESULTS_DIR, EXCEL_FILE
from .domain import canonical_domain, extract_domain_from_url, resolve_domain, validate_domain
from .scoring import calculate_brand_score_v7
from .utils import calculate_ad_age
from .keyword_enricher import KeywordEnricher
from .defaults import (
    DASH, NONE_LABEL, UNKNOWN,
    safe_str, safe_int, safe_pct, safe_money, safe_date,
    safe_list_join, safe_dict_keys_join, safe_dict_kv_join,
    is_empty, completeness_score,
)

from core.logger import get_logger
log = get_logger(__name__)

# v10: 清理 openpyxl 不接受的非法字符 (控制字符 0x00-0x1F 除了 \t \n \r)
import openpyxl.utils.cell
_ILLEGAL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _sanitize_for_excel(value):
    """Strip illegal XML characters that openpyxl rejects."""
    if isinstance(value, str):
        return _ILLEGAL_CHARS_RE.sub('', value)
    return value

_keyword_enricher = KeywordEnricher()

ROW_CAP = int(os.getenv('FB_EXCEL_ROW_CAP', '50000'))

# update_excel 托管的 sheet 名字(其他 sheet 由 update-keyword-library 脚本维护)
MANAGED_SHEETS = {
    '关键词汇总', '验证期产品(30-90天)', '长期爆品(90天+)',
    '测试期广告(<30天)', '扫描结果', '产品机会',
}


def update_excel() -> Tuple[int, int]:
    """v8: 逐文件流式写入, 内存恒定.

    不再蓄积 all_brands/all_keywords/all_opportunities 列表.
    每个 _corrected.json 解析后立即写入各 sheet 行.
    """
    try:
        from openpyxl import Workbook, load_workbook
        from openpyxl.styles import Font, PatternFill
        from openpyxl.cell import WriteOnlyCell
    except ImportError:
        log.info("错误: 需要安装 openpyxl")
        return 0, 0

    # ── 1. 读出非托管 sheet (仅值) ────────────────────────────────────────────
    preserved = []
    if EXCEL_FILE.exists():
        try:
            src = load_workbook(EXCEL_FILE, read_only=True, data_only=True)
            for sn in src.sheetnames:
                if sn in MANAGED_SHEETS:
                    continue
                ws = src[sn]
                rows = [list(r) for r in ws.iter_rows(values_only=True)]
                preserved.append({'name': sn, 'rows': rows})
            src.close()
        except Exception as e:
            log.info(f"警告: 读取旧 workbook 非托管 sheet 失败: {e}")

    # ── 2. 创建 workbook + 所有 sheet (带 header) ────────────────────────────
    wb = Workbook(write_only=True)

    # Sheet 0: 关键词汇总
    ws_kw = wb.create_sheet('关键词汇总')
    h0 = ['关键词', '搜索时间', '广告量', '品牌数', '新学词数', '竞争强度',
          '市场趋势', '季节性', '平台分布', '活跃率', '平均广告龄', '数据完整度']
    _write_header(ws_kw, h0, 'BF8F00')
    _set_col_widths(ws_kw, [25, 18, 8, 8, 8, 10, 18, 18, 25, 10, 10, 14])

    # Sheet 1-3: 时间分层品牌表
    brand_headers = [
        '关键词', '搜索时间', '广告量', '品牌', '粉丝数', '广告数',
        '独立站网址', '广告天数', '阶段', 'Top CTA', '平台',
        '花费估算', '目标受众', '媒体类型', '痛点词', '场景词',
        '品牌评分', '活跃', '关联品牌',
    ]
    brand_widths = [
        18, 16, 8, 25, 10, 8,
        30, 8, 10, 15, 20,
        15, 20, 15, 25, 25,
        8, 6, 20,
    ]

    ws_testing   = wb.create_sheet('测试期广告(<30天)')
    _write_header(ws_testing, brand_headers, '808080')
    _set_col_widths(ws_testing, brand_widths)

    ws_validated = wb.create_sheet('验证期产品(30-90天)')
    _write_header(ws_validated, brand_headers, '2F5496')
    _set_col_widths(ws_validated, brand_widths)

    ws_proven    = wb.create_sheet('长期爆品(90天+)')
    _write_header(ws_proven, brand_headers, 'BF8F00')
    _set_col_widths(ws_proven, brand_widths)

    # Sheet 4: 扫描结果(全部)
    ws_all = wb.create_sheet('扫描结果')
    _write_header(ws_all, brand_headers, '2F5496')
    _set_col_widths(ws_all, brand_widths)

    # Sheet 5: 产品机会
    ws_opp = wb.create_sheet('产品机会')
    opp_headers = ['来源关键词', '机会类型', '建议产品', '中文', '机会评分', '原因']
    opp_widths  = [25, 12, 35, 20, 10, 45]
    _write_header(ws_opp, opp_headers, '2E7D32')
    _set_col_widths(ws_opp, opp_widths)

    # ── 3. 逐文件流式写入 ────────────────────────────────────────────────────
    row_counts = Counter()  # per-sheet row counter for cap
    total_kw = 0
    total_brands = 0
    seen_brand_keys = set()

    corrected_files = sorted(
        (jf for jf in RESULTS_DIR.iterdir()
         if jf.name.endswith('_corrected.json') and '汇总' not in jf.name),
        key=lambda x: x.stat().st_mtime
    )

    for jf in corrected_files:
        try:
            with open(jf, encoding='utf-8') as f:
                raw = f.read()
            raw = re.sub(r',\s*}', '}', raw)
            raw = re.sub(r',\s*]', ']', raw)
            data = json.loads(raw)
        except Exception as e:
            log.info(f"警告: 无法解析 {jf.name}: {e}")
            continue

        kw     = data.get('keyword', '')
        time_s = data.get('time', data.get('scraped_at', ''))
        ads_val = data.get('ads', data.get('count', data.get('total_ads', 0)))
        ads    = len(ads_val) if isinstance(ads_val, list) else (ads_val or 0)
        brands = data.get('brands', 0)
        if not brands:
            comps = data.get('top_competitors', [])
            if comps and isinstance(comps, list):
                brands = len(comps)
            elif isinstance(ads_val, list):
                brands = len(set(
                    a.get('brand', a.get('page_name', ''))
                    for a in ads_val if isinstance(a, dict)
                ))
        market = data.get('market_analysis', {})

        competition = market.get('competition_level', '')
        if not competition or competition == '未知':
            if brands >= 15:    competition = '高竞争'
            elif brands >= 6:   competition = '中竞争'
            elif brands > 0:   competition = '低竞争'
            else:               competition = '未知'

        trend = market.get('market_trend', {}).get('trend', '')
        if not trend or trend in ('unknown', '未知', ''):
            te = market.get('trend_estimate', {})
            trend = te.get('trend_direction', '')
            if not trend:
                if brands >= 15:    trend = '成熟市场'
                elif brands >= 6:  trend = '稳定市场'
                elif brands > 0:   trend = '新兴/小众'
                else:              trend = '未知'

        te = market.get('trend_estimate', {})
        volume = te.get('volume_estimate', '')
        seasonal = te.get('seasonal_note', '')
        trend_full = trend
        if volume:
            trend_full += f' ({volume})'

        # ── 写关键词行 ──
        if row_counts['关键词汇总'] < ROW_CAP:
            q = safe_str((data.get('metadata') or {}).get('data_quality'), UNKNOWN)
            qp = safe_int((data.get('metadata') or {}).get('data_quality_pct'))
            ws_kw.append([
                safe_str(kw, UNKNOWN),
                safe_date(time_s),
                safe_int(ads),
                safe_int(brands),
                safe_int(len(data.get('learned') or [])),
                safe_str(competition, UNKNOWN),
                safe_str(trend_full, UNKNOWN),
                safe_str(seasonal, DASH),
                safe_dict_keys_join(market.get('platform_distribution'), limit=5),
                safe_pct(market.get('active_rate')),
                safe_str(market.get('market_trend', {}).get('avg_ad_age'), DASH),
                f"{q} {qp}%" if qp else q,
            ])
            row_counts['关键词汇总'] += 1
            total_kw += 1

        # ── 写品牌行 (直接分到 tier sheet + 扫描结果) ──
        for comp in data.get('top_competitors', []):
            if not isinstance(comp, dict):
                continue
            brand   = comp.get('brand', 'Unknown')
            landing = comp.get('landing', '') or resolve_domain(brand)
            bkey    = f"{kw}||{brand}"
            if bkey in seen_brand_keys:
                continue
            seen_brand_keys.add(bkey)

            platforms_str = safe_dict_keys_join(comp.get('platforms'), limit=5)
            spend_str = safe_money(comp.get('spend_estimate'))

            target_str = DASH
            tp = comp.get('target_profile') or {}
            if tp:
                parts = []
                if tp.get('ages'):
                    parts.append(f"Age:{safe_str(tp['ages'])}")
                if tp.get('genders'):
                    parts.append(f"G:{safe_str(tp['genders'])}")
                if tp.get('locations'):
                    locs = safe_list_join(tp['locations'], limit=3, default='')
                    if locs:
                        parts.append(f"Loc:{locs}")
                target_str = ' '.join(parts) if parts else DASH

            media_str = safe_dict_kv_join(comp.get('media_mix'))

            ta = comp.get('text_analysis') or {}
            pain_str  = safe_list_join(ta.get('top_pain_keywords'), limit=3, default='')
            scene_str = safe_list_join(ta.get('top_scene_keywords'), limit=3, default='')

            if ((not pain_str or pain_str == NONE_LABEL) or
                (not scene_str or scene_str == NONE_LABEL)) and kw:
                _kw_enrich = _keyword_enricher.enrich_from_keyword(kw) or {}
                if not pain_str or pain_str == NONE_LABEL:
                    pain_str = safe_list_join(_kw_enrich.get('pain_keywords'), limit=3, default='')
                if not scene_str or scene_str == NONE_LABEL:
                    scene_str = safe_list_join(_kw_enrich.get('scene_keywords'), limit=3, default='')

            pain_str = pain_str or NONE_LABEL
            scene_str = scene_str or NONE_LABEL
            ad_age = safe_int(comp.get('ad_age_days'))

            brand_row_vals = [
                safe_str(kw, UNKNOWN), safe_date(time_s), safe_int(ads),
                safe_str(brand, UNKNOWN),
                safe_int(comp.get('likes')) or DASH,
                safe_int(comp.get('ads')),
                safe_str(landing, DASH),
                ad_age,
                safe_str(comp.get('ad_age_category'), DASH),
                safe_str(comp.get('top_cta'), DASH),
                platforms_str, spend_str, target_str, media_str,
                pain_str, scene_str,
                safe_int(comp.get('brand_score')),
                '✓' if comp.get('is_active') else DASH,
                safe_list_join(comp.get('related_brands'), limit=5) or NONE_LABEL,
            ]

            # 写入扫描结果 (全量)
            if row_counts['扫描结果'] < ROW_CAP:
                _append_brand_row(ws_all, brand_row_vals, safe_str(landing, DASH), 0)
                row_counts['扫描结果'] += 1

            # 写入对应的 tier sheet
            if ad_age >= 90:
                sheet_name = '长期爆品(90天+)'
                target_ws = ws_proven
                highlight_score = 70
            elif ad_age >= 30:
                sheet_name = '验证期产品(30-90天)'
                target_ws = ws_validated
                highlight_score = 0
            else:
                sheet_name = '测试期广告(<30天)'
                target_ws = ws_testing
                highlight_score = 0

            if row_counts[sheet_name] < ROW_CAP:
                _append_brand_row(target_ws, brand_row_vals,
                                  safe_str(landing, DASH), highlight_score)
                row_counts[sheet_name] += 1

            total_brands += 1

        # ── 写产品机会行 ──
        po = data.get('product_opportunities', {})
        if po and row_counts['产品机会'] < ROW_CAP:
            for exp in po.get('product_expansions', [])[:5]:
                if row_counts['产品机会'] >= ROW_CAP:
                    break
                ws_opp.append([
                    safe_str(kw, UNKNOWN), '产品扩展',
                    safe_str(exp.get('keyword'), DASH),
                    safe_str(exp.get('cn'), DASH),
                    safe_int(exp.get('score')),
                    safe_str(exp.get('reason'), DASH),
                ])
                row_counts['产品机会'] += 1
            for upg in po.get('product_upgrades', [])[:5]:
                if row_counts['产品机会'] >= ROW_CAP:
                    break
                ws_opp.append([
                    safe_str(kw, UNKNOWN), '产品升级',
                    safe_str(upg.get('keyword'), DASH),
                    safe_str(upg.get('cn'), DASH),
                    safe_int(upg.get('score')),
                    safe_str(upg.get('reason'), DASH),
                ])
                row_counts['产品机会'] += 1
            for bundle in po.get('bundle_ideas', [])[:3]:
                if row_counts['产品机会'] >= ROW_CAP:
                    break
                kws = bundle.get('keywords') or []
                ws_opp.append([
                    safe_str(kw, UNKNOWN), '组合建议',
                    safe_list_join(kws, sep=' + ', default=DASH),
                    safe_str(bundle.get('cn'), DASH),
                    60,
                    safe_str(bundle.get('reason'), DASH),
                ])
                row_counts['产品机会'] += 1
            for niche in po.get('niche_opportunities', [])[:3]:
                if row_counts['产品机会'] >= ROW_CAP:
                    break
                ws_opp.append([
                    safe_str(kw, UNKNOWN), '蓝海市场',
                    safe_str(niche.get('keyword'), DASH),
                    DASH,
                    safe_int(niche.get('gap_score')),
                    safe_str(niche.get('reason'), DASH),
                ])
                row_counts['产品机会'] += 1
            for gap in po.get('market_gaps', [])[:3]:
                if row_counts['产品机会'] >= ROW_CAP:
                    break
                ws_opp.append([
                    safe_str(kw, UNKNOWN), '市场空白',
                    safe_str(gap.get('keyword'), DASH),
                    safe_str(gap.get('cn'), DASH),
                    50,
                    safe_str(gap.get('reason'), DASH),
                ])
                row_counts['产品机会'] += 1

    # ── 4. 回写非托管 sheet (原顺序, 末尾) ────────────────────────────────────
    for item in preserved:
        ws = wb.create_sheet(item['name'])
        for row in item['rows']:
            ws.append([_sanitize_for_excel(c) for c in row])

    wb.save(EXCEL_FILE)
    log.info(f"  ✅ 关键词: {total_kw}, 品牌: {total_brands}, 产品机会: {row_counts['产品机会']}")
    log.info(f"     测试期: {row_counts['测试期广告(<30天)']}, "
             f"验证期: {row_counts['验证期产品(30-90天)']}, "
             f"长期爆品: {row_counts['长期爆品(90天+)']}")
    if any(row_counts[s] >= ROW_CAP for s in MANAGED_SHEETS):
        log.info(f"     ⚠️  部分 sheet 达到行上限 {ROW_CAP}, 低分行已截断")
    if preserved:
        log.info(f"     保留非托管 sheet: {len(preserved)} 个 ({', '.join(p['name'] for p in preserved)})")
    return total_kw, total_brands


def _write_header(ws, headers: list, color: str):
    from openpyxl.styles import Font, PatternFill
    from openpyxl.cell import WriteOnlyCell
    fill = PatternFill(start_color=color, end_color=color, fill_type='solid')
    font = Font(bold=True, color='FFFFFF')
    row = []
    for h in headers:
        c = WriteOnlyCell(ws, value=h)
        c.fill = fill
        c.font = font
        row.append(c)
    ws.append(row)


def _set_col_widths(ws, widths: list):
    import openpyxl
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w


def _write_brand_sheet(wb, sheet_name: str, brands: list, headers: list,
                       widths: list, header_color: str,
                       highlight_score: int = 0):
    """Legacy helper — no longer used by streaming path, kept for compat."""
    ws = wb.create_sheet(sheet_name)
    _write_header(ws, headers, header_color)
    for b in brands:
        vals = [
            b.get('keyword', ''), b.get('time', ''), b.get('ads', 0),
            b.get('brand', ''), b.get('likes') or DASH,
            b.get('ads_count', 0), b.get('landing', DASH),
            b.get('ad_age', 0), b.get('ad_category', DASH),
            b.get('top_cta', DASH), b.get('platforms', ''),
            b.get('spend', ''), b.get('target', DASH),
            b.get('media', ''), b.get('pain_kw', ''),
            b.get('scene_kw', ''), b.get('brand_score', 0),
            '✓' if b.get('is_active') else DASH,
            b.get('related') or NONE_LABEL,
        ]
        _append_brand_row(ws, vals, b.get('landing', DASH), highlight_score)
    _set_col_widths(ws, widths)


def _append_brand_row(ws, values: list, landing: str, highlight_score: int = 0):
    """Write a single brand row with optional hyperlink + highlight."""
    from openpyxl.styles import Font, PatternFill
    from openpyxl.cell import WriteOnlyCell
    green_fill = PatternFill(start_color='C6EFCE', end_color='C6EFCE', fill_type='solid')
    link_font  = Font(color='0563C1', underline='single')

    brand_score = values[16] if len(values) > 16 else 0
    highlight = highlight_score and safe_int(brand_score) >= highlight_score
    row = []
    for idx, v in enumerate(values):
        c = WriteOnlyCell(ws, value=_sanitize_for_excel(v))
        # 独立站超链接 (第 7 列, index 6)
        if idx == 6 and landing and landing != DASH and validate_domain(landing):
            url = landing if landing.startswith('http') else f'https://{landing}'
            c.hyperlink = url
            c.font = link_font
        if highlight:
            c.fill = green_fill
        row.append(c)
    ws.append(row)

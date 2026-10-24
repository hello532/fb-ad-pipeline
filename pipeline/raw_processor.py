"""raw_processor.py — batch-process raw scan JSON files with a process pool."""
import json, os, re
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from concurrent.futures import ProcessPoolExecutor, as_completed

from core.logger import get_logger

from .paths import RESULTS_DIR
from .scan_result import save_scan_result, apply_scan_side_effects

log = get_logger(__name__)


def _extract_ads_from_legacy(raw: dict) -> Optional[List[dict]]:
    """从各种旧版JSON格式中提取广告列表, 返回None表示无法提取"""
    if 'results' in raw and isinstance(raw['results'], list):
        return raw['results']
    if 'raw_ads' in raw and isinstance(raw['raw_ads'], list):
        return raw['raw_ads']
    if 'ads' in raw and isinstance(raw['ads'], list):
        return raw['ads']
    if 'top_competitors' in raw and isinstance(raw['top_competitors'], list):
        return [_legacy_competitor_to_ad(c) for c in raw['top_competitors'] if isinstance(c, dict)]
    if 'new_brands' in raw and isinstance(raw['new_brands'], list) and raw['new_brands']:
        return [{'page_name': brand, 'page_likes': 0} for brand in raw['new_brands'] if brand]
    if 'domains' in raw and isinstance(raw['domains'], (list, dict)):
        domains = raw['domains']
        if isinstance(domains, dict):
            return [{'page_name': d, 'link_url': f'https://{d}'} for d in domains]
        else:
            return [{'page_name': d, 'link_url': f'https://{d}'} for d in domains if isinstance(d, str)]
    if 'brands' in raw and isinstance(raw['brands'], dict):
        ads = []
        for bname, binfo in raw['brands'].items():
            if isinstance(binfo, dict):
                ads.append({
                    'page_name': bname,
                    'page_likes': binfo.get('likes', binfo.get('page_likes', 0)),
                    'link_url': binfo.get('url', binfo.get('landing_url', binfo.get('domain', ''))),
                })
            elif isinstance(binfo, (int, float)):
                ads.append({'page_name': bname, 'page_likes': int(binfo)})
        if ads:
            return ads
    return None


def _legacy_competitor_to_ad(comp: dict) -> dict:
    """将旧版 top_competitors 条目转为 normalize_ad 可接受的伪广告"""
    angle = comp.get('angle', '') or comp.get('strategy', '') or ''
    en_parts = re.findall(r'[A-Za-z][A-Za-z\s\-\']{2,}', angle)
    clean_angle = ' '.join(p.strip() for p in en_parts).strip() if en_parts else ''

    result = {
        'page_name': comp.get('brand', 'Unknown'),
        'page_likes': comp.get('likes', 0),
        'link_url': comp.get('landing_url', '') or comp.get('landing', ''),
        'title': clean_angle or angle,
        'body': clean_angle,
        'cta_type': comp.get('top_cta', ''),
    }
    if comp.get('start_date'):
        result['start_date'] = comp['start_date']
    if comp.get('ad_age_days'):
        age_days = comp['ad_age_days']
        approx_start = int((datetime.now() - timedelta(days=age_days)).timestamp())
        result['start_date'] = approx_start
    return result


def _should_process(jf: Path, force: bool) -> bool:
    name = jf.name
    if not name.endswith('.json'):
        return False
    if '_corrected' in name or '汇总' in name or 'brand_intelligence' in name:
        return False
    corrected = jf.with_name(name.replace('.json', '_corrected.json'))
    if corrected.exists() and not force:
        return False
    return True


def _process_one(jf: Path, strict: bool = False):
    """Worker function: parse + save_scan_result without applying shared-file side effects.
    Returns (ok: bool, message: str, side_effects: dict | None).
    """
    name = jf.name
    try:
        with open(jf, encoding='utf-8') as f:
            raw_text = f.read()
        raw_text = re.sub(r',\s*}', '}', raw_text)
        raw_text = re.sub(r',\s*]', ']', raw_text)
        raw = json.loads(raw_text)
    except Exception as e:
        return False, f"跳过 {name}: 解析失败 {e}", None

    stem = jf.stem
    keyword = raw.get('keyword', '')
    if not keyword:
        parts = stem.split('_', 1)
        keyword = parts[1].replace('-', ' ') if len(parts) == 2 else stem

    ads_list = _extract_ads_from_legacy(raw)
    if ads_list is None or len(ads_list) == 0:
        return False, f"跳过 {name}: 无可用广告数据", None

    # 数据质量门 — 去空值, 保证下游 "精确数据"
    from .quality import filter_ads, scan_file_worth_processing
    reason = scan_file_worth_processing(ads_list)
    if reason:
        return False, f"跳过 {name}: 质量不足 ({reason})", None
    ads_list, rejected = filter_ads(ads_list)
    if rejected:
        log.info(f"  {name}: 质量过滤剔除 {rejected} 条低质量广告")
    if len(ads_list) < 1:
        return False, f"跳过 {name}: 过滤后仅剩 {len(ads_list)} 条", None

    # Pydantic 校验 (轻量)
    if strict:
        try:
            from .models import NormalizedAd
            from .utils import normalize_ad as _norm
            for a in ads_list:
                NormalizedAd.model_validate(_norm(a))
        except Exception as e:
            return False, f"处理失败 {name}: schema 校验失败 {e}", None

    try:
        filepath, _, side_effects = save_scan_result(
            keyword, ads_list, source_raw=raw, apply_side_effects=False
        )
        # 可选: 校验输出 (仅 warn)
        try:
            from .models import ScanResult
            with open(filepath, encoding='utf-8') as f:
                ScanResult.model_validate(json.load(f))
        except Exception as e:
            if strict:
                return False, f"处理失败 {name}: 输出 schema 校验失败 {e}", None
            # else 静默吞掉 — 默认轻量模式
        return True, f"处理 {name} ({len(ads_list)} 条) → {Path(filepath).name}", side_effects
    except Exception as e:
        return False, f"处理失败 {name}: {e}", None


def process_raw_results(force: bool = False, workers: Optional[int] = None,
                        strict: bool = False) -> int:
    """批量处理未处理的原始 JSON. workers>1 时使用进程池.
    strict: 启用 Pydantic 严格校验, 失败即跳过此文件.
    """
    tasks = [jf for jf in sorted(RESULTS_DIR.iterdir()) if _should_process(jf, force)]
    if not tasks:
        return 0

    if workers is None:
        workers = min(os.cpu_count() or 2, 8)
    workers = max(1, workers)

    processed = 0
    side_effects_batch = []

    if workers == 1:
        # Sequential path — easier to debug, identical byte output
        for jf in tasks:
            ok, msg, side = _process_one(jf, strict=strict)
            if ok:
                processed += 1
                log.info(msg)
                if side:
                    side_effects_batch.append(side)
            elif '处理失败' in msg:
                log.exception(msg)
            elif '质量不足' in msg or '过滤后' in msg:
                # Expected skip (low-signal sources); demote to DEBUG to keep logs clean
                log.debug(msg)
            else:
                log.warning(msg)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_process_one, jf, strict): jf for jf in tasks}
            for fut in as_completed(futures):
                ok, msg, side = fut.result()
                if ok:
                    processed += 1
                    log.info(msg)
                    if side:
                        side_effects_batch.append(side)
                elif '处理失败' in msg:
                    log.error(msg)
                elif '质量不足' in msg or '过滤后' in msg:
                    log.debug(msg)
                else:
                    log.warning(msg)

    # Apply shared-file side effects serially in main process
    # Sort by today+keyword for deterministic output when workers>1
    side_effects_batch.sort(key=lambda s: (s['today'], s['keyword']))
    apply_scan_side_effects(side_effects_batch)

    return processed

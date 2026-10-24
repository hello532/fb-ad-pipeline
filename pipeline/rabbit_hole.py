"""rabbit_hole.py — extracted from fb-scan-pipeline-v7.py:1955-2056"""
import json, re
from datetime import datetime
from collections import Counter
from urllib.parse import urlparse

from .paths import SCAN_FILE, RESULTS_DIR, INTEL_FILE, RABBIT_QUEUE

from core.logger import get_logger
log = get_logger(__name__)

# ── 兔子洞自动队列 ───────────────────────────────────────────────────────────

def generate_rabbit_hole_queue() -> int:
    """分析所有扫描结果, 生成下一批建议扫描关键词"""
    today = datetime.now().strftime('%Y-%m-%d')

    # 收集已扫描关键词: 源 1 = SCAN_FILE 文本, 源 2 = scans DB 表 (更完整, 避免重复)
    scanned = set()
    if SCAN_FILE.exists():
        with open(SCAN_FILE, encoding='utf-8') as f:
            for line in f:
                parts = line.strip().split('|')
                if parts:
                    scanned.add(parts[0].strip().lower().lstrip('#').strip())
    try:
        from .persistence import get_persistence
        with get_persistence()._conn() as conn:
            for r in conn.execute("SELECT DISTINCT LOWER(keyword) FROM scans"):
                if r and r[0]:
                    scanned.add(r[0].strip())
    except Exception as e:
        log.warning(f"rabbit_hole 读取 scans DB 失败: {e}")

    # 来源A: 从corrected JSON中的 extracted_keywords 提取高频词
    keyword_freq = Counter()
    brand_keywords = []  # 来源B: 品牌衍生词

    corrected_files = list(RESULTS_DIR.glob('*_corrected.json'))
    for jf in corrected_files:
        if '汇总' in jf.name:
            continue
        try:
            data = json.loads(jf.read_text(encoding='utf-8'))
        except Exception:
            continue

        # 来源A: 文本提取关键词
        extracted = data.get('extracted_keywords', {})
        for cat in ['product_keywords', 'pain_keywords', 'scene_keywords']:
            for kw in extracted.get(cat, []):
                kw_lower = kw.lower().strip()
                if len(kw_lower) > 5 and kw_lower not in scanned:
                    keyword_freq[kw_lower] += 1

        # 兔子洞建议
        for kw in data.get('rabbit_hole', {}).get('suggested_keywords', []):
            kw_lower = kw.lower().strip()
            if kw_lower not in scanned:
                keyword_freq[kw_lower] += 1

        # 来源B: 爆品品牌的落地页路径
        for comp in data.get('top_competitors', []):
            if comp.get('ad_age_days', 0) >= 60:
                url = comp.get('landing_url', '')
                if url:
                    path = urlparse(url).path
                    path_words = re.findall(r'[a-z]+(?:-[a-z]+)+', path.lower())
                    for pw in path_words:
                        clean = pw.replace('-', ' ')
                        if len(clean) > 5 and clean not in scanned:
                            brand_keywords.append(clean)

    # 来源C: 轨道品牌名
    if INTEL_FILE.exists():
        try:
            intel = json.loads(INTEL_FILE.read_text(encoding='utf-8'))
            for ob in intel.get('orbit_brands', []):
                name = ob.get('name', '').lower()
                if name and name not in scanned:
                    brand_keywords.append(name)
        except Exception:
            pass

    # 合并并排序
    suggestions = []
    seen = set()

    # 高频文本词 (出现2+次)
    for kw, cnt in keyword_freq.most_common(60):
        if cnt >= 2 and kw not in seen:
            suggestions.append((kw, f'文本提取(x{cnt})', 'auto'))
            seen.add(kw)

    # 品牌衍生词
    brand_freq = Counter(brand_keywords)
    for kw, cnt in brand_freq.most_common(30):
        if kw not in seen:
            suggestions.append((kw, f'品牌衍生(x{cnt})', 'auto'))
            seen.add(kw)

    if not suggestions:
        log.warning("暂无兔子洞建议（需要更多扫描数据）")
        return 0

    # 写入队列文件
    with open(RABBIT_QUEUE, 'w', encoding='utf-8') as f:
        f.write(f'# === 兔子洞自动生成 {today} ===\n')
        f.write(f'# 格式: 英文关键词|中文翻译|类型|子类型\n')
        f.write(f'# 共 {len(suggestions)} 个建议\n\n')
        for kw, source, kw_type in suggestions:
            f.write(f'{kw}|待翻译|{kw_type}|兔子洞-{source}\n')

    log.info(f"  ✅ 兔子洞队列: {len(suggestions)} 个建议关键词")
    log.info(f"     保存到: {RABBIT_QUEUE.name}")
    for i, (kw, source, _) in enumerate(suggestions[:10], 1):
        log.info(f"     {i:2d}. {kw:<35} [{source}]")
    if len(suggestions) > 10:
        log.info(f"     ... 还有 {len(suggestions) - 10} 个")
    return len(suggestions)

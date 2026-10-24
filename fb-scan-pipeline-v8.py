#!/usr/bin/env python3
"""FB Ad Library 批量扫描流水线 v8 — 模块化 + 结构化日志 + 并发

v8 相对 v7.1 变化:
- 业务逻辑拆分到 pipeline/*.py (15 个模块)
- print → core.logger 结构化日志
- process_raw_results 使用 ProcessPoolExecutor 并发, 默认 min(cpu, 8)
- SCAN_FILE 和 LEARNED_FILE 的写入统一在主进程执行, 避免多进程竞争
- _corrected.json / Excel / brand_intelligence.json / 兔子洞 输出字节级兼容 v7.1
"""
import argparse
import sys
from pathlib import Path

# 支持从 NEW/ 目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.logger import setup_logging, get_logger  # noqa: E402
from pipeline.paths import LOG_DIR  # noqa: E402


def _parse_text_items(text: str) -> list[str]:
    import re

    seen = set()
    out = []
    for part in re.split(r'[\n,;\t]+', text or ''):
        item = part.strip().strip('"\'').lower()
        if 2 <= len(item) <= 120 and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def main():
    parser = argparse.ArgumentParser(
        description='FB Ad Library 扫描流水线 v8 — 模块化+结构化日志+并发'
    )
    parser.add_argument('--update-excel',   action='store_true', help='从 _corrected.json 更新 Excel (时间分层)')
    parser.add_argument('--process-raw',    action='store_true', help='批量处理未处理的原始 JSON')
    parser.add_argument('--force',          action='store_true', help='强制重新处理已有 corrected 文件')
    parser.add_argument('--list-queue',     action='store_true', help='列出待扫描关键词')
    parser.add_argument('--instructions',   metavar='KEYWORD',   help='生成指定关键词的扫描指引')
    parser.add_argument('--rabbit-hole',    action='store_true', help='分析扫描结果, 生成兔子洞自动扫描队列')
    parser.add_argument('--update-intel',   action='store_true', help='更新跨品牌情报库')
    parser.add_argument('--show-orbits',    action='store_true', help='显示轨道品牌（出现在3+扫描中）')
    parser.add_argument('--full-pipeline',  action='store_true', help='完整流水线: process-raw → update-excel → update-intel → rabbit-hole')
    parser.add_argument('--show-js',        action='store_true', help='输出 JS v7 提取代码')
    parser.add_argument('--workers',        type=int, default=None, help='并发 worker 数 (默认: min(cpu, 8), 1 为顺序模式)')
    parser.add_argument('--log-level',      default='INFO', help='日志级别 (DEBUG/INFO/WARNING/ERROR)')
    parser.add_argument('--log-file',       default=None, help='日志文件路径 (默认 logs/pipeline.log)')
    parser.add_argument('--strict',         action='store_true', help='启用 Pydantic 严格校验 (失败则跳过该文件)')
    parser.add_argument('--init-db',        action='store_true', help='初始化 SQLite schema (pipeline.db)')
    parser.add_argument('--query-brand',    metavar='DOMAIN',    help='查询单个品牌 (SQLite)')
    parser.add_argument('--query-orbits',   action='store_true', help='列出轨道品牌 (SQLite)')
    parser.add_argument('--query-learned',  action='store_true', help='查询已学习关键词 (配合 --kw-type)')
    parser.add_argument('--kw-type',        default=None,        help='learned 查询的类型过滤 (pain/scene/product/brand)')
    parser.add_argument('--serve',          action='store_true', help='启动 FastAPI 服务 (需要 pip install fastapi uvicorn)')
    parser.add_argument('--host',           default='127.0.0.1', help='FastAPI host (默认 127.0.0.1)')
    parser.add_argument('--port',           type=int, default=8000, help='FastAPI port (默认 8000)')
    parser.add_argument('--reload',         action='store_true', help='FastAPI 开发模式热重载')
    parser.add_argument('--login',          action='store_true', help='打开浏览器手动登录 FB, 保存 session')
    parser.add_argument('--auto-scrape',    action='store_true', help='从 scan_queue 自动采集广告 (headless)')
    parser.add_argument('--scrape-limit',   type=int, default=5, help='单轮采集的关键词数量')
    parser.add_argument('--scrape-headful', action='store_true', help='调试: 采集时显示浏览器 (非 headless)')
    parser.add_argument('--max-pages',     type=int, default=0, help='翻页上限 (0=用 FB_MAX_PAGES env, 默认 1=仅首页, 5=约 125-150 条)')
    parser.add_argument('--daemon',         action='store_true', help='守护模式: 循环 scrape→pipeline→sleep')
    parser.add_argument('--cycle-minutes',  type=int, default=60, help='daemon 周期 (分钟, 默认 60)')
    parser.add_argument('--v3-scrape',      metavar='KEYWORDS', help='V3 直连抓取关键词, 支持逗号/换行分隔, 不依赖 127.0.0.1:8000')
    parser.add_argument('--v3-scrape-library', action='store_true', help='V3 自动读取原 scan queue/词库并抓取, 不依赖 127.0.0.1:8000')
    parser.add_argument('--v3-auto-scrape', metavar='SEEDS', help='V3 自动扩词后直连抓取, 支持逗号/换行分隔, 不依赖 127.0.0.1:8000')
    parser.add_argument('--v3-top-n',        type=int, default=25, help='V3 auto 选取扩词数量')
    parser.add_argument('--v3-country',      default='US', help='V3 抓取国家 (默认 US)')
    parser.add_argument('--v3-max-scrolls',  type=int, default=5, help='V3 每个关键词最大滚动次数')
    parser.add_argument('--v3-delay-sec',    type=float, default=0.15, help='V3 关键词完成后的轻量间隔')
    parser.add_argument('--v3-no-process',   action='store_true', help='V3 抓完不自动 process/recommendations')
    parser.add_argument('--v3-engine',       default=None,
                        choices=['auto', 'firecrawl', 'scrapling', 'playwright'],
                        help='V3 抓取引擎: firecrawl 优先 / scrapling 三层 / playwright 仅 L2 / auto=按 env')
    parser.add_argument('--v3-trend-lists', action='store_true', help='每日抓取 V3 三清单: Google Trends 产品 / AMZ123 新榜 / FB+Shopify 适配')
    parser.add_argument('--v3-trend-force', action='store_true', help='强制刷新 V3 三清单, 忽略每日一次去重')
    parser.add_argument('--v3-trend-append-scan', action='store_true', help='把 FB+Shopify 适配清单追加到 scan queue')
    args = parser.parse_args()

    log_file = args.log_file or str(LOG_DIR / 'pipeline.log')
    setup_logging(level=args.log_level, log_file=log_file)
    log = get_logger('pipeline.cli')

    # Fast paths that don't need heavy pipeline imports
    if args.serve:
        from pipeline.server import run as run_server
        print(f"Serving on http://{args.host}:{args.port} (docs at /docs)")
        run_server(host=args.host, port=args.port, reload=args.reload)
        return

    if args.login:
        from pipeline.scraper import login_interactive
        login_interactive()
        return

    if args.daemon:
        from pipeline.scraper import daemon_loop
        mp = args.max_pages if args.max_pages > 0 else None
        daemon_loop(
            scrape_limit=args.scrape_limit,
            cycle_minutes=args.cycle_minutes,
            headless=not args.scrape_headful,
            max_pages=mp,
        )
        return

    if args.auto_scrape:
        from pipeline.scraper import scrape_queue
        mp = args.max_pages if args.max_pages > 0 else None
        saved = scrape_queue(limit=args.scrape_limit, headless=not args.scrape_headful, max_pages=mp)
        print(f"采集完成: {len(saved)} 个文件")
        return

    if args.v3_scrape or args.v3_scrape_library:
        from pipeline.scraper import scrape_keywords_v3
        import json as _json
        import os as _os

        # --v3-engine 映射到 env (firecrawl_scraper 每次读 env)
        if args.v3_engine == "firecrawl":
            _os.environ["FB_ENGINE"] = "firecrawl"
            _os.environ["FB_FIRECRAWL"] = "1"
        elif args.v3_engine == "scrapling":
            _os.environ["FB_FIRECRAWL"] = "0"
        elif args.v3_engine == "playwright":
            _os.environ["FB_FIRECRAWL"] = "0"
            _os.environ["FB_SCRAPLING_MODE"] = "off"

        if args.v3_scrape_library:
            from pipeline.scan_instructions import scan_queue_loader
            rows = scan_queue_loader()
            keywords = [row['keyword'] for row in rows if row.get('keyword')][:max(1, min(args.scrape_limit, 100))]
        else:
            keywords = _parse_text_items(args.v3_scrape)[:100]
        mp = args.max_pages if args.max_pages > 0 else None
        result = scrape_keywords_v3(
            keywords,
            max_scrolls=max(1, min(args.v3_max_scrolls, 20)),
            headless=not args.scrape_headful,
            country=args.v3_country.strip().upper(),
            fast=True,
            delay_sec=max(0.0, min(args.v3_delay_sec, 10.0)),
            refresh_every=10,
            max_pages=mp,
        )
        if args.v3_scrape_library:
            result['source'] = 'scan_queue_loader'
            result['library_limit'] = args.scrape_limit
        if not args.v3_no_process and result.get('saved'):
            from pipeline.raw_processor import process_raw_results
            from pipeline.recommendations import rebuild_recommendations
            result['processed_files'] = process_raw_results(force=False, workers=2)
            rebuild_recommendations()
        print(_json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.v3_auto_scrape:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from pipeline.keyword_evolution import fetch_external_suggestions, ingest_suggestions
        from pipeline.scraper import scrape_keywords_v3
        import json as _json
        import os as _os

        if args.v3_engine == "firecrawl":
            _os.environ["FB_ENGINE"] = "firecrawl"
            _os.environ["FB_FIRECRAWL"] = "1"
        elif args.v3_engine == "scrapling":
            _os.environ["FB_FIRECRAWL"] = "0"
        elif args.v3_engine == "playwright":
            _os.environ["FB_FIRECRAWL"] = "0"
            _os.environ["FB_SCRAPLING_MODE"] = "off"

        seeds = _parse_text_items(args.v3_auto_scrape)
        suggestion_runs = []
        errors = []
        with ThreadPoolExecutor(max_workers=min(8, len(seeds) or 1)) as ex:
            future_to_seed = {ex.submit(fetch_external_suggestions, seed, True): seed for seed in seeds}
            for fut in as_completed(future_to_seed):
                seed = future_to_seed[fut]
                try:
                    data = fut.result()
                    if data.get('error'):
                        errors.append({'seed': seed, 'error': data.get('error')})
                    else:
                        suggestion_runs.append(data)
                except Exception as e:
                    errors.append({'seed': seed, 'error': str(e)})

        merged = {}
        for run in suggestion_runs:
            for row in run.get('scored') or []:
                kw = row.get('keyword', '').strip().lower()
                if not kw:
                    continue
                cur = merged.get(kw) or {}
                sources = sorted(set(cur.get('sources', []) + row.get('sources', [])))
                score = max(cur.get('trend_score', 0), row.get('trend_score', 0))
                merged[kw] = {'keyword': kw, 'trend_score': score, 'sources': sources, 'kw_type': row.get('kw_type', 'product'), 'subtype': row.get('subtype', 'generic')}

        ranked = sorted(merged.values(), key=lambda r: (-int(r.get('trend_score') or 0), -len(r.get('sources') or []), len(r.get('keyword') or ''), r.get('keyword') or ''))
        selected_rows = ranked[:max(1, min(args.v3_top_n, 100))]
        selected_keywords = [r['keyword'] for r in selected_rows]
        ingested = ingest_suggestions(selected_keywords, source='v3-auto-cli') if selected_keywords else 0
        mp = args.max_pages if args.max_pages > 0 else None
        scrape_result = scrape_keywords_v3(
            selected_keywords,
            max_scrolls=max(1, min(args.v3_max_scrolls, 20)),
            headless=not args.scrape_headful,
            country=args.v3_country.strip().upper(),
            fast=True,
            delay_sec=max(0.0, min(args.v3_delay_sec, 10.0)),
            refresh_every=10,
            max_pages=mp,
        ) if selected_keywords else {'ok': True, 'requested': 0, 'parsed': 0, 'saved': 0, 'results': []}
        if not args.v3_no_process and scrape_result.get('saved'):
            from pipeline.raw_processor import process_raw_results
            from pipeline.recommendations import rebuild_recommendations
            scrape_result['processed_files'] = process_raw_results(force=False, workers=2)
            rebuild_recommendations()
        print(_json.dumps({'ok': True, 'mode': 'v3-auto-cli', 'seeds': seeds, 'suggestion_errors': errors, 'generated_count': len(merged), 'selected_count': len(selected_keywords), 'top_keywords': selected_rows, 'ingested': ingested, 'scrape': scrape_result}, ensure_ascii=False, indent=2))
        return

    if args.v3_trend_lists:
        from pipeline.trend_product_lists import update_daily_lists
        import json as _json
        result = update_daily_lists(
            force=args.v3_trend_force,
            append_scan=args.v3_trend_append_scan,
        )
        print(_json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.init_db:
        from pipeline.persistence import get_persistence
        p = get_persistence()
        print(f"DB ready at {p.db_file}")

    if args.query_brand:
        from pipeline.persistence import get_persistence
        brand = get_persistence().get_brand(args.query_brand)
        if not brand:
            print(f"not found: {args.query_brand}")
        else:
            import json as _json
            print(_json.dumps(brand, indent=2, ensure_ascii=False))
        return

    if args.query_orbits:
        from pipeline.persistence import get_persistence
        for b in get_persistence().list_orbit_brands(min_appearance=3, limit=50):
            print(f"  {b['orbit_score']:3d}/100  x{b['appearance_count']:<2}  {b['canonical_name']:<30} {b['domain']}")
        return

    if args.query_learned:
        from pipeline.persistence import get_persistence
        rows = get_persistence().search_learned(kw_type=args.kw_type, limit=100)
        if not rows:
            print("(empty)")
        for r in rows:
            print(f"  [{r['kw_type']:<8}] {r['keyword']:<30} ← {r['source_kw']} ({r['learned_date']})")
        return

    # Lazy imports so --help and --show-js work without optional deps
    from pipeline.raw_processor import process_raw_results
    from pipeline.excel_writer import update_excel
    from pipeline.intelligence import update_brand_intelligence, show_orbits
    from pipeline.rabbit_hole import generate_rabbit_hole_queue
    from pipeline.scan_instructions import generate_scan_instructions, scan_queue_loader
    from pipeline.js_script import EXTRACT_JS_V7

    if args.full_pipeline:
        print("=" * 60)
        print("  FB Ad Library 完整流水线 v8 — 模块化+并发")
        print("=" * 60)
        print("\n[1/4] 批量处理原始 JSON...")
        n = process_raw_results(force=args.force, workers=args.workers, strict=args.strict)
        print(f"  完成: 处理了 {n} 个文件")
        print("\n[2/4] 更新 Excel (时间分层)...")
        kw_count, brand_count = update_excel()
        print(f"  完成: {kw_count} 个关键词, {brand_count} 条品牌")
        print("\n[3/4] 更新跨品牌情报...")
        update_brand_intelligence()
        print("\n[4/4] 生成兔子洞队列...")
        generate_rabbit_hole_queue()
        print("\n" + "=" * 60)
        print("  流水线完成!")
        print("=" * 60)
        return

    did_anything = False
    if args.process_raw:
        did_anything = True
        log.info(f"批量处理原始 JSON (workers={args.workers or 'auto'})")
        n = process_raw_results(force=args.force, workers=args.workers, strict=args.strict)
        print(f"完成: 处理了 {n} 个文件")

    if args.update_excel:
        did_anything = True
        log.info("更新 Excel (时间分层)")
        kw_count, brand_count = update_excel()
        print(f"Excel 已更新: {kw_count} 个关键词, {brand_count} 条品牌数据")

    if args.update_intel:
        did_anything = True
        log.info("更新跨品牌情报")
        update_brand_intelligence()

    if args.rabbit_hole:
        did_anything = True
        log.info("生成兔子洞队列")
        generate_rabbit_hole_queue()

    if args.show_orbits:
        did_anything = True
        show_orbits()

    if args.list_queue:
        did_anything = True
        items = scan_queue_loader()
        print(f"待扫描关键词 ({len(items)} 个):")
        for i, item in enumerate(items, 1):
            print(f"  {i:3d}. {item['keyword']:<35} [{item['type']}]")

    if args.instructions:
        did_anything = True
        print(generate_scan_instructions(args.instructions))

    if args.show_js:
        did_anything = True
        print(EXTRACT_JS_V7)

    if not did_anything:
        parser.print_help()


if __name__ == '__main__':
    main()

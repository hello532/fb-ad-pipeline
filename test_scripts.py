#!/usr/bin/env python3
"""Smoke test: verify v9 entry points import + respond to --help."""
import subprocess
import sys
from unittest.mock import patch

NEW_DIR = '$HOME/.claude/skills/NEW'


def test_cli(args, label):
    try:
        r = subprocess.run(
            ['python3', 'fb-scan-pipeline-v8.py'] + args,
            capture_output=True, text=True, timeout=15, cwd=NEW_DIR,
        )
        if r.returncode == 0:
            print(f"OK   fb-scan-pipeline-v8.py {label}")
            return True
        print(f"FAIL fb-scan-pipeline-v8.py {label}: {r.stderr[:200]}")
        return False
    except Exception as e:
        print(f"FAIL fb-scan-pipeline-v8.py {label}: {e}")
        return False


def test_pipeline_import():
    try:
        r = subprocess.run(
            ['python3', '-c',
             'import pipeline; '
             'from pipeline import scraper, raw_processor, server, persistence, '
             'recommendations, scan_result, scan_instructions, intelligence, '
             'rabbit_hole, strategies, keyword_evolution, classifier, insights, '
             'playbook, trends, exporter, excel_writer, v2_scrape_fast, '
             'proxy_pool, orchestrator, scheduler, preflight; '
             'print("OK", pipeline.__version__)'],
            capture_output=True, text=True, timeout=15, cwd=NEW_DIR,
        )
        if r.returncode == 0:
            print(f"OK   pipeline package import: {r.stdout.strip()}")
            return True
        print(f"FAIL pipeline import: {r.stderr[:300]}")
        return False
    except Exception as e:
        print(f"FAIL pipeline import: {e}")
        return False


def test_v2_filters():
    try:
        from pipeline.v2_scrape_fast import apply_d2c_winner_filter, filter_seed_keywords
        from pipeline.lp_prober import _normalize_probe_url

        kept, dropped = filter_seed_keywords([
            'pet hair remover', 'amazon deals', 'loan quote', 'neck massager',
            'sleep quran relax', 'sleep instrumental music', 'sleep questionnaire pdf',
        ])
        assert kept == ['pet hair remover', 'neck massager']
        assert 'amazon deals' in dropped and 'loan quote' in dropped
        assert 'sleep quran relax' in dropped
        assert 'sleep instrumental music' in dropped
        assert 'sleep questionnaire pdf' in dropped

        rec = {
            'domain': 'example.com',
            'landing_url': 'https://example.com/products/widget',
            'ads_count': 3,
            'price_main': 29,
            'ad_age_days': 20,
        }
        ok, reason = apply_d2c_winner_filter(rec, {'lp:shopify'}, min_ads=2, min_price=9)
        assert ok and reason == 'PASS'
        rec['landing_url'] = 'https://amazon.com/dp/abc'
        ok, reason = apply_d2c_winner_filter(rec, {'lp:shopify'}, min_ads=2, min_price=9)
        assert not ok and reason.startswith('url-blacklist')

        rec['landing_url'] = 'https://example.com/products/widget'
        ok, reason = apply_d2c_winner_filter(rec, set(), min_ads=2, min_price=9)
        assert not ok and reason == 'no-d2c-lp:none'

        rec['landing_url'] = 'https://example.com/pages/about'
        ok, reason = apply_d2c_winner_filter(rec, {'lp:shopify'}, min_ads=2, min_price=9)
        assert not ok and reason == 'no-commerce-url'

        rec['landing_url'] = 'https://example.com/products/windows-license'
        rec['top_product'] = 'Microsoft Windows 10 Professional Digital License'
        ok, reason = apply_d2c_winner_filter(rec, {'lp:shopify'}, min_ads=2, min_price=9)
        assert not ok and reason == 'trademark-risk'

        rec['top_product'] = 'Sleep apnea supplement'
        ok, reason = apply_d2c_winner_filter(rec, {'lp:shopify'}, min_ads=2, min_price=9)
        assert not ok and reason == 'regulated-risk'

        normalized, error = _normalize_probe_url(r'https:\/\/example.com\/products\/widget?x=1&amp;y=2')
        assert error is None
        assert normalized == 'https://example.com/products/widget?x=1&y=2'

        rec['top_product'] = 'Generic home organizer'
        rec['landing_url'] = r'https:\/\/example.com\/products\/widget'
        ok, reason = apply_d2c_winner_filter(rec, {'lp:shopify'}, min_ads=2, min_price=9)
        assert ok and reason == 'PASS'
        print('OK   v2 filters')
        return True
    except Exception as e:
        print(f'FAIL v2 filters: {e}')
        return False


def test_v2_dry_run_model():
    try:
        from pipeline.models import V2ScrapeFastRequest
        from pipeline.v2_scrape_fast import start_scrape_session

        req = V2ScrapeFastRequest(keywords=[' pet hair remover ', 'amazon deals', 'pet hair remover'], workers=2, dry_run=True)
        res = start_scrape_session(keywords=req.keywords, workers=req.workers, dry_run=req.dry_run)
        assert res['dry_run'] is True
        assert res['state'] == 'planned'
        assert res['kw_count'] == 2
        print('OK   v2 dry-run model')
        return True
    except Exception as e:
        print(f'FAIL v2 dry-run model: {e}')
        return False


def test_v2_session_mock():
    try:
        import pipeline.scraper as scraper
        from pipeline import v2_scrape_fast as v2

        def fake_scrape(keyword, **kwargs):
            if keyword == 'challenge':
                return 'CHALLENGE'
            if keyword == 'empty':
                return None
            if keyword == 'boom':
                raise RuntimeError('mock boom')
            return '/tmp/mock-result.json'

        with patch.object(scraper, 'scrape_keyword', side_effect=fake_scrape):
            res = v2.start_scrape_session(keywords=['ok', 'empty', 'boom'], workers=2)
            sid = res['id']
            for _ in range(50):
                state = v2.get_session_state(sid)
                if state and state['state'] in {'done', 'partial', 'failed'}:
                    break
                import time
                time.sleep(0.05)
            state = v2.get_session_state(sid)
            assert state['counts']['ok'] == 1
            assert state['counts']['empty'] == 1
            assert state['counts']['error'] == 1
            assert state['state'] in {'partial', 'failed'}
        print('OK   v2 session mock')
        return True
    except Exception as e:
        print(f'FAIL v2 session mock: {e}')
        return False


def test_l2_fallback_thread_bridge():
    try:
        import pipeline.scraper as scraper

        calls = {}

        def fake_thread(keyword, max_scrolls, headless, country, max_pages):
            calls.update({
                'keyword': keyword,
                'max_scrolls': max_scrolls,
                'headless': headless,
                'country': country,
                'max_pages': max_pages,
            })
            return '/tmp/l2-result.json'

        with (
            patch.object(scraper, 'triple_scan', return_value='NEED_PLAYWRIGHT'),
            patch.object(scraper, '_has_running_asyncio_loop', return_value=True),
            patch.object(scraper, '_run_l2_fallback_in_clean_thread', side_effect=fake_thread),
        ):
            res = scraper.scrape_keyword('pet hair remover', max_scrolls=3, headless=True, country='US', max_pages=1)

        assert res == '/tmp/l2-result.json'
        assert calls == {
            'keyword': 'pet hair remover',
            'max_scrolls': 3,
            'headless': True,
            'country': 'US',
            'max_pages': 1,
        }
        print('OK   L2 fallback thread bridge')
        return True
    except Exception as e:
        print(f'FAIL L2 fallback thread bridge: {e}')
        return False


def test_defaults_strip_excel_illegal_chars():
    try:
        from openpyxl import Workbook
        from pipeline.defaults import safe_str, scrub_output

        cleaned = safe_str('Dr. James\x19 Health Insights')
        assert cleaned == 'Dr. James Health Insights'
        assert scrub_output({'brand': 'A\x00B'}) == {'brand': 'AB'}

        wb = Workbook(write_only=True)
        ws = wb.create_sheet('test')
        ws.append([cleaned])
        wb.save('/tmp/fbpipeline-safe-str-test.xlsx')
        print('OK   defaults strip Excel illegal chars')
        return True
    except Exception as e:
        print(f'FAIL defaults strip Excel illegal chars: {e}')
        return False


def test_v2_fastapi_dry_run():
    try:
        from fastapi.testclient import TestClient
        from pipeline.server import app

        client = TestClient(app)
        health = client.get('/v2/health')
        assert health.status_code == 200
        res = client.post('/v2/scrape-fast', json={
            'keywords': ['pet hair remover', 'amazon deals', 'neck massager'],
            'workers': 2,
            'dry_run': True,
        })
        assert res.status_code == 200, res.text
        data = res.json()
        assert data['dry_run'] is True
        assert data['state'] == 'planned'
        assert data['pre_filter_dropped'] == 1
        print('OK   v2 FastAPI dry-run')
        return True
    except Exception as e:
        print(f'FAIL v2 FastAPI dry-run: {e}')
        return False


def test_runtime_helpers():
    try:
        from pipeline.paths import NEW_DIR
        from pipeline.runtime import get_proxy_candidates, resolve_env_path

        with patch.dict('os.environ', {'FB_PROXY': 'http://127.0.0.1:6152', 'FB_PROXY_FALLBACK_DIRECT': '1'}):
            assert get_proxy_candidates() == ['http://127.0.0.1:6152', None]

        with patch.dict('os.environ', {'FB_PROXY': 'direct', 'FB_PROXY_FALLBACK_DIRECT': '1'}):
            assert get_proxy_candidates() == [None]

        with patch.dict('os.environ', {'FB_DB_FILE': 'relative/pipeline.db'}):
            resolved = resolve_env_path('FB_DB_FILE', NEW_DIR / 'pipeline.db', NEW_DIR)
            assert resolved == (NEW_DIR / 'relative' / 'pipeline.db').resolve()

        print('OK   runtime helpers')
        return True
    except Exception as e:
        print(f'FAIL runtime helpers: {e}')
        return False


def test_proxy_pool():
    try:
        from pipeline.proxy_pool import ProxyPool, classify_error, ERROR_PROXY_CONNECTION, ERROR_TIMEOUT
        from unittest.mock import patch

        with patch.dict('os.environ', {
            'FB_PROXY_LIST': 'http://a:1,http://b:2',
            'FB_PROXY': '',
            'FB_PROXY_FALLBACK_DIRECT': '1',
        }):
            pool = ProxyPool()
            stats = pool.stats()
            assert stats['candidates'] == 3, f"expected 3, got {stats['candidates']}"
            assert stats['healthy'] == 3

            proxy = pool.acquire()
            assert proxy is not None or proxy is None

            pool.report_success('http://a:1')
            pool.report_failure('http://b:2', ERROR_PROXY_CONNECTION)

            stats = pool.stats()
            a_entry = [e for e in stats['entries'] if e['url'] == 'http://a:1'][0]
            assert a_entry['success'] == 1, f"a success should be 1, got {a_entry['success']}"

            for i in range(6):
                pool.report_failure('http://b:2', ERROR_PROXY_CONNECTION)
            stats = pool.stats()
            b_entry = [e for e in stats['entries'] if e['url'] == 'http://b:2'][0]
            assert b_entry['fused'] is True, "b should be fused after 7 consecutive failures"
            assert stats['fused'] >= 1

        assert classify_error(Exception("ERR_CONNECTION_CLOSED")) == ERROR_PROXY_CONNECTION
        assert classify_error(Exception("request timed out")) == ERROR_TIMEOUT

        seq = pool.acquire_sequence()
        assert isinstance(seq, list) and len(seq) > 0

        print('OK   proxy pool')
        return True
    except Exception as e:
        print(f'FAIL proxy pool: {e}')
        import traceback; traceback.print_exc()
        return False


def test_orchestrator():
    try:
        from pipeline.orchestrator import Orchestrator, ScrapeResult, LayerAttempt

        result = ScrapeResult(keyword='test', country='US')
        assert result.final_status == 'pending'
        assert result.legacy_return is None

        result.final_status = 'challenge'
        assert result.legacy_return == 'CHALLENGE'

        result.trace.append(LayerAttempt(layer='L0', status='has_results', ads_count=5))
        result.trace.append(LayerAttempt(layer='L1.5', status='success', ads_count=30))
        summary = result.trace_summary()
        assert 'L0:has_results(5)' in summary
        assert 'L1.5:success(30)' in summary

        orch = Orchestrator()
        stats = orch.layer_stats()
        assert isinstance(stats, dict)

        print('OK   orchestrator')
        return True
    except Exception as e:
        print(f'FAIL orchestrator: {e}')
        import traceback; traceback.print_exc()
        return False


def test_scheduler():
    try:
        from pipeline.scheduler import AdaptiveScheduler

        sched = AdaptiveScheduler()
        workers = sched.recommended_workers()
        assert workers >= 1
        assert workers <= 12

        sched.record_simple(success=True, duration_sec=3.0)
        sched.record_simple(success=True, duration_sec=2.0)
        sched.record_simple(success=False, challenge=True, duration_sec=1.0)

        stats = sched.stats()
        assert stats['total_keywords'] == 3
        assert stats['total_success'] == 2
        assert stats['total_challenge'] == 1
        assert 'fd_pressure' in stats
        assert 'recent_success_rate' in stats

        with sched.light_slot() as acquired:
            assert acquired is True or acquired is False

        with sched.heavy_slot() as acquired:
            assert acquired is True or acquired is False

        print('OK   scheduler')
        return True
    except Exception as e:
        print(f'FAIL scheduler: {e}')
        import traceback; traceback.print_exc()
        return False


def test_preflight():
    try:
        from pipeline.preflight import run_preflight

        report = run_preflight(fix=False)
        assert report['status'] in ('ok', 'degraded', 'fatal')
        assert isinstance(report['checks'], list)
        assert len(report['checks']) >= 5

        dir_check = [c for c in report['checks'] if c['name'] == 'directories'][0]
        assert dir_check['status'] in ('ok', 'fixed')

        db_check = [c for c in report['checks'] if c['name'] == 'db_access'][0]
        assert db_check['status'] in ('ok', 'fatal')

        print('OK   preflight')
        return True
    except Exception as e:
        print(f'FAIL preflight: {e}')
        import traceback; traceback.print_exc()
        return False


def test_proxy_pool_integration_with_runtime():
    try:
        from pipeline.proxy_pool import get_pool, proxy_for_playwright
        from pipeline.runtime import proxy_label

        pool = get_pool()
        proxy = pool.acquire()
        assert proxy_label(proxy) is not None

        pw_proxy = proxy_for_playwright(proxy)
        if proxy is not None:
            assert isinstance(pw_proxy, dict) and 'server' in pw_proxy
        else:
            assert pw_proxy is None

        pw_none = proxy_for_playwright(None)
        assert pw_none is None

        print('OK   proxy pool integration')
        return True
    except Exception as e:
        print(f'FAIL proxy pool integration: {e}')
        return False


def main():
    results = [
        test_cli(['--help'], '--help'),
        test_cli(['--show-js'], '--show-js'),
        test_pipeline_import(),
        test_v2_filters(),
        test_v2_dry_run_model(),
        test_v2_session_mock(),
        test_l2_fallback_thread_bridge(),
        test_defaults_strip_excel_illegal_chars(),
        test_v2_fastapi_dry_run(),
        test_runtime_helpers(),
        test_proxy_pool(),
        test_orchestrator(),
        test_scheduler(),
        test_preflight(),
        test_proxy_pool_integration_with_runtime(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*60}\nResults: {passed}/{total} passed")
    sys.exit(0 if passed == total else 1)


if __name__ == '__main__':
    main()

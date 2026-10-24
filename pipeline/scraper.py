"""pipeline/scraper.py — Playwright 人类行为模拟采集器.

核心目标: 看起来像一个真实用户在浏览 FB Ad Library.
- 鼠标曲线移动 / hover 广告 / 递增滚动
- 每关键词 60-180s 间隔, 每天配额上限
- 静默时段 (凌晨 2-7 点跳过)
- 熔断: 连续 3 次 CHALLENGE → 暂停 6 小时 + 写 state 文件
- 日/周 quota 持久化到 state 文件
- session 过期自动进入冷却, 等候人工 --login
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from core.logger import get_logger

from .paths import NEW_DIR, RESULTS_DIR
from . import js_script as _js_script
from .graphql_sniffer import install_sniffer, merge_dom_with_graphql
from .commerce_filter import filter_commerce_ads
from .scrapling_scraper import triple_scan, _patchright_paginate
from .proxy_pool import get_pool, proxy_for_playwright, classify_error, scrape_proxy_context
from .scheduler import get_scheduler
from .orchestrator import get_orchestrator


def _get_extract_js() -> str:
    """Hot-reload js_script from disk so edits to EXTRACT_JS_V9 take effect
    without restarting the daemon. Cost is a single mtime check + occasional
    reload — negligible vs. a Playwright fetch."""
    import importlib
    try:
        importlib.reload(_js_script)
    except Exception:
        pass
    return _js_script.EXTRACT_JS_V7

log = get_logger(__name__)


def _raise_nofile_limit(target: int) -> int | None:
    """Raise the per-process file descriptor limit for browser-heavy scraping."""
    try:
        import resource

        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft >= target:
            return int(soft)
        hard_cap = target if hard == resource.RLIM_INFINITY else int(hard)
        new_soft = min(target, hard_cap)
        if new_soft > soft:
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_soft, hard))
            log.info(f"NOFILE soft limit raised: {soft} -> {new_soft}")
            return int(new_soft)
        return int(soft)
    except Exception as exc:
        log.debug(f"NOFILE soft limit unchanged: {exc}")
        return None


_NOFILE_SOFT_LIMIT = _raise_nofile_limit(int(os.getenv("FB_NOFILE_LIMIT", "4096")))

PROFILE_DIR = NEW_DIR / ".browser_profile"
STATE_FILE = NEW_DIR / ".scraper_state.json"
AD_LIBRARY_BASE = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&search_type=keyword_unordered"
)

# 匿名模式开关 (默认开): 不加载登录 cookie, 彻底零账号风险
ANONYMOUS_MODE = os.getenv("FB_ANONYMOUS", "1") != "0"

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]
VIEWPORTS = [
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1680, "height": 1050},
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
]
TIMEZONES = ["America/Los_Angeles", "America/New_York", "America/Chicago"]


# ── v10: BrowserForge 统计真实指纹 ──────────────────────────────────────────
def _generate_fingerprint() -> dict:
    """生成统计真实的浏览器指纹 (BrowserForge). 回退到硬编码列表.

    实际生成在 pipeline.fingerprint, 这里保留以兼容历史调用者.
    L1.5 Patchright 也用同一个生成器, 保证 layer 间指纹一致策略."""
    from .fingerprint import generate_fingerprint
    return generate_fingerprint()
CHALLENGE_MARKERS = ("checkpoint", "captcha", "/login", "/challenge", "unusual_activity",
                     "recaptcha", "temporarily_restricted", "security check", "verify you are",
                     "unusual activity", "suspicious")

# 默认配置 — 保守人类节奏 (v3: 再加强)
# 可用环境变量覆盖, 适配无头 24h 不间断抓取:
#   FB_DELAY_MIN_SEC / FB_DELAY_MAX_SEC  关键词间基础间隔
#   FB_LONG_BREAK_PROB                   每次抓取后以此概率插入"人类长休"
#   FB_LONG_BREAK_MIN_SEC / FB_LONG_BREAK_MAX_SEC  长休时长
#   FB_DAILY_QUOTA                       <=0 表示无配额(24h 连续跑)
#   FB_QUIET_ENABLED                     "0"/"false" 禁用静默时段
#   FB_QUIET_START / FB_QUIET_END        静默时段起止小时
DEFAULT_KEYWORD_DELAY = (
    float(os.getenv('FB_DELAY_MIN_SEC', '2')),
    float(os.getenv('FB_DELAY_MAX_SEC', '5')),
)
LONG_BREAK_PROB = float(os.getenv('FB_LONG_BREAK_PROB', '0.002'))
LONG_BREAK_RANGE = (
    float(os.getenv('FB_LONG_BREAK_MIN_SEC', '30')),
    float(os.getenv('FB_LONG_BREAK_MAX_SEC', '80')),
)
DEFAULT_DAILY_QUOTA = int(os.getenv('FB_DAILY_QUOTA', '0'))  # 0 = 无配额
QUIET_ENABLED = os.getenv('FB_QUIET_ENABLED', '0').lower() not in ('0', 'false', 'no', '')
DEFAULT_QUIET_HOURS = (
    int(os.getenv('FB_QUIET_START', '1')),
    int(os.getenv('FB_QUIET_END', '8')),
)
DEFAULT_CYCLE_MINUTES = int(os.getenv('FB_CYCLE_MINUTES', '5'))
DEFAULT_AD_LIBRARY_COUNTRY = os.getenv('FB_AD_LIBRARY_COUNTRY', 'US').strip().upper() or 'US'
CHALLENGE_COOLDOWN_SEC = 8 * 3600          # 触发风控 8 小时冷却
MAX_CONSECUTIVE_CHALLENGES = 2

# v10: Surge 代理轮换 — 通过 proxy_pool 管理, 支持多代理 + 健康追踪
# 设 FB_PROXY=http://127.0.0.1:6152 启用单代理
# 设 FB_PROXY_LIST=http://a:1,http://b:2 启用多代理
# 留空 = 直连 (默认)
#
# NOTE: FB_PROXY is exported as a module constant because scraper_v9 imports it.
# An empty string means "direct connection". proxy_pool.py reads FB_PROXY again at
# call time, so changing the env var after import still works there.
FB_PROXY = os.getenv('FB_PROXY', '').strip()
FB_PROXY_LIST = [p.strip() for p in os.getenv('FB_PROXY_LIST', '').split(',') if p.strip()]

# v9.3 速度强化: 阻断不必要资源 (图片/字体/样式表/媒体), 让页面只下载 HTML+JS
# FB Ad Library 我们要的全在 <script> tags 里, 视觉资源完全不需要.
# 关闭后单关键词 page.goto 从 5-10s → 1-3s. 这是最大单点提速.
FB_BLOCK_RESOURCES = os.getenv('FB_BLOCK_RESOURCES', '1') != '0'
BLOCKED_RESOURCE_TYPES = {
    'image', 'media', 'font', 'stylesheet',
}
# 也阻断已知非必要的第三方域 (跟踪/分析), 不影响数据抓取
BLOCKED_DOMAINS = (
    'connect.facebook.net/en_US/fbevents.js',  # pixel
    'static.xx.fbcdn.net/rsrc.php',            # 部分非必要静态
    'scontent.', 'scontent-',                   # CDN 图片域 (兜底, 已被 image type 拦)
    'video.xx.fbcdn.net',                       # 视频
)


def _human_delay(base_range: tuple[float, float]) -> float:
    """拟人化间隔: 多数时候在基础范围, 偶尔来一次"长休"(模拟离开电脑)."""
    if random.random() < LONG_BREAK_PROB:
        return random.uniform(*LONG_BREAK_RANGE)
    # 对数正态 → 偏态分布比 uniform 更像真人(多数短, 长尾)
    lo, hi = base_range
    mid = (lo + hi) / 2
    sigma = 0.28
    v = random.lognormvariate(math.log(mid), sigma)
    return max(lo * 0.6, min(hi * 1.2, v))


# ── State 持久化 ──────────────────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        'today': '', 'today_count': 0, 'total_scraped': 0,
        'consecutive_challenges': 0, 'cooldown_until': None,
        'last_scrape_at': None, 'last_challenge_at': None,
    }


def _save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2))


def get_status() -> dict:
    """对外暴露: daemon 状态快照 (V9.0 含扫描间隔元数据)."""
    s = _load_state()
    now = datetime.now()
    in_cooldown = False
    cooldown_remaining = 0
    if s.get('cooldown_until'):
        try:
            cu = datetime.fromisoformat(s['cooldown_until'])
            if cu > now:
                in_cooldown = True
                cooldown_remaining = int((cu - now).total_seconds())
        except Exception:
            pass
    today = now.strftime('%Y-%m-%d')

    # V9.0: 扫描间隔 + 下次预估触发
    lo_kw, hi_kw = DEFAULT_KEYWORD_DELAY
    next_cycle_eta_sec = None
    next_cycle_at = None
    last_scrape_at = s.get('last_scrape_at')
    if last_scrape_at:
        try:
            last_dt = datetime.fromisoformat(last_scrape_at)
            # daemon 周期 = cycle_minutes * (1 ± 0.2) jitter, 平均取中值
            next_dt = last_dt + timedelta(minutes=DEFAULT_CYCLE_MINUTES)
            next_cycle_at = next_dt.isoformat(timespec='seconds')
            next_cycle_eta_sec = max(0, int((next_dt - now).total_seconds()))
        except Exception:
            pass

    return {
        'today': today,
        'today_count': s.get('today_count', 0) if s.get('today') == today else 0,
        'daily_quota': DEFAULT_DAILY_QUOTA,
        'total_scraped': s.get('total_scraped', 0),
        'consecutive_challenges': s.get('consecutive_challenges', 0),
        'in_cooldown': in_cooldown,
        'cooldown_remaining_sec': cooldown_remaining,
        'last_scrape_at': last_scrape_at,
        'last_challenge_at': s.get('last_challenge_at'),
        'in_quiet_hours': _is_quiet_hours(),
        # V9.0 新增: 扫描间隔元数据
        'intervals': {
            'keyword_delay_min_sec': int(lo_kw),
            'keyword_delay_max_sec': int(hi_kw),
            'keyword_delay_display': f"{int(lo_kw)}-{int(hi_kw)} 秒 (约 {int(lo_kw/60)}-{int(hi_kw/60)} 分钟)",
            'cycle_minutes': DEFAULT_CYCLE_MINUTES,
            'cycle_display': f"{DEFAULT_CYCLE_MINUTES} 分钟/周期 (±20% 抖动)",
            'quiet_hours': f"{DEFAULT_QUIET_HOURS[0]}:00 - {DEFAULT_QUIET_HOURS[1]}:00 静默",
            'quiet_hours_range': list(DEFAULT_QUIET_HOURS),
            'challenge_cooldown_hours': int(CHALLENGE_COOLDOWN_SEC / 3600),
        },
        'next_cycle_at': next_cycle_at,
        'next_cycle_eta_sec': next_cycle_eta_sec,
    }


def _is_quiet_hours(now: Optional[datetime] = None) -> bool:
    if not QUIET_ENABLED:
        return False
    now = now or datetime.now()
    h = now.hour
    lo, hi = DEFAULT_QUIET_HOURS
    if lo == hi:
        return False
    if lo <= hi:
        return lo <= h < hi
    return h >= lo or h < hi


def _can_scrape_now(s: dict) -> tuple[bool, str]:
    now = datetime.now()
    if s.get('cooldown_until'):
        try:
            cu = datetime.fromisoformat(s['cooldown_until'])
            if cu > now:
                return False, f"熔断冷却中, 剩余 {int((cu - now).total_seconds()/60)} 分钟"
        except Exception:
            pass
    if _is_quiet_hours(now):
        return False, f"静默时段 ({DEFAULT_QUIET_HOURS[0]}-{DEFAULT_QUIET_HOURS[1]}), 模拟休息"
    today = now.strftime('%Y-%m-%d')
    if DEFAULT_DAILY_QUOTA > 0 and s.get('today') == today and s.get('today_count', 0) >= DEFAULT_DAILY_QUOTA:
        return False, f"已达今日配额 {DEFAULT_DAILY_QUOTA}"
    return True, ""


# ── 登录 ──────────────────────────────────────────────────────────────────

def login_interactive(timeout_sec: int = 300) -> None:
    from playwright.sync_api import sync_playwright
    PROFILE_DIR.mkdir(exist_ok=True)
    log.info(f"打开浏览器, 请手动登录 Facebook. profile: {PROFILE_DIR}")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.facebook.com/login")
        print(f">>> 请登录并关闭浏览器继续 (超时 {timeout_sec}s)")
        try:
            page.wait_for_event("close", timeout=timeout_sec * 1000)
        except Exception:
            pass
        ctx.close()
    # 登录成功后清除熔断
    s = _load_state()
    s['consecutive_challenges'] = 0
    s['cooldown_until'] = None
    _save_state(s)
    log.info("登录流程结束 — session 已保存, 熔断已重置")


# ── 拟人化动作 ────────────────────────────────────────────────────────────

def _human_mouse_move(page, target_x: int, target_y: int, steps: int = 20) -> None:
    """贝塞尔曲线式鼠标移动, 代替一步到位."""
    try:
        cur_x = random.randint(100, 800)
        cur_y = random.randint(100, 500)
        for i in range(steps):
            t = (i + 1) / steps
            # 缓动曲线 (ease-in-out)
            te = t * t * (3 - 2 * t)
            x = cur_x + (target_x - cur_x) * te + random.uniform(-3, 3)
            y = cur_y + (target_y - cur_y) * te + random.uniform(-3, 3)
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.005, 0.012))
    except Exception:
        pass


def _human_scroll(page, max_scrolls: int = 6) -> None:
    """递增滚动 + 随机停顿. v9.3.1: 容错滚动 — FB 导航时 evaluate 会抛 'context destroyed',
    我们捕获就 break, 让外层用 networkidle 等等再 evaluate 主提取."""
    last_bottom = 0
    stable_rounds = 0
    for i in range(max_scrolls):
        step = random.randint(600, 1100)
        try:
            page.evaluate(f"window.scrollBy(0, {step})")
        except Exception as e:
            # context destroyed 或网络异常, 保护性退出滚动循环
            err = str(e)
            if 'destroyed' in err.lower() or 'navigation' in err.lower() or 'closed' in err.lower():
                log.debug(f"  scroll: context destroyed @ #{i}, break early")
                return
            return
        pause = random.uniform(0.2, 0.6)
        time.sleep(pause)
        if random.random() < 0.08 and i < max_scrolls // 2:
            try:
                _human_mouse_move(page,
                                  random.randint(200, 1100),
                                  random.randint(200, 700),
                                  steps=4)
            except Exception:
                pass
            time.sleep(random.uniform(0.1, 0.3))
        # 到底判定 (容错)
        try:
            h = page.evaluate("document.body.scrollHeight")
        except Exception:
            return
        if h == last_bottom:
            stable_rounds += 1
            if stable_rounds >= 2:
                break
        else:
            stable_rounds = 0
            last_bottom = h


def _human_initial_pause(page) -> None:
    """页面加载后短暂停顿. v9.4: 0.1-0.3."""
    _human_mouse_move(page, random.randint(300, 1000), random.randint(150, 400), steps=4)
    time.sleep(random.uniform(0.1, 0.3))


def _attach_resource_blocker(ctx) -> None:
    """v9.3: 在 BrowserContext 上挂路由, 拦截图片/字体/样式表/媒体.

    FB Ad Library 数据全在 <script> tags 里, 视觉资源不需要下载.
    带来 4-7x 的 page.goto 提速. 风控影响: 极小 (匿名爬虫不要求渲染像真用户).
    """
    if not FB_BLOCK_RESOURCES:
        return
    def _route(route):
        try:
            r = route.request
            if r.resource_type in BLOCKED_RESOURCE_TYPES:
                return route.abort()
            url = r.url or ''
            if any(d in url for d in BLOCKED_DOMAINS):
                return route.abort()
            return route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass
    try:
        ctx.route('**/*', _route)
    except Exception:
        pass


# ── 单关键词抓取 ───────────────────────────────────────────────────────────

def _has_running_asyncio_loop() -> bool:
    try:
        import asyncio as _asyncio

        _asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _run_l2_fallback_in_clean_thread(
    keyword: str,
    max_scrolls: int,
    headless: bool,
    country: str,
    max_pages: Optional[int],
) -> Optional[object]:
    """Run sync Playwright fallback outside a thread that already owns asyncio."""
    result: dict[str, object] = {}

    def _target() -> None:
        try:
            result["value"] = scrape_keyword(
                keyword,
                max_scrolls=max_scrolls,
                headless=headless,
                country=country,
                max_pages=max_pages,
                _force_l2=True,
            )
        except BaseException as exc:
            result["error"] = exc

    worker = threading.Thread(
        target=_target,
        name=f"playwright-l2-{abs(hash(keyword)) % 100000}",
        daemon=True,
    )
    worker.start()
    timeout = float(os.getenv("FB_L2_THREAD_TIMEOUT_SEC", "180"))
    worker.join(timeout=timeout)
    if worker.is_alive():
        log.warning(f"  L2 Playwright fallback 超时 ({timeout:.0f}s): {keyword}")
        return None
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result.get("value")


def scrape_keyword(keyword: str, max_scrolls: int = 12, headless: bool = True,
                   country: Optional[str] = None, max_pages: Optional[int] = None,
                   *, _force_l2: bool = False) -> Optional[object]:
    """返回: Path(成功) / 'CHALLENGE'(风控) / None(其它失败).
    默认匿名模式 (不加载登录 cookie). 通过 FB_ANONYMOUS=0 切回登录模式.

    v9.5 多层组合抓取:
      Firecrawl (可选优先, FB_FIRECRAWL/FB_ENGINE/有 API key 时)
      L0 Fetcher (2s) 快筛 → 零命中跳过
      L1 StealthyFetcher (5-7s) 深抓 → 95% 字段, 直接输出
      L1.5 Patchright+graphql 翻页 (max_pages>1 时) → 125-150 条
      L2 Playwright (15s) fallback → 仅 L1 失败时触发
    用 FB_FIRECRAWL=1 / FB_ENGINE=firecrawl / --v3-engine firecrawl 启用 Firecrawl.
    用 FB_FIRECRAWL=only 只用 Firecrawl 不回退.
    用 FB_SCRAPLING_MODE=off 关闭 Scrapling 全层.
    用 FB_SCRAPLING_MODE=quick 仅用 L0 快筛 + Playwright.
    用 FB_MAX_PAGES=N 翻 N 页 (默认 1=仅首页).
    """
    country = (country or DEFAULT_AD_LIBRARY_COUNTRY).strip().upper()
    mode = "ANON" if ANONYMOUS_MODE else "AUTH"

    if not _force_l2:
        # ── Firecrawl 优先层 (FB_FIRECRAWL / FB_ENGINE=firecrawl / 有 API key) ──
        try:
            from .firecrawl_scraper import firecrawl_enabled, firecrawl_mode, scrape_via_firecrawl
            if firecrawl_enabled():
                fc_mode = firecrawl_mode()
                log.info(f"scrape[{country}|{mode}]: {keyword} (Firecrawl mode={fc_mode})")
                fc = scrape_via_firecrawl(keyword, country=country, max_scrolls=max_scrolls)
                if fc == "CHALLENGE":
                    return "CHALLENGE"
                if isinstance(fc, Path):
                    return fc
                if fc_mode == "only":
                    log.warning(f"  Firecrawl only 模式无结果: {keyword}")
                    return None
                log.info(f"  Firecrawl 未命中/失败 → 回退 Scrapling/Playwright: {keyword}")
        except Exception:
            log.exception(f"  Firecrawl 层异常, 回退: {keyword}")

        # ── 三层自动升级 ──────────────────────────────────────────────────
        try:
            result = triple_scan(keyword, country=country, headless=headless, max_pages=max_pages)
            if result == "NEED_PLAYWRIGHT":
                # L0/L1 失败, 回退到 Playwright
                log.info(f"scrape[{country}|{mode}]: {keyword} (Scrapling → Playwright fallback)")
            elif result is not None and result != "CHALLENGE" and isinstance(result, Path):
                # Scrapling 成功完成! 直接返回
                return result
            elif result == "CHALLENGE":
                return "CHALLENGE"
            elif result is None:
                return None
        except Exception:
            log.exception(f"  Scrapling 三层异常, 回退到 Playwright: {keyword}")

        # ── L1.5 直接翻页 (当 Scrapling L0/L1 失败但 max_pages > 1 时) ────
        from .scrapling_scraper import MAX_PAGES as _DEFAULT_MAX_PAGES
        _effective_pages = max_pages if max_pages is not None else _DEFAULT_MAX_PAGES
        if _effective_pages > 1:
            try:
                l15_result = _patchright_paginate(keyword, country, headless=headless, max_pages=_effective_pages)
                if l15_result is not None and l15_result.get("challenge"):
                    return "CHALLENGE"
                if l15_result is not None and l15_result["count"] > 0:
                    from .scrapling_scraper import save_result
                    out = save_result(l15_result)
                    log.info(f"  L1.5 直接翻页成功: {keyword} → {l15_result['count']}条 ({l15_result.get('pages_loaded', '?')}页)")
                    return out
            except Exception:
                log.exception(f"  L1.5 直接翻页异常, 继续 Playwright: {keyword}")

    # ── L2: Playwright fallback ───────────────────────────────────────
    # Scrapling / Patchright 某些路径会在当前 worker 线程留下运行中的 asyncio
    # loop。Playwright sync API 在这种环境会直接抛错；转入干净线程执行 L2。
    if not _force_l2 and _has_running_asyncio_loop():
        log.warning(f"  L2 Playwright fallback 转入干净线程 (async loop active): {keyword}")
        return _run_l2_fallback_in_clean_thread(keyword, max_scrolls, headless, country, max_pages)

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    url = f"{AD_LIBRARY_BASE}&country={country}&q={keyword.replace(' ', '+')}"
    log.info(f"scrape[{country}|{mode}]: {keyword} (Playwright)")

    # v10: BrowserForge 统计真实指纹
    fp = _generate_fingerprint()
    ua = fp['user_agent']
    viewport = fp['viewport']
    tz = fp['timezone']

    # v9.1: pre-declare so they survive even if the playwright block exits early
    payload = None
    graphql_ads_by_id: dict = {}
    graphql_meta = {'graphql_count': 0, 'ad_responses': 0, 'unique_ads': 0, 'errors': 0}

    with sync_playwright() as p:
        if ANONYMOUS_MODE:
            # 临时浏览器 — 每次全新, 无 cookie, 无 fingerprint 连续性
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ]
            launch_kwargs = dict(headless=headless, args=launch_args)
            pool = get_pool()
            selected_proxy = pool.acquire()
            pw_proxy = proxy_for_playwright(selected_proxy)
            if pw_proxy:
                launch_kwargs['proxy'] = pw_proxy
            browser = p.chromium.launch(**launch_kwargs)
            ctx = browser.new_context(
                viewport=viewport, user_agent=ua,
                locale="en-US", timezone_id=tz,
            )
            _attach_resource_blocker(ctx)
            page = ctx.new_page()
        else:
            # 登录模式 (legacy): 使用持久化 profile
            PROFILE_DIR.mkdir(exist_ok=True)
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=headless,
                viewport=viewport, user_agent=ua,
                locale="en-US", timezone_id=tz,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            _attach_resource_blocker(ctx)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            browser = None

        # 混淆 webdriver 指纹
        try:
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        except Exception:
            pass

        # v9.1: 启动 GraphQL 响应嗅探器 — 拿到的是 FB UI 自己读的 JSON, 字段最齐
        sniffer_state = install_sniffer(page)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PWTimeout:
            log.warning(f"  页面加载超时: {keyword}")
            ctx.close()
            if browser:
                browser.close()
            return None

        # 匿名模式下 FB 可能弹登录对话框, 尝试关闭它
        if ANONYMOUS_MODE:
            try:
                # FB 的登录弹窗: 右上角 X, 或 "不, 谢谢"
                close_selectors = [
                    '[aria-label="Close"]',
                    '[aria-label="关闭"]',
                    'div[role="button"][aria-label*="lose"]',
                ]
                for sel in close_selectors:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            el.click()
                            time.sleep(0.5)
                            break
                    except Exception:
                        pass
            except Exception:
                pass

        # 风控检测 — 匿名模式通常只出现 rate limit
        cur_url = (page.url or "").lower()
        if any(m in cur_url for m in CHALLENGE_MARKERS):
            log.warning(f"  被重定向 (可能 IP 限速): {page.url}")
            ctx.close()
            if browser:
                browser.close()
            return "CHALLENGE"

        try:
            body_sample = page.evaluate("document.body.innerText.substring(0, 500).toLowerCase()")
            if any(m in body_sample for m in ("temporarily restricted", "unusual activity",
                                               "too many requests", "rate limit")):
                log.warning(f"  IP 限速: {body_sample[:100]}")
                ctx.close()
                if browser:
                    browser.close()
                return "CHALLENGE"
        except Exception:
            pass

        _human_initial_pause(page)
        _human_scroll(page, max_scrolls=max_scrolls)
        # v9.4: 滚完等 DOM 稳定 — networkidle 优先, fallback 短停
        try:
            page.wait_for_load_state('networkidle', timeout=3000)
        except Exception:
            pass
        time.sleep(random.uniform(0.3, 0.7))

        try:
            payload = page.evaluate(_get_extract_js())
        except Exception as e:
            err = str(e)
            if 'Execution context was destroyed' in err or 'navigation' in err.lower():
                # FB 在 evaluate 时正好导航 — 等 1.5s 重试一次
                log.info(f"  context destroyed, retry after 1s: {keyword}")
                try:
                    time.sleep(1.0)
                    page.wait_for_load_state('domcontentloaded', timeout=4000)
                    time.sleep(0.3)
                    payload = page.evaluate(_get_extract_js())
                except Exception as e2:
                    log.warning(f"  JS 执行 retry 仍失败 ({keyword}): {e2} — 仅依赖 GraphQL")
                    payload = None
            else:
                log.warning(f"  JS 执行失败 ({keyword}): {err[:120]} — 仅依赖 GraphQL")
                payload = None

        # 关闭前再动一下鼠标 (v9.3: 0.5-1.5 → 0.1-0.3)
        _human_mouse_move(page, random.randint(500, 1200), random.randint(100, 300), steps=4)
        time.sleep(random.uniform(0.05, 0.15))
        # v9.1: snapshot sniffer state before tearing down browser context
        graphql_ads_by_id = dict(sniffer_state.get('ads') or {})
        graphql_meta = {
            'graphql_count': sniffer_state.get('graphql_count', 0),
            'ad_responses': sniffer_state.get('ad_responses', 0),
            'unique_ads': len(graphql_ads_by_id),
            'errors': sniffer_state.get('errors', 0),
        }
        ctx.close()
        if browser:
            browser.close()

    # v9.1: parse DOM payload defensively — if it fails, GraphQL alone is still usable
    data: dict = {}
    if payload is not None:
        try:
            data = json.loads(payload) if isinstance(payload, str) else (payload or {})
        except Exception:
            log.warning(f"  DOM JS 输出非 JSON ({keyword}), 仅用 GraphQL 数据")
            data = {}
    if not isinstance(data, dict):
        data = {}

    results = data.get('results', []) if isinstance(data, dict) else []
    # v9.1: merge dom-extracted results with graphql-sniffed (graphql wins on non-empty)
    if graphql_ads_by_id:
        before = len(results)
        results = merge_dom_with_graphql(results, graphql_ads_by_id)
        log.info(
            f"  graphql merge: dom={before} graphql_uniq={len(graphql_ads_by_id)}"
            f" (responses={graphql_meta['ad_responses']}/{graphql_meta['graphql_count']})"
            f" → final={len(results)}"
        )
    # v9.1: filter out malformed ads (no ad_archive_id or non-digit) AND
    #       non-active ads (URL already says active_status=active, but FB sometimes
    #       returns recently-stopped ads in the same payload — drop them).
    valid: list = []
    dropped_inactive = 0
    for r in results or []:
        if not isinstance(r, dict):
            continue
        aid = str(r.get('ad_archive_id') or r.get('id') or '').strip()
        if not aid or not aid.isdigit() or len(aid) < 8:
            continue
        # Only-active gate: is_active must be truthy. Default True if field missing
        # (some old DOM payloads omit it; URL-level filter already pre-screened).
        is_active = r.get('is_active')
        if is_active is False:
            dropped_inactive += 1
            continue
        valid.append(r)
    if len(valid) != len(results or []):
        log.info(
            f"  validation: dropped {len(results or []) - len(valid)} "
            f"({dropped_inactive} inactive, "
            f"{len(results or []) - len(valid) - dropped_inactive} malformed)"
        )
    results = valid

    # v9.2: 只保留独立站卖货广告 — 政治/lead-gen/marketplace/app/服务类全部 drop
    before_commerce = len(results)
    results, drop_stats = filter_commerce_ads(results)
    if drop_stats:
        top_reasons = sorted(drop_stats.items(), key=lambda kv: -kv[1])[:5]
        reasons_str = ', '.join(f'{k}={v}' for k, v in top_reasons)
        log.info(
            f"  commerce filter: kept {len(results)}/{before_commerce} "
            f"(dropped {sum(drop_stats.values())}: {reasons_str})"
        )

    if not results:
        log.warning(f"  {keyword}: 无结果 (count=0)")
        return None

    today = datetime.now().strftime('%Y-%m-%d')
    slug = keyword.strip().replace(' ', '-').replace('+', 'plus').replace('/', '_')
    out = RESULTS_DIR / f"{today}_{slug}.json"
    payload_out = {
        'keyword': keyword,
        'scanned_at': datetime.now().isoformat(timespec='seconds'),
        'source': 'playwright-anonymous' if ANONYMOUS_MODE else 'playwright-auth',
        'country': country,
        'count': len(results),
        'version': data.get('version', '9.1'),
        'graphql': graphql_meta,
        'results': results,
    }
    with open(out, 'wb') as f:
        # ensure_ascii=False + errors='replace': lone surrogates from JS (e.g. \ud835 fragments
        # of mathematical bold chars) would otherwise crash the UTF-8 encoder.
        f.write(json.dumps(payload_out, ensure_ascii=False, indent=2).encode('utf-8', errors='replace'))
    log.info(f"  保存 {len(results)} 条 -> {out.name}")
    return out


# ── 批量抓取 ────────────────────────────────────────────────────────────────

# v11: adaptive worker count. Base from env, dynamically adjusted by scheduler.
_REQUESTED_SCRAPE_WORKERS = max(1, int(os.getenv('FB_SCRAPE_WORKERS', '10')))
if _NOFILE_SOFT_LIMIT and _NOFILE_SOFT_LIMIT < 1024:
    _CLAMPED_WORKERS = min(_REQUESTED_SCRAPE_WORKERS, max(1, _NOFILE_SOFT_LIMIT // 96))
else:
    _CLAMPED_WORKERS = _REQUESTED_SCRAPE_WORKERS
SCRAPE_WORKERS = _CLAMPED_WORKERS


def scrape_queue(limit: int = 5,
                 delay_range: tuple[float, float] = DEFAULT_KEYWORD_DELAY,
                 headless: bool = True,
                 max_pages: Optional[int] = None) -> list[Path]:
    """取 limit 个未扫描关键词, 并发拟人化抓取. 遵守静默时段/日配额/熔断.

    v10: 支持 FB_SCRAPE_WORKERS 并发 (默认 2). 每个 worker 独立浏览器+指纹.
    """
    from .scan_instructions import scan_queue_loader

    s = _load_state()
    today = datetime.now().strftime('%Y-%m-%d')
    if s.get('today') != today:
        s['today'] = today
        s['today_count'] = 0

    can, why = _can_scrape_now(s)
    if not can:
        log.info(f"本轮跳过: {why}")
        _save_state(s)
        return []

    remaining_quota = (
        DEFAULT_DAILY_QUOTA - s.get('today_count', 0)
        if DEFAULT_DAILY_QUOTA > 0 else limit
    )
    effective = min(limit, remaining_quota) if DEFAULT_DAILY_QUOTA > 0 else limit
    if effective <= 0:
        log.info("今日配额已用完")
        return []

    items = scan_queue_loader()
    if not items:
        log.info("scan_queue 为空")
        return []

    picked = random.sample(items, min(effective, len(items)))
    saved: list[Path] = []
    quota_note = f"配额 {remaining_quota}" if DEFAULT_DAILY_QUOTA > 0 else "无配额"
    scheduler = get_scheduler()
    adaptive_workers = scheduler.recommended_workers()
    workers = min(adaptive_workers, len(picked))
    log.info(f"本轮计划 {len(picked)} 个 (队列 {len(items)}, {quota_note}, "
             f"workers={workers} [adaptive from {SCRAPE_WORKERS}])")

    if workers <= 1:
        # 串行模式 (兼容旧行为)
        return _scrape_serial(picked, s, delay_range, headless, max_pages)

    # v10: 并发模式
    return _scrape_parallel(picked, s, delay_range, headless, max_pages, workers)


def _scrape_serial(picked, s, delay_range, headless, max_pages) -> list[Path]:
    """原始串行抓取逻辑."""
    saved: list[Path] = []
    for i, it in enumerate(picked, 1):
        kw = it['keyword']
        log.info(f"[{i}/{len(picked)}] {kw}")
        try:
            with scrape_proxy_context():
                p = scrape_keyword(kw, headless=headless, max_pages=max_pages)
        except Exception:
            log.exception(f"  采集异常: {kw}")
            p = None

        if p == "CHALLENGE":
            s['consecutive_challenges'] = s.get('consecutive_challenges', 0) + 1
            s['last_challenge_at'] = datetime.now().isoformat(timespec='seconds')
            log.error(f"  触发风控 ({s['consecutive_challenges']}/{MAX_CONSECUTIVE_CHALLENGES})")
            if s['consecutive_challenges'] >= MAX_CONSECUTIVE_CHALLENGES:
                cooldown_end = datetime.now() + timedelta(seconds=CHALLENGE_COOLDOWN_SEC)
                s['cooldown_until'] = cooldown_end.isoformat(timespec='seconds')
                log.error(f"  连续 {MAX_CONSECUTIVE_CHALLENGES} 次风控 — 熔断至 {s['cooldown_until']}")
                _save_state(s)
                break
            _save_state(s)
            time.sleep(20 * 60)
            continue

        if p and isinstance(p, Path):
            saved.append(p)
            s['today_count'] = s.get('today_count', 0) + 1
            s['total_scraped'] = s.get('total_scraped', 0) + 1
            s['consecutive_challenges'] = 0
            s['last_scrape_at'] = datetime.now().isoformat(timespec='seconds')
            _save_state(s)

        if i < len(picked):
            cc = s.get('consecutive_challenges', 0)
            mult = 1.0 + 0.5 * cc
            delay = _human_delay(delay_range) * mult
            time.sleep(delay)

    _save_state(s)
    if saved:
        from .notify import banner
        banner(
            "✨ 采集批次完成",
            f"保存 {len(saved)} 个关键词, 今日累计 {s.get('today_count', 0)}",
            highlight=True,
        )
    return saved


def _scrape_parallel(picked, s, delay_range, headless, max_pages, workers) -> list[Path]:
    """v11 并发抓取: orchestrator + adaptive scheduler + proxy pool."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    saved: list[Path] = []
    batch_results = []
    status_counts: dict[str, int] = {}
    failed_count = 0
    challenge_count = 0
    lock = threading.Lock()
    stop_flag = threading.Event()
    orch = get_orchestrator()
    scheduler = get_scheduler()

    def _worker(item):
        if stop_flag.is_set():
            return None
        kw = item['keyword']
        time.sleep(random.uniform(0.5, 2.0))
        t0 = time.perf_counter()
        try:
            result = orch.scrape(kw, headless=headless, max_pages=max_pages)
            duration = time.perf_counter() - t0
            is_heavy = any(a.layer in ("L1.5", "L2", "L1.3", "L2-thread")
                          for a in result.trace)
            scheduler.record_simple(
                success=result.final_status == "success",
                challenge=result.final_status == "challenge",
                heavy_layer=is_heavy,
                duration_sec=duration,
            )
            return result
        except Exception:
            log.exception(f"  并发采集异常: {kw}")
            scheduler.record_simple(success=False, duration_sec=time.perf_counter() - t0)
            return None

    log.info(f"  并发模式: {workers} workers × {len(picked)} 关键词")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_item = {executor.submit(_worker, it): it for it in picked}

        for future in as_completed(future_to_item):
            if stop_flag.is_set():
                break
            it = future_to_item[future]
            kw = it['keyword']
            try:
                result = future.result()
            except Exception:
                log.exception(f"  future 异常: {kw}")
                result = None

            with lock:
                if result is None:
                    failed_count += 1
                    continue

                batch_results.append(result)
                status_counts[result.final_status] = status_counts.get(result.final_status, 0) + 1
                p = result.legacy_return

                if p == "CHALLENGE":
                    challenge_count += 1
                    s['consecutive_challenges'] = s.get('consecutive_challenges', 0) + 1
                    s['last_challenge_at'] = datetime.now().isoformat(timespec='seconds')
                    log.error(f"  触发风控 ({s['consecutive_challenges']}/{MAX_CONSECUTIVE_CHALLENGES}): {kw}")
                    if s['consecutive_challenges'] >= MAX_CONSECUTIVE_CHALLENGES:
                        cooldown_end = datetime.now() + timedelta(seconds=CHALLENGE_COOLDOWN_SEC)
                        s['cooldown_until'] = cooldown_end.isoformat(timespec='seconds')
                        log.error(f"  熔断! 停止所有 worker")
                        stop_flag.set()
                    _save_state(s)
                elif p and isinstance(p, Path):
                    saved.append(p)
                    s['today_count'] = s.get('today_count', 0) + 1
                    s['total_scraped'] = s.get('total_scraped', 0) + 1
                    s['consecutive_challenges'] = 0
                    s['last_scrape_at'] = datetime.now().isoformat(timespec='seconds')
                    _save_state(s)
                    log.info(f"  [并发] {kw}: {result.ads_count} ads via {result.source_layer} "
                             f"({result.total_duration_sec:.1f}s)")

    _save_state(s)

    pool_stats = get_pool().stats()
    sched_stats = scheduler.stats()
    orch_stats = orch.layer_stats()
    slowest = sorted(batch_results, key=lambda r: r.total_duration_sec, reverse=True)[:3]
    slowest_label = [(r.keyword, r.final_status, r.total_duration_sec, r.source_layer) for r in slowest]
    proxy_errors: dict[str, int] = {}
    for entry in pool_stats.get('entries', []):
        for error_type, count in entry.get('error_types', {}).items():
            proxy_errors[error_type] = proxy_errors.get(error_type, 0) + count
    log.info(
        f"  === 批次统计 === "
        f"saved={len(saved)}/{len(picked)} "
        f"completed={len(batch_results)} failed_none={failed_count} "
        f"challenges={challenge_count} statuses={status_counts} "
        f"slowest={slowest_label} "
        f"proxy=[healthy={pool_stats['healthy']}/{pool_stats['candidates']}, "
        f"switches={pool_stats['total_switches']}, "
        f"direct_takeovers={pool_stats['direct_takeovers']}, "
        f"errors={proxy_errors}] "
        f"scheduler=[workers={sched_stats['current_workers']}, "
        f"fd_pressure={sched_stats['fd_pressure']:.2f}, "
        f"success_rate={sched_stats['recent_success_rate']:.2f}] "
        f"layers={orch_stats}"
    )

    if saved:
        from .notify import banner
        banner(
            "✨ 并发采集完成",
            f"保存 {len(saved)}/{len(picked)} 个关键词, 今日累计 {s.get('today_count', 0)}",
            highlight=True,
        )
    return saved


def _batch_audit(rows: list[dict], runtime_s: float) -> dict:
    status_counts: dict[str, int] = {}
    errors: dict[str, int] = {}
    durations = []
    total_ads = 0
    for row in rows:
        status = str(row.get('status') or 'unknown')
        status_counts[status] = status_counts.get(status, 0) + 1
        total_ads += int(row.get('count') or 0)
        duration = row.get('duration_s')
        if isinstance(duration, (int, float)):
            durations.append(float(duration))
        if row.get('error'):
            prefix = str(row['error']).replace('http://', '').replace('https://', '')[:120]
            errors[prefix] = errors.get(prefix, 0) + 1

    duration_stats = {}
    if durations:
        ordered = sorted(durations)
        p95_idx = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
        duration_stats = {
            'min': round(ordered[0], 2),
            'median': round(statistics.median(ordered), 2),
            'p95': round(ordered[p95_idx], 2),
            'max': round(ordered[-1], 2),
        }

    total = len(rows) or 1
    audit = {
        'status_counts': status_counts,
        'total_ads': total_ads,
        'challenge_rate': round(status_counts.get('challenge', 0) / total, 4),
        'error_rate': round(status_counts.get('error', 0) / total, 4),
        'duration': duration_stats,
        'slowest': sorted(rows, key=lambda r: float(r.get('duration_s') or 0), reverse=True)[:3],
        'errors': errors,
        'keywords_per_minute': round((len(rows) / runtime_s) * 60, 2) if runtime_s > 0 else 0,
    }
    baseline = float(os.getenv('FB_SCRAPE_BASELINE_RUNTIME_SEC', '0') or 0)
    if baseline > 0 and runtime_s > 0:
        audit['speedup_pct_vs_baseline'] = round((baseline - runtime_s) / baseline * 100, 2)
    return audit


def scrape_keywords_v3(keywords: list[str], max_scrolls: int = 5, headless: bool = True,
                       country: Optional[str] = None, fast: bool = True,
                       delay_sec: float = 0.15, refresh_every: int = 10,
                       max_pages: Optional[int] = None) -> dict:
    """V3 指定关键词批量抓取: 复用 NEW 多层高速 scrape_keyword, 返回结构化结果."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    country = (country or DEFAULT_AD_LIBRARY_COUNTRY).strip().upper()
    clean_keywords = []
    seen = set()
    for kw in keywords:
        kw = str(kw).strip().lower()
        if 2 <= len(kw) <= 120 and kw not in seen:
            seen.add(kw)
            clean_keywords.append(kw)

    started = time.time()
    workers = min(max(1, SCRAPE_WORKERS), len(clean_keywords) or 1)
    rows = []
    saved = []
    challenges = 0

    def _run(kw: str) -> dict:
        t0 = time.time()
        try:
            with scrape_proxy_context():
                p = scrape_keyword(kw, max_scrolls=max_scrolls, headless=headless, country=country, max_pages=max_pages)
            if p == "CHALLENGE":
                return {'keyword': kw, 'ok': False, 'status': 'challenge', 'count': 0, 'file': None, 'duration_s': round(time.time() - t0, 2)}
            if p and isinstance(p, Path):
                count = 0
                try:
                    data = json.loads(p.read_text(encoding='utf-8'))
                    count = int(data.get('count') or len(data.get('results') or []))
                except Exception:
                    pass
                return {'keyword': kw, 'ok': True, 'status': 'saved', 'count': count, 'file': str(p), 'duration_s': round(time.time() - t0, 2)}
            return {'keyword': kw, 'ok': False, 'status': 'empty_or_failed', 'count': 0, 'file': None, 'duration_s': round(time.time() - t0, 2)}
        except Exception as e:
            log.exception(f"v3 指定关键词抓取异常: {kw}")
            return {'keyword': kw, 'ok': False, 'status': 'error', 'error': str(e), 'count': 0, 'file': None, 'duration_s': round(time.time() - t0, 2)}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_kw = {ex.submit(_run, kw): kw for kw in clean_keywords}
        for fut in as_completed(future_to_kw):
            row = fut.result()
            rows.append(row)
            if row.get('status') == 'challenge':
                challenges += 1
            if row.get('ok') and row.get('file'):
                saved.append(row['file'])
            if delay_sec > 0:
                time.sleep(min(delay_sec, 1.0))

    rows.sort(key=lambda r: clean_keywords.index(r['keyword']) if r['keyword'] in clean_keywords else 9999)
    runtime_s = round(time.time() - started, 2)
    return {
        'ok': True,
        'requested': len(keywords),
        'parsed': len(clean_keywords),
        'saved': len(saved),
        'challenge_count': challenges,
        'runtime_s': runtime_s,
        'workers': workers,
        'files': saved,
        'results': rows,
        'audit': _batch_audit(rows, runtime_s),
    }


# ── 关键词自主学习闭环 ──────────────────────────────────────────────────────

def ingest_rabbit_hole_into_queue() -> int:
    """把 scan_queue_rabbit_hole.txt 里的新词自动合并进 facebook-ad-keywords-scan.txt.
    去重: 已在 scan_file 或 learned_file 里的不追加.
    """
    from .paths import SCAN_FILE, LEARNED_FILE, RABBIT_QUEUE

    if not RABBIT_QUEUE.exists():
        return 0

    # 已有关键词 (规范化小写)
    known: set[str] = set()
    for f in (SCAN_FILE, LEARNED_FILE):
        if f.exists():
            for line in f.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                kw = line.lstrip('#').strip().split('|')[0].strip().lower()
                if kw:
                    known.add(kw)

    new_lines = []
    for line in RABBIT_QUEUE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        kw = line.split('|')[0].strip().lower()
        if kw and kw not in known:
            new_lines.append(line)
            known.add(kw)

    if not new_lines:
        return 0

    today = datetime.now().strftime('%Y-%m-%d')
    with open(SCAN_FILE, 'a', encoding='utf-8') as f:
        f.write(f"\n# === 自主学习入队 {today} ({len(new_lines)} 个) ===\n")
        for line in new_lines:
            f.write(f"{line}\n")

    log.info(f"自主学习: 入队 {len(new_lines)} 个新关键词")
    return len(new_lines)


# ── Daemon 主循环 ──────────────────────────────────────────────────────────

STALE_ALERT_HOURS = float(os.getenv('FB_STALE_ALERT_HOURS', '3'))


def _watchdog_check() -> None:
    """每个 daemon 周期开头调用. 检测数据停滞, 发通知.

    触发条件:
    - 上次成功 scrape > STALE_ALERT_HOURS 小时前, 且当前可以 scrape
    - scan_queue 为空 (队列枯竭, 需要补关键词)
    """
    from .notify import both as notify_both
    from .scan_instructions import scan_queue_loader

    s = _load_state()
    last = s.get('last_scrape_at')
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            hours = (datetime.now() - last_dt).total_seconds() / 3600
            can, why = _can_scrape_now(s)
            if hours >= STALE_ALERT_HOURS and can:
                notify_both(
                    "⚠️ 数据停滞警告",
                    f"{hours:.1f}h 没有新采集, 请检查 pipeline.log",
                    f"当前状态: {why or 'ok'}",
                    highlight=True,
                )
        except Exception:
            pass

    try:
        items = scan_queue_loader()
        if not items:
            notify_both(
                "⚠️ 扫描队列枯竭",
                "facebook-ad-keywords-scan.txt 已空, 自动触发外部平台关键词收割",
                highlight=True,
            )
            # auto-harvest from external platforms (TikTok / Reddit / Google Trends / Amazon)
            try:
                from .keyword_harvester import harvest_all
                summary = harvest_all(target=300)
                log.info(f"auto-harvest: {summary}")
            except Exception:
                log.exception("auto-harvest failed")
        elif len(items) < 50:
            # low-water mark: top up before queue exhausts
            try:
                from .keyword_harvester import harvest_all
                summary = harvest_all(target=200)
                log.info(f"low-water harvest: queue={len(items)} → {summary}")
            except Exception:
                log.exception("low-water harvest failed")
    except Exception:
        pass


def daemon_loop(scrape_limit: int = 3, cycle_minutes: int = 45,
                headless: bool = True, max_pages: Optional[int] = None) -> None:
    import signal
    import threading
    from .raw_processor import process_raw_results
    from .excel_writer import update_excel
    from .intelligence import update_brand_intelligence
    from .rabbit_hole import generate_rabbit_hole_queue
    from .recommendations import rebuild_recommendations
    from .categories import ingest_category_seeds
    from .strategies import run_all_strategies
    from .ai_scorer import score_unscored_ads
    from .preflight import run_preflight

    log.info(f"daemon 启动: 每轮 {scrape_limit} 个, 周期 {cycle_minutes} 分钟")

    preflight = run_preflight(fix=True)
    if preflight["status"] == "fatal":
        log.error(f"PREFLIGHT FATAL — daemon 无法启动. 修复后重试.")
        return

    # Graceful shutdown: SIGTERM/SIGINT trip the event so sleep wakes up immediately.
    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        log.info(f"daemon 收到信号 {signum}, 准备优雅退出")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass

    # 开机先确保类目种子入队
    try:
        ingest_category_seeds()
    except Exception:
        log.exception("类目种子入队异常")

    cycle = 0
    while not stop_event.is_set():
        cycle += 1
        log.info(f"=== 周期 #{cycle} @ {datetime.now().isoformat(timespec='seconds')} ===")

        try:
            _watchdog_check()
        except Exception:
            log.exception("watchdog 异常")

        try:
            saved = scrape_queue(limit=scrape_limit, headless=headless, max_pages=max_pages)
        except Exception:
            log.exception("采集阶段异常")
            saved = []

        if stop_event.is_set():
            break

        try:
            if saved:
                n = process_raw_results(force=False, workers=2)
                log.info(f"  处理 {n} 个文件")
                update_excel()
                update_brand_intelligence()
                generate_rabbit_hole_queue()
                ingest_rabbit_hole_into_queue()
                ingest_category_seeds()
                rebuild_recommendations()
                scored = score_unscored_ads(limit=3000, min_score=0)
                log.info(f"  AI 自动评分: eligible={scored['eligible']} scored={scored['scored']}")
            else:
                ingest_rabbit_hole_into_queue()
                ingest_category_seeds()
                rebuild_recommendations()
                scored = score_unscored_ads(limit=1000, min_score=0)
                if scored['eligible']:
                    log.info(f"  AI 自动补评分: eligible={scored['eligible']} scored={scored['scored']}")
        except Exception:
            log.exception("处理阶段异常")

        # 每 6 个周期跑一次三种策略 (3h 频率, 不耗性能)
        if cycle % 6 == 0:
            try:
                run_all_strategies()
            except Exception:
                log.exception("策略选品异常")

        # 每 24 个周期做一次 SQLite VACUUM
        if cycle % 24 == 0:
            try:
                from .persistence import get_persistence
                get_persistence().vacuum()
                log.info("  SQLite VACUUM 完成")
            except Exception:
                log.exception("VACUUM 异常")

        if stop_event.is_set():
            break

        jitter = random.uniform(-0.2, 0.2)
        sleep_sec = max(60.0, cycle_minutes * 60 * (1 + jitter))
        log.info(f"=== 休眠 {sleep_sec/60:.1f} 分钟 ===")
        # Interruptible sleep: wake immediately on SIGTERM/SIGINT
        if stop_event.wait(timeout=sleep_sec):
            break

    log.info(f"daemon 退出 (共完成 {cycle} 个周期)")

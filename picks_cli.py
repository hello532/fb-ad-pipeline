#!/usr/bin/env python3
"""picks_cli — FB 选品离线决策出口（NEW skill 化 v1，2026-07-26）

为什么存在
----------
pipeline.db 里躺着 4.7 万行 0-120 分推荐、6.8 万品牌、2700+ 关键词扫描，
但出口只有 dashboard（人眼）和 HTTP API（要手工 curl）。本 CLI 把「选品决策」
变成一条命令，全离线、只读，口径与 server 同源：

  * picks     → 复用 pipeline.recommendations.list_recommendations（/recommendations 同源）
  * keyword   → 读 RESULTS_DIR 最新 *_corrected.json（/scans/{kw} 同源）
  * export-r8 → 把 keyword 竞争面折算成 gshopping-rising deep-verify 证据槽
  * profit-screen → 用真实售价/成本执行 r9 downside 利润预筛

设计规则
--------
1. 永不写库：keyword/export-r8 纯文件读；picks/brand 用 sqlite `mode=ro`。
2. 口径复用：平台剔除用 pipeline.commerce_filter 的判定，不自造清单。
3. 新鲜度铁律：scan 距今 >14 天标 [STALE] 并给重扫命令；绝不把旧数据当今天的事实。
4. 不编造：查无扫描 → 明说 no_scan 并给抓取命令，不猜数字。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pipeline.paths import RESULTS_DIR  # noqa: E402
from pipeline.commerce_filter import _is_marketplace, _norm_domain  # noqa: E402

DB_PATH = HERE / "pipeline.db"
STALE_DAYS = 14
AD_CAP = 30  # 单词单次抓取上限；ads 达到该值=密度截断，绝对数不可比，只用比率型指标
API_BASE = "http://127.0.0.1:8000"
GSHOPPING_SCRIPTS = Path(os.getenv(
    "GSHOPPING_SCRIPTS",
    str(Path(__file__).resolve().parent.parent / "gshopping-rising" / "scripts")))
_UNIT_ECON_FOR = None


# ------------------------------------------------------------------ helpers
def _today() -> date:
    return date.today()


def _ro_conn(db: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db or DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _norm_kw(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _is_brand_self(keyword: str, brand_name: str, domain: str) -> bool:
    """keyword 本身就是品牌名/域名 → 品牌自播广告，不是品类需求。"""
    k = _norm_kw(keyword)
    if not k:
        return False
    b = _norm_kw(brand_name)
    d = _norm_kw((domain or "").split(".")[0])
    return bool(b and (k == b or k in b or b in k)) or bool(d and k.replace(" ", "") == d)


def _scan_file_for(keyword: str) -> Path | None:
    safe = keyword.replace(" ", "-").replace("+", "plus")
    matches = sorted(Path(RESULTS_DIR).glob(f"*_{safe}_corrected.json"))
    return matches[-1] if matches else None


def _scan_date_of(path: Path) -> str | None:
    m = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
    return m.group(1) if m else None


def _age_days(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (_today() - datetime.strptime(iso, "%Y-%m-%d").date()).days
    except ValueError:
        return None


def _load_scan(keyword: str) -> dict | None:
    p = _scan_file_for(keyword)
    if not p:
        return None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    data["_file"] = str(p)
    data["_scan_date"] = _scan_date_of(p)
    data["_age_days"] = _age_days(data["_scan_date"])
    data["_stale"] = (data["_age_days"] or 0) > STALE_DAYS
    return data


_PLATFORM_WORDS = ("temu", "amazon", "shein", "walmart", "aliexpress", "ebay", "wish")


def _looks_platform_alias(brand: str, landing: str, landing_url: str) -> bool:
    """平台马甲识别：brand/域名含平台词（如 Shoptemu→shoptemu.com 实为 temu 深链壳）。"""
    blob = _norm_kw(f"{brand} {landing}").replace(" ", "")
    if any(w in blob for w in _PLATFORM_WORDS):
        return True
    return any(w in (landing_url or "").lower() for w in _PLATFORM_WORDS)


def _split_competitors(scan: dict) -> tuple[list[dict], list[dict]]:
    """独立站 vs 平台（Temu/Amazon/马甲域/深链壳，commerce_filter 口径 + 别名启发）。"""
    dtc, platforms = [], []
    for c in scan.get("top_competitors") or []:
        landing = c.get("landing") or ""
        # corrected.json 里的 URL 带 \/ 转义，先清洗再解析域名
        url = (c.get("landing_url") or f"https://{landing}").replace("\\/", "/")
        dom = _norm_domain(url)
        if (
            _is_marketplace(dom)
            or _is_marketplace(landing)
            or _looks_platform_alias(c.get("brand") or "", landing, url)
        ):
            platforms.append(c)
        else:
            dtc.append(c)
    return dtc, platforms


def _rescan_hint(keyword: str) -> str:
    return (
        f'cd {HERE} && python3 fb-scan-pipeline-v8.py --v3-scrape "{keyword}" '
        f"--v3-country US --v3-max-scrolls 5 --max-pages 1"
    )


def _is_source_ref(value: str | None) -> bool:
    """Accept auditable web/file references, not free-form source claims."""
    return isinstance(value, str) and value.strip().startswith(("https://", "http://", "file://"))


def _load_unit_econ_for():
    """Load the canonical r9 profit model without copying its business rules."""
    global _UNIT_ECON_FOR
    if _UNIT_ECON_FOR is not None:
        return _UNIT_ECON_FOR
    model_file = GSHOPPING_SCRIPTS / "deep_verify.py"
    if not model_file.exists():
        raise RuntimeError(f"profit model missing: {model_file}")
    if str(GSHOPPING_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(GSHOPPING_SCRIPTS))
    from deep_verify import unit_econ_for

    _UNIT_ECON_FOR = unit_econ_for
    return _UNIT_ECON_FOR


def _signal_only(row: dict) -> dict:
    """Mark a recommendation score as demand evidence, never a profit verdict."""
    return {
        **row,
        "commercial_status": "UNVERIFIED_UNIT_ECON",
        "next_gate": "picks_cli.py profit-screen, then gshopping deep_verify ingest",
    }


def _profit_screen_decision(view: dict, econ: dict) -> tuple[str, str, str]:
    """Return (decision, reason, next_action) for the local pre-screen."""
    if econ.get("unit_econ_verdict") == "dead":
        return (
            "KILL_UNIT_ECON",
            f"downside breakeven ROAS {econ.get('breakeven_roas') or '∞'} 不成立",
            "改价格、bundle、供应链或放弃；不要靠广告分数挽救",
        )
    if view.get("status") != "ok":
        return "HOLD_NO_FB_SCAN", "本地没有该词扫描，竞争证据未知", view.get("rescan") or "先重扫"
    if view.get("stale"):
        return "HOLD_STALE_FB", f"FB 扫描已 {view.get('age_days')} 天", view.get("rescan") or "先重扫"
    if int(view.get("dtc_count") or 0) < 3:
        return "HOLD_WEAK_FB_EVIDENCE", "活跃 DTC 独立站少于 3 家，需求尚未被广告验证", "补其它需求证据或继续观察"
    if int(view.get("dtc_count") or 0) >= 25:
        return "HOLD_CROWDED_FB", "活跃 DTC 独立站至少 25 家，需先证明差异化角度", "进入 deep_verify 检查饱和与差异化"
    return "READY_FOR_DEEP_VERIFY", "FB 竞争面新鲜且 downside 单位经济学可测", "导出 r8 槽并运行 gshopping deep_verify ingest"


# ------------------------------------------------------------------ picks
BULKY_RE = re.compile(
    r"\b(pillow|mattress|blanket|sofa|couch|furniture|chair|table|desk|crib|dresser|cabinet|"
    r"wardrobe|treadmill|sauna|refrigerator|freezer|washer|dryer|trampoline|bathtub|"
    r"massage chair|standing desk|dog house|cat tree|hammock stand)\.?s?\b", re.I)


def cmd_picks(args: argparse.Namespace) -> int:
    from pipeline.recommendations import list_recommendations

    dropped_bulky = 0

    rows = list_recommendations(
        limit=max(args.limit * 6, 300),
        min_score=args.min_score,
        days=args.days,
        only_shopify=args.only_shopify,
    )
    out = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        dom = (d.get("domain") or "").lower()
        if _is_marketplace(dom):
            continue
        if not args.include_brand_self and _is_brand_self(
            d.get("keyword") or "", d.get("brand_name") or "", dom
        ):
            continue
        d["_bulky"] = bool(BULKY_RE.search((d.get("keyword") or "").lower()))
        if getattr(args, "light_only", False) and d["_bulky"]:
            dropped_bulky += 1
            continue
        out.append(_signal_only(d))
        if len(out) >= args.limit:
            break

    truncated = sum(1 for d in out if (d.get("ads_count") or 0) >= AD_CAP)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    if not out:
        print(f"no picks (min_score={args.min_score} days={args.days}) — 先跑抓取或放宽条件")
        return 1
    print(f"# FB SIGNAL PICKS · min_score={args.min_score} · days={args.days or 'all'} · 非利润结论")
    print(f"{'score':>5} {'orbit':>5} {'ads':>4} {'age_d':>5}  {'keyword':30.30} {'brand':22.22} {'domain':28.28} pain")
    if getattr(args, "light_only", False) and dropped_bulky:
        print(f"# [light-only] 已剔除 {dropped_bulky} 行大件重货（枕/毯/家具/器械类），只留直发友好小件")
    for d in out:
        cap_mark = "≈" if (d.get("ads_count") or 0) >= AD_CAP else " "
        heavy_mark = "📦" if d.get("_bulky") else ""
        print(
            f"{d.get('score') or 0:>5} {d.get('orbit_score') or 0:>5} {d.get('ads_count') or 0:>4} "
            f"{d.get('ad_age_days') or 0:>5}  {(d.get('keyword') or '—'):28.28}{cap_mark} "
            f"{(d.get('brand_name') or '—'):22.22} {(d.get('domain') or '—'):28.28} "
            f"{heavy_mark}{(d.get('top_pain') or '')[:26]}"
        )
    if truncated:
        print(f"\n! [密度截断] {truncated} 行标 ≈：ads≥{AD_CAP} 为抓取上限，绝对数不可比——只用长跑率/new_share/印钞数等比率指标。")
    print("\n! 下一闸门：对候选运行 profit-screen；FB 分数本身不得直接触发花钱测试。")
    return 0


# ------------------------------------------------------------------ keyword
def _keyword_view(keyword: str) -> dict:
    scan = _load_scan(keyword)
    if not scan:
        return {"keyword": keyword, "status": "no_scan", "rescan": _rescan_hint(keyword)}
    dtc, platforms = _split_competitors(scan)
    oldest = max((int(c.get("ad_age_days") or 0) for c in dtc), default=0)
    return {
        "keyword": keyword,
        "status": "ok",
        "scan_date": scan["_scan_date"],
        "age_days": scan["_age_days"],
        "stale": scan["_stale"],
        "ads_total": scan.get("ads") or scan.get("count"),
        "density_truncated": (scan.get("ads") or scan.get("count") or 0) >= AD_CAP,
        "brands_total": scan.get("brands"),
        "dtc_count": len(dtc),
        "platform_count": len(platforms),
        "oldest_dtc_ad_days": oldest,
        "dtc": [
            {
                "brand": c.get("brand"),
                "landing": c.get("landing"),
                "ads": c.get("ads"),
                "ad_age_days": c.get("ad_age_days"),
                "category": c.get("ad_age_category"),
                "brand_score": c.get("brand_score"),
                "active": c.get("is_active"),
            }
            for c in dtc
        ],
        "platforms": [
            {"brand": c.get("brand"), "landing": c.get("landing"), "ads": c.get("ads")}
            for c in platforms
        ],
        "file": scan["_file"],
        "rescan": _rescan_hint(keyword) if scan["_stale"] else None,
    }


def cmd_keyword(args: argparse.Namespace) -> int:
    view = _keyword_view(args.keyword)
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
        return 0 if view["status"] == "ok" else 1
    if view["status"] != "ok":
        print(f"[no_scan] {args.keyword} — 本地库无扫描记录，先抓：\n  {view['rescan']}")
        return 1
    tag = " [STALE>14d]" if view["stale"] else ""
    cap_tag = " [密度截断·比率才可信]" if view.get("density_truncated") else ""
    print(f"# {view['keyword']} · scan {view['scan_date']} ({view['age_days']}d){tag}{cap_tag}")
    print(f"ads={view['ads_total']} brands={view['brands_total']} · 独立站 {view['dtc_count']} 家 · 平台 {view['platform_count']} 家 · 最老独立站广告 {view['oldest_dtc_ad_days']}d")
    for c in view["dtc"]:
        print(f"  DTC  {c['brand'] or '?':24.24} {c['landing'] or '?':30.30} ads={c['ads']:<3} age={c['ad_age_days']}d {c['category'] or ''}")
    for c in view["platforms"]:
        print(f"  PLAT {c['brand'] or '?':24.24} {c['landing'] or '?':30.30} ads={c['ads']}")
    if view["rescan"]:
        print(f"! 数据超过 {STALE_DAYS} 天，重扫：\n  {view['rescan']}")
    return 0


# ------------------------------------------------------------------ brand
def cmd_brand(args: argparse.Namespace) -> int:
    conn = _ro_conn()
    dom = args.domain.lower().strip()
    row = conn.execute("SELECT * FROM brands WHERE domain = ?", (dom,)).fetchone()
    if not row:
        rows = conn.execute(
            "SELECT domain FROM brands WHERE domain LIKE ? LIMIT 8", (f"%{dom}%",)
        ).fetchall()
        print(f"[not_found] {dom}" + (f" · 近似: {[r['domain'] for r in rows]}" if rows else ""))
        return 1
    kws = [
        r["keyword"]
        for r in conn.execute(
            "SELECT keyword FROM brand_keywords WHERE domain = ? LIMIT 20", (dom,)
        )
    ]
    d = dict(row)
    d.pop("raw_json", None)
    d["keywords"] = kws
    print(json.dumps(d, ensure_ascii=False, indent=2))
    return 0


# ------------------------------------------------------------------ export-r8
def cmd_export_r8(args: argparse.Namespace) -> int:
    """输出 gshopping-rising deep-verify 证据槽片段（slots 直接可粘进 evidence todo）。"""
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    out = {}
    for kw in keywords:
        view = _keyword_view(kw)
        if view["status"] != "ok":
            out[kw] = {"status": "no_scan", "note": "本地 FB 库无此词扫描；勿编造，先抓取", "rescan": view["rescan"]}
            continue
        src_file = f"file://{view['file']}"
        src_api = f"{API_BASE}/scans/{kw.replace(' ', '%20')}"
        note_common = (
            f"本地 FB 广告库 scan {view['scan_date']}（{view['age_days']}d 前，US 采样）；"
            f"独立站口径已剔平台 {view['platform_count']} 家"
            + ("；[STALE>14d 建议重扫后再用]" if view["stale"] else "")
        )
        dtc_names = ", ".join(f"{c['brand']}({c['ads']}ads/{c['ad_age_days']}d)" for c in view["dtc"][:8])
        out[kw] = {
            "status": "ok",
            "stale": view["stale"],
            "slots": {
                "fb_active_ads": {
                    "value": view["dtc_count"],
                    "note": f"{note_common}；名单: {dtc_names or '无'}",
                    "sources": [src_api, src_file],
                    "checked_at": view["scan_date"],
                },
                "fb_oldest_ad_days": {
                    "value": view["oldest_dtc_ad_days"],
                    "note": note_common,
                    "sources": [src_api, src_file],
                    "checked_at": view["scan_date"],
                },
                "dtc_competitor_count": {
                    "value": view["dtc_count"],
                    "note": note_common,
                    "sources": [src_api, src_file],
                    "checked_at": view["scan_date"],
                },
            },
        }
    payload = {
        "schema": "gshopping.deep_verify.fb_slots.v1",
        "generated": _today().isoformat(),
        "stale_days_threshold": STALE_DAYS,
        "keywords": out,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)  # --out 父目录缺失时自动创建
        out_p.write_text(text, encoding="utf-8")
        print(f"wrote → {args.out}")
    else:
        print(text)
    return 0


# ------------------------------------------------------------------ r9 profit screen
def cmd_profit_screen(args: argparse.Namespace) -> int:
    """Combine fresh FB evidence with sourced downside unit economics."""
    if args.aov <= 0 or args.cogs_low <= 0 or args.cogs_high <= 0:
        print("ERROR: AOV and COGS must be positive", file=sys.stderr)
        return 2
    if args.cogs_low > args.cogs_high:
        print("ERROR: --cogs-low cannot exceed --cogs-high", file=sys.stderr)
        return 2
    if not _is_source_ref(args.retail_source) or not _is_source_ref(args.supplier_source):
        print("ERROR: --retail-source and --supplier-source must be http(s):// or file:// references", file=sys.stderr)
        return 2

    try:
        unit_econ_for = _load_unit_econ_for()
        econ = unit_econ_for(
            aov=args.aov,
            cogs_range=[args.cogs_low, args.cogs_high],
            structure=args.structure,
            return_risk=args.return_risk,
            cac_safety_ratio=args.cac_safety_ratio,
        )
    except Exception as exc:
        print(f"ERROR: profit model unavailable: {exc}", file=sys.stderr)
        return 2

    view = _keyword_view(args.keyword)
    decision, reason, next_action = _profit_screen_decision(view, econ)
    payload = {
        "schema": "fb.profit_screen.v1",
        "generated": _today().isoformat(),
        "keyword": args.keyword,
        "decision": decision,
        "reason": reason,
        "next_action": next_action,
        "commercial_inputs": {
            "aov_usd": args.aov,
            "cogs_range_usd": [args.cogs_low, args.cogs_high],
            "retail_source": args.retail_source,
            "supplier_source": args.supplier_source,
            "checked_at": args.checked_at or _today().isoformat(),
        },
        "fb_signal": view,
        "unit_econ": {**econ, "commercial_confidence": "verified_input"},
        "scope": "pre_screen_only; final GO requires gshopping deep_verify ingest",
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"# {args.keyword} · {decision}")
        print(f"原因: {reason}")
        cs = econ.get("cash_speed") or {}
        lane_tag = "⚡快钱通道(直发可行)" if cs.get("lane") == "fast" else "🐢慢通道(直发不成立·须美仓备货压资金)"
        print(
            f"downside BE ROAS={econ.get('breakeven_roas')} · "
            f"目标 CAC=${econ.get('target_cac_usd')} · 目标 ROAS={econ.get('target_roas')} · "
            f"利润缓冲=${econ.get('profit_buffer_usd')} · 履约={econ.get('best_mode')}"
        )
        print(f"现金通道: {lane_tag} · 直发BE={cs.get('direct_parcel_breakeven_roas')} — {cs.get('note')}")
        print(f"下一步: {next_action}")
        print("范围: 这是预筛，不是 GO；最终花钱许可只来自 gshopping MONEY_DECISION.md。")
    return 1 if decision.startswith("KILL") else 0


# ------------------------------------------------------------------ doctor / selftest
def cmd_doctor(_args: argparse.Namespace) -> int:
    ok = True
    def chk(name, cond, detail=""):
        nonlocal ok
        print(f"[{'OK ' if cond else 'FAIL'}] {name}: {detail}")
        ok = ok and bool(cond)

    chk("db:exists", DB_PATH.exists(), f"{DB_PATH} ({DB_PATH.stat().st_size/1e6:.0f}MB)" if DB_PATH.exists() else "missing")
    wal = DB_PATH.with_suffix(".db-wal")
    if wal.exists():
        big = wal.stat().st_size > 300e6
        print(f"[{'WARN' if big else 'OK '}] db:wal_size: {wal.stat().st_size/1e6:.0f}MB" + ("（>300MB，可在服务空闲时 checkpoint；本 CLI 不自动执行）" if big else ""))
    try:
        conn = _ro_conn()
        n = conn.execute("SELECT COUNT(*) FROM recommendations").fetchone()[0]
        chk("db:recommendations", n > 0, f"{n} rows")
    except Exception as e:
        chk("db:readable", False, str(e))
    rd = Path(RESULTS_DIR)
    files = list(rd.glob("*_corrected.json")) if rd.exists() else []
    chk("results_dir", bool(files), f"{rd} ({len(files)} corrected files)")
    try:
        chk("profit_model:r9", callable(_load_unit_econ_for()), str(GSHOPPING_SCRIPTS / "deep_verify.py"))
    except Exception as e:
        chk("profit_model:r9", False, str(e))
    if files:
        latest = max(files, key=lambda p: p.name)
        age = _age_days(_scan_date_of(latest))
        age_v = age if age is not None else 99
        print(f"[{'WARN' if age_v > STALE_DAYS else 'OK '}] freshness: latest scan {_scan_date_of(latest)} ({age}d)")
    import socket
    s = socket.socket(); s.settimeout(0.5)
    alive = s.connect_ex(("127.0.0.1", 8000)) == 0
    s.close()
    print(f"[INFO] service:8000: {'online（dashboard 可用）' if alive else 'offline（本 CLI 不依赖它，正常）'}")
    print("-" * 50)
    print("DOCTOR", "OK" if ok else "FAIL")
    return 0 if ok else 1


def cmd_selftest(_args: argparse.Namespace) -> int:
    checks = []
    # 1) brand-self 降噪
    checks.append(("brand_self_filter",
                   _is_brand_self("Ben Greenfield Fitness", "Ben Greenfield Fitness", "bengreenfieldfitness.com")
                   and not _is_brand_self("knee compression sleeve", "Kliyra", "kliyra.com"), ""))
    # 2) marketplace 口径（复用 commerce_filter）
    checks.append(("marketplace_filter",
                   _is_marketplace("temu.com") and not _is_marketplace("kliyra.com"), "temu剔/kliyra留"))
    # 3) 真实 fixture：knee compression sleeve（若存在）
    view = _keyword_view("knee compression sleeve")
    if view["status"] == "ok":
        cond = view["dtc_count"] >= 3 and view["oldest_dtc_ad_days"] >= 200 and view["platform_count"] >= 1
        checks.append(("fixture_knee", cond,
                       f"dtc={view['dtc_count']} oldest={view['oldest_dtc_ad_days']}d plat={view['platform_count']}"))
        # 4) export-r8 结构
        ns = argparse.Namespace(keywords="knee compression sleeve", out=None)
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_export_r8(ns)
        payload = json.loads(buf.getvalue())
        slots = payload["keywords"]["knee compression sleeve"]["slots"]
        cond = all(k in slots for k in ("fb_active_ads", "fb_oldest_ad_days", "dtc_competitor_count")) \
            and all(s["sources"] for s in slots.values())
        checks.append(("export_r8_shape", cond, "3 slots + sources"))
    else:
        checks.append(("fixture_knee", True, "skip: no local scan (soft)"))
    # 5) no_scan 不编造
    v2 = _keyword_view("zzz nonexistent keyword 42")
    checks.append(("no_scan_honest", v2["status"] == "no_scan" and "rescan" in v2, ""))
    # 6) recommendation scores are explicitly signal-only
    marked = _signal_only({"keyword": "demo", "score": 99})
    checks.append(("signal_not_profit",
                   marked["commercial_status"] == "UNVERIFIED_UNIT_ECON", marked["next_gate"]))
    checks.append(("source_ref_validation",
                   _is_source_ref("https://shop.example/p") and not _is_source_ref("trust me"), ""))
    # 7) canonical r9 economics + FB state routing
    try:
        unit_econ_for = _load_unit_econ_for()
        econ = unit_econ_for(aov=44, cogs_range=[4, 6], structure="pain_device")
        fresh = {"status": "ok", "stale": False, "dtc_count": 6}
        dec = _profit_screen_decision(fresh, econ)[0]
        checks.append(("profit_screen_ready", dec == "READY_FOR_DEEP_VERIFY",
                       f"{dec} target_cac={econ.get('target_cac_usd')}"))
        econ_dead = unit_econ_for(aov=25, cogs_range=[18, 20], structure="home_organizer_light")
        dead = _profit_screen_decision(fresh, econ_dead)[0]
        checks.append(("profit_screen_dead", dead == "KILL_UNIT_ECON", dead))
        stale = _profit_screen_decision({"status": "ok", "stale": True, "age_days": 30, "dtc_count": 6}, econ)[0]
        checks.append(("profit_screen_stale", stale == "HOLD_STALE_FB", stale))
    except Exception as e:
        checks.append(("profit_model_import", False, str(e)))

    fail = 0
    for name, ok, detail in checks:
        print(f"[{'OK ' if ok else 'FAIL'}] picks_cli:{name}: {detail}")
        fail += 0 if ok else 1
    print("-" * 50)
    print(f"picks_cli selftest: {len(checks)-fail}/{len(checks)} passed")
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="FB 选品离线决策 CLI（NEW skill 出口）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("picks", help="Top 选品决策表（/recommendations 同源 + 降噪）")
    p.add_argument("--min-score", type=int, default=80)
    p.add_argument("--days", type=int, default=30, help="0=全部历史")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--only-shopify", action="store_true")
    p.add_argument("--light-only", action="store_true",
                   help="剔除大件重货（枕/毯/家具/器械），只留直发友好小件——快钱通道前置过滤")
    p.add_argument("--include-brand-self", action="store_true", help="保留品牌自播行")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_picks)

    k = sub.add_parser("keyword", help="单词竞争全景（/scans/{kw} 同源）")
    k.add_argument("keyword")
    k.add_argument("--json", action="store_true")
    k.set_defaults(fn=cmd_keyword)

    b = sub.add_parser("brand", help="品牌档案")
    b.add_argument("domain")
    b.set_defaults(fn=cmd_brand)

    e = sub.add_parser("export-r8", help="输出 gshopping deep-verify FB 证据槽 JSON")
    e.add_argument("--keywords", required=True, help="逗号分隔")
    e.add_argument("--out", default=None)
    e.set_defaults(fn=cmd_export_r8)

    ps = sub.add_parser("profit-screen", help="FB 信号 + r9 downside 单位经济学预筛")
    ps.add_argument("keyword")
    ps.add_argument("--aov", type=float, required=True)
    ps.add_argument("--cogs-low", type=float, required=True)
    ps.add_argument("--cogs-high", type=float, required=True)
    ps.add_argument("--structure", default=None)
    ps.add_argument("--return-risk", default="med", choices=["low", "med", "high"])
    ps.add_argument("--cac-safety-ratio", type=float, default=0.70)
    ps.add_argument("--retail-source", required=True)
    ps.add_argument("--supplier-source", required=True)
    ps.add_argument("--checked-at", default=None)
    ps.add_argument("--json", action="store_true")
    ps.set_defaults(fn=cmd_profit_screen)

    sub.add_parser("doctor", help="环境体检").set_defaults(fn=cmd_doctor)
    sub.add_parser("selftest", help="离线自测").set_defaults(fn=cmd_selftest)

    args = ap.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())

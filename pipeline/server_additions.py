"""pipeline/server_additions.py — SelectPro API 增量端点.

插入位置:
1. 在 `pipeline/server.py` 创建 FastAPI app 后调用 `register_selectpro_routes(app)`。
2. 在 lifespan 启动时调用 `ensure_selectpro_schema()`。
3. 将 `V2_DASHBOARD_HTML` 替换为 `SELECTPRO_DASHBOARD_HTML`。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from core.logger import get_logger

from .ai_scorer import batch_score, ensure_schema as ensure_score_schema
from .paths import SCAN_FILE
from .persistence import DB_FILE, get_persistence
from .v3_selection_core import ensure_v3_schema, register_v3_selection_core

log = get_logger(__name__)


SELECTPRO_EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS product_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_domain TEXT,
    action TEXT,
    note TEXT,
    reviewed_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_product_reviews_brand_time
  ON product_reviews(brand_domain, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_product_reviews_action
  ON product_reviews(action);
"""


class ScoreBatchRequest(BaseModel):
    brand_domain: str = Field(..., min_length=2, max_length=255)
    min_score: int = Field(45, ge=0, le=100)

    @field_validator("brand_domain")
    @classmethod
    def _domain(cls, value: str) -> str:
        return value.strip().lower().replace("https://", "").replace("http://", "").strip("/")


class ReviewRequest(BaseModel):
    action: Literal["approve", "watch", "skip"]
    note: str = Field("", max_length=2000)


class ScrapeNowRequest(BaseModel):
    keywords: list[str] = Field(..., min_length=1, max_length=50)
    pages: int = Field(10, ge=1, le=30)
    parallel: int = Field(3, ge=1, le=8)
    country: str = Field("US", max_length=8)

    @field_validator("keywords")
    @classmethod
    def _keywords(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            kw = str(item or "").strip()
            key = kw.lower()
            if not kw or key in seen:
                continue
            if any(c in kw for c in "\n\r\t"):
                continue
            seen.add(key)
            out.append(kw[:120])
        if not out:
            raise ValueError("keywords empty")
        return out


class QueueAddRequest(BaseModel):
    keyword: str = Field(..., min_length=2, max_length=120)
    note: str = Field("manual-selectpro", max_length=200)


def ensure_selectpro_schema() -> None:
    """创建 SelectPro 需要的新表。可重复执行。"""
    ensure_score_schema(DB_FILE)
    ensure_v3_schema()
    conn = sqlite3.connect(str(DB_FILE))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.executescript(SELECTPRO_EXTRA_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _connect() -> sqlite3.Connection:
    ensure_selectpro_schema()
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def _json_list(value: Any) -> list[Any]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _row_to_pick(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    r = dict(row)
    return {
        "ad_id": r.get("ad_id"),
        "brand_domain": r.get("brand_domain") or r.get("domain") or "",
        "page_name": r.get("page_name") or "",
        "title": r.get("title") or "",
        "body": r.get("body") or "",
        "link_url": r.get("link_url") or "",
        "keyword": r.get("keyword") or "",
        "score": int(r.get("score") or 0),
        "grade": r.get("grade") or "SKIP",
        "active_days": int(r.get("active_days") or r.get("longevity") or 0),
        "target_countries": _json_list(r.get("target_locations")),
        "dimensions": {
            "longevity": int(r.get("longevity") or 0),
            "density": int(r.get("density") or 0),
            "market": int(r.get("market") or 0),
            "price_signal": int(r.get("price_signal") or 0),
            "creative": int(r.get("creative") or 0),
        },
        "signals": _json_list(r.get("signals")),
        "flags": _json_list(r.get("flags")),
        "ali_keywords": _json_list(r.get("ali_keywords")),
        "est_price_range": r.get("est_price_range") or "",
        "review_action": r.get("review_action") or "",
        "scored_at": r.get("scored_at") or "",
    }


def _fetch_brand_ads(conn: sqlite3.Connection, domain: str, limit: int = 5000) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """SELECT a.*, COALESCE(b.total_ads_across_scans, 0) AS brand_total_ads
               FROM ad_archive a
               LEFT JOIN brands b ON b.domain = a.domain
               WHERE a.domain = ?
               ORDER BY COALESCE(a.active_days, 0) DESC, a.scan_date DESC
               LIMIT ?""",
            (domain, limit),
        )
    ]


def _queue_items() -> list[dict[str, Any]]:
    from .scan_instructions import scan_queue_loader

    try:
        return scan_queue_loader()
    except Exception:
        return []


def _latest_review_sql() -> str:
    return """
      SELECT pr.brand_domain, pr.action
      FROM product_reviews pr
      JOIN (
        SELECT brand_domain, MAX(reviewed_at) AS reviewed_at
        FROM product_reviews
        GROUP BY brand_domain
      ) last
        ON last.brand_domain = pr.brand_domain
       AND last.reviewed_at = pr.reviewed_at
    """


def register_selectpro_routes(app: FastAPI) -> None:
    """把新增 /api 端点挂到现有 FastAPI app 上。"""
    register_v3_selection_core(app)

    @app.post("/api/score-batch")
    def api_score_batch(payload: ScoreBatchRequest):
        domain = payload.brand_domain
        with _connect() as conn:
            ads = _fetch_brand_ads(conn, domain)
        if not ads:
            raise HTTPException(status_code=404, detail=f"no ads found for brand: {domain}")
        scored = batch_score(ads, min_score=payload.min_score)
        return {
            "ok": True,
            "brand_domain": domain,
            "min_score": payload.min_score,
            "scored_count": len(scored),
            "items": scored,
        }

    @app.get("/api/picks")
    def api_picks(
        grade: str | None = Query(None, pattern="^(HERO|SOLID|WATCH|SKIP)$"),
        limit: int = Query(20, ge=1, le=500),
        sort: str = Query("score", pattern="^(score|days|scored_at)$"),
    ):
        order = {
            "score": "ps.score DESC, ps.scored_at DESC",
            "days": "COALESCE(a.active_days, ps.longevity) DESC, ps.score DESC",
            "scored_at": "ps.scored_at DESC, ps.score DESC",
        }[sort]
        where = "WHERE ps.grade = ?" if grade else ""
        params: list[Any] = [grade] if grade else []
        params.append(limit)
        with _connect() as conn:
            rows = conn.execute(
                f"""SELECT ps.*, a.page_name, a.title, a.body, a.link_url, a.keyword,
                          a.active_days, a.target_locations, rv.action AS review_action
                    FROM product_scores ps
                    LEFT JOIN ad_archive a ON a.ad_archive_id = ps.ad_id
                    LEFT JOIN ({_latest_review_sql()}) rv ON rv.brand_domain = ps.brand_domain
                    {where}
                    ORDER BY {order}
                    LIMIT ?""",
                params,
            ).fetchall()
        return {"ok": True, "items": [_row_to_pick(r) for r in rows]}

    @app.post("/api/review/{brand_domain}")
    def api_review(brand_domain: str, payload: ReviewRequest):
        domain = brand_domain.strip().lower()
        if not domain:
            raise HTTPException(status_code=400, detail="brand_domain required")
        with _connect() as conn:
            conn.execute(
                "INSERT INTO product_reviews(brand_domain, action, note) VALUES (?,?,?)",
                (domain, payload.action, payload.note.strip()),
            )
            conn.commit()
        return {"ok": True, "brand_domain": domain, "action": payload.action}

    @app.get("/api/profit")
    def api_profit(
        price: float = Query(..., gt=0),
        cogs: float = Query(..., ge=0),
        shipping: float = Query(0, ge=0),
    ):
        gross = price - cogs - shipping
        margin_pct = round(gross / price * 100, 1) if price else 0
        max_cpa = round(max(gross, 0), 2)
        breakeven_roas = round(price / gross, 2) if gross > 0 else None
        monthly_est = round(gross * 100, 2)
        return {
            "ok": True,
            "price": price,
            "cogs": cogs,
            "shipping": shipping,
            "gross_profit": round(gross, 2),
            "margin_pct": margin_pct,
            "max_cpa": max_cpa,
            "breakeven_roas": breakeven_roas,
            "monthly_est": monthly_est,
            "orders": 100,
        }

    @app.post("/api/scrape-now")
    async def api_scrape_now(payload: ScrapeNowRequest):
        async def _stream():
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            done = asyncio.Event()

            async def progress(event: dict[str, Any]) -> None:
                await queue.put(event)

            async def runner() -> None:
                try:
                    from .scraper_v9 import scrape_keywords_v9
                    summary = await scrape_keywords_v9(
                        payload.keywords,
                        pages=payload.pages,
                        parallel_kw=payload.parallel,
                        page_concurrency=min(3, payload.pages),
                        country=payload.country.upper() or "US",
                        headless=True,
                        progress=progress,
                    )
                    await queue.put({"type": "scrape_summary", **summary})
                    if summary.get("saved_keywords"):
                        from .raw_processor import process_raw_results
                        from .recommendations import rebuild_recommendations
                        from .ai_scorer import score_unscored_ads

                        processed = await asyncio.to_thread(process_raw_results, False, 2, False)
                        scored = await asyncio.to_thread(score_unscored_ads, 3000, 0)
                        rebuilt = await asyncio.to_thread(rebuild_recommendations)
                        await queue.put({
                            "type": "pipeline_done",
                            "processed_files": processed,
                            "auto_scored": scored,
                            "recommendations": rebuilt,
                        })
                except Exception as exc:
                    await queue.put({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
                finally:
                    done.set()

            asyncio.create_task(runner())
            while not done.is_set() or not queue.empty():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                yield json.dumps(event, ensure_ascii=False) + "\n"

        return StreamingResponse(_stream(), media_type="application/x-ndjson")

    @app.get("/api/stats")
    def api_stats():
        queue_items = _queue_items()
        with _connect() as conn:
            today_new = conn.execute(
                "SELECT COUNT(*) FROM ad_archive WHERE inserted_at >= date('now')"
            ).fetchone()[0]
            hero_count = conn.execute(
                "SELECT COUNT(*) FROM product_scores WHERE grade = 'HERO'"
            ).fetchone()[0]
            pending = conn.execute(
                """SELECT COUNT(*) FROM product_scores ps
                   WHERE NOT EXISTS (
                     SELECT 1 FROM product_reviews pr
                     WHERE pr.brand_domain = ps.brand_domain
                   )"""
            ).fetchone()[0]
        daemon = {}
        try:
            from .scraper import get_status
            daemon = get_status()
        except Exception:
            pass
        return {
            "ok": True,
            "today_new": today_new,
            "hero_count": hero_count,
            "pending_reviews": pending,
            "queue_remaining": len(queue_items),
            "daemon": daemon,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }

    @app.get("/api/brands")
    def api_brands(
        limit: int = Query(200, ge=1, le=2000),
        q: str | None = Query(None, max_length=120),
    ):
        where = ""
        params: list[Any] = []
        if q:
            where = "WHERE b.domain LIKE ? OR b.canonical_name LIKE ?"
            like = f"%{q.strip()}%"
            params.extend([like, like])
        params.append(limit)
        with _connect() as conn:
            rows = conn.execute(
                f"""SELECT b.domain, b.canonical_name, b.appearance_count,
                          b.total_ads_across_scans, b.orbit_score,
                          COALESCE(MAX(a.active_days), 0) AS max_active_days,
                          COALESCE(MAX(ps.score), 0) AS ai_score,
                          COALESCE(MAX(ps.grade), '') AS grade,
                          rv.action AS status
                    FROM brands b
                    LEFT JOIN ad_archive a ON a.domain = b.domain
                    LEFT JOIN product_scores ps ON ps.brand_domain = b.domain
                    LEFT JOIN ({_latest_review_sql()}) rv ON rv.brand_domain = b.domain
                    {where}
                    GROUP BY b.domain
                    ORDER BY ai_score DESC, b.orbit_score DESC, b.total_ads_across_scans DESC
                    LIMIT ?""",
                params,
            ).fetchall()
        return {"ok": True, "items": [dict(r) for r in rows]}

    @app.get("/api/orbit-brands")
    def api_orbit_brands(
        min_appearance: int = Query(3, ge=1),
        limit: int = Query(50, ge=1, le=500),
    ):
        return {
            "ok": True,
            "items": list(get_persistence().list_orbit_brands(min_appearance, limit)),
        }

    @app.get("/api/scan-queue")
    def api_scan_queue():
        return {"ok": True, "items": _queue_items()}

    @app.post("/api/scan-queue")
    def api_scan_queue_add(payload: QueueAddRequest):
        keyword = payload.keyword.strip()
        if not keyword:
            raise HTTPException(status_code=400, detail="keyword required")
        SCAN_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = set()
        if SCAN_FILE.exists():
            for line in SCAN_FILE.read_text(encoding="utf-8").splitlines():
                raw = line.strip().lstrip("#").split("|")[0].strip().lower()
                if raw:
                    existing.add(raw)
        if keyword.lower() not in existing:
            with SCAN_FILE.open("a", encoding="utf-8") as f:
                f.write(f"{keyword} | {payload.note.strip() or 'manual-selectpro'}\n")
        return {"ok": True, "keyword": keyword}

    @app.delete("/api/scan-queue/{keyword}")
    def api_scan_queue_delete(keyword: str):
        target = keyword.strip().lower()
        if not SCAN_FILE.exists():
            return {"ok": True, "removed": 0}
        lines = SCAN_FILE.read_text(encoding="utf-8").splitlines()
        kept: list[str] = []
        removed = 0
        for line in lines:
            raw = line.strip().lstrip("#").split("|")[0].strip().lower()
            if raw == target:
                removed += 1
                continue
            kept.append(line)
        SCAN_FILE.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        return {"ok": True, "removed": removed}


SELECTPRO_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FB SelectPro</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
:root{--bg:#0F172A;--surface:#1E293B;--surface2:#111827;--line:#334155;--text:#E5E7EB;--muted:#94A3B8;--accent:#6366F1;--ok:#22C55E;--warn:#F59E0B;--bad:#EF4444;--watch:#38BDF8;--shadow:0 14px 34px rgba(0,0,0,.24)}
:root.light{--bg:#F8FAFC;--surface:#FFFFFF;--surface2:#EEF2FF;--line:#CBD5E1;--text:#0F172A;--muted:#64748B;--shadow:0 12px 28px rgba(15,23,42,.10)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;font-size:14px;letter-spacing:0}button,input,select,textarea{font:inherit}button{border:0;cursor:pointer}
.topbar{position:sticky;top:0;z-index:20;height:64px;background:rgba(15,23,42,.88);backdrop-filter:blur(14px);border-bottom:1px solid var(--line);display:flex;align-items:center;gap:18px;padding:0 22px}.light .topbar{background:rgba(248,250,252,.88)}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;font-size:18px}.logo{width:31px;height:31px}.kpis{display:flex;gap:8px;flex-wrap:wrap}.pill{background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:6px 10px;color:var(--muted);white-space:nowrap}.pill b{color:var(--text);margin-left:5px}.spacer{flex:1}.theme{background:var(--surface);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 11px}
.wrap{padding:18px 22px 28px}.tabs{display:flex;gap:8px;border-bottom:1px solid var(--line);margin-bottom:16px;overflow:auto}.tab{background:transparent;color:var(--muted);padding:12px 14px;border-bottom:2px solid transparent;white-space:nowrap}.tab.active{color:var(--text);border-color:var(--accent);font-weight:700}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}.control{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:8px 10px;color:var(--text)}.control input,.control select{background:transparent;color:var(--text);border:0;outline:0;min-width:70px}.btn{background:var(--surface);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px 11px}.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.btn.good{background:rgba(34,197,94,.18);border-color:rgba(34,197,94,.45);color:#86EFAC}.btn.bad{background:rgba(239,68,68,.16);border-color:rgba(239,68,68,.45);color:#FCA5A5}.btn.small{padding:6px 8px;font-size:12px}
.view{display:none}.view.active{display:block}.kanban{display:grid;grid-template-columns:repeat(4,minmax(240px,1fr));gap:12px}.lane{min-width:0}.lane h2{font-size:13px;margin:0 0 8px;color:var(--muted);display:flex;justify-content:space-between}.cards{display:flex;flex-direction:column;gap:10px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:12px;transition:transform .16s ease,box-shadow .16s ease,border-color .16s ease}.card:hover,.card.selected{transform:translateY(-2px);box-shadow:var(--shadow);border-color:var(--accent)}.card.fade{opacity:.25;transform:translateX(18px)}
.cardtop{display:flex;gap:10px;align-items:center}.domain{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.meta{color:var(--muted);font-size:12px;margin-top:2px}.score-ring{width:54px;height:54px;flex:0 0 54px;transform:rotate(-90deg)}.score-ring circle{fill:none;stroke-width:8}.score-ring .bg{stroke:rgba(148,163,184,.22)}.score-ring .fg{stroke:var(--accent);stroke-linecap:round;pathLength:100;stroke-dasharray:0 100;animation:ring .6s ease-out forwards}@keyframes ring{to{stroke-dasharray:var(--score) 100}}.score-text{position:absolute;margin-left:13px;margin-top:-39px;font-size:13px;font-weight:800;width:28px;text-align:center}
.badge{display:inline-flex;border-radius:999px;padding:3px 7px;font-size:11px;font-weight:800}.HERO{background:rgba(99,102,241,.22);color:#C7D2FE}.SOLID{background:rgba(34,197,94,.18);color:#86EFAC}.WATCH{background:rgba(245,158,11,.18);color:#FCD34D}.SKIP{background:rgba(239,68,68,.18);color:#FCA5A5}.signals{display:flex;gap:5px;flex-wrap:wrap;margin:10px 0}.tag{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 6px}.actions{display:flex;gap:6px;margin-top:10px}
.tablewrap{background:var(--surface);border:1px solid var(--line);border-radius:8px;overflow:auto}table{width:100%;border-collapse:collapse}th,td{padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{color:var(--muted);font-size:12px;cursor:pointer}tr:hover{background:rgba(99,102,241,.08)}.detail-row td{white-space:normal;color:var(--muted);background:var(--surface2)}
.profit-grid{display:grid;grid-template-columns:minmax(280px,420px) 1fr;gap:16px}.panel{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:16px}.field{display:grid;grid-template-columns:80px 1fr 86px;gap:10px;align-items:center;margin:14px 0}.field input[type=range]{width:100%}.field input[type=number],textarea{background:var(--surface2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px;width:100%}.metrics{display:grid;grid-template-columns:repeat(2,minmax(160px,1fr));gap:10px}.metric{background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:14px}.metric b{display:block;font-size:26px;margin-top:6px}.queue{display:grid;grid-template-columns:minmax(260px,360px) 1fr;gap:16px}.queue-list{max-height:420px;overflow:auto;display:flex;flex-direction:column;gap:7px}.queue-item{display:flex;justify-content:space-between;gap:8px;background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:8px}.log{height:420px;overflow:auto;background:#020617;color:#D1FAE5;border:1px solid var(--line);border-radius:8px;padding:12px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;white-space:pre-wrap}.status-dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 4px rgba(34,197,94,.14);margin-right:7px}
@media(max-width:1100px){.kanban{grid-template-columns:repeat(2,1fr)}.profit-grid,.queue{grid-template-columns:1fr}}@media(max-width:640px){.topbar{height:auto;align-items:flex-start;flex-wrap:wrap;padding:12px}.kanban{grid-template-columns:1fr}.wrap{padding:14px}.metrics{grid-template-columns:1fr}.field{grid-template-columns:1fr}}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand"><svg class="logo" viewBox="0 0 40 40" aria-hidden="true"><defs><linearGradient id="g" x1="0" x2="1"><stop stop-color="#6366F1"/><stop offset="1" stop-color="#22C55E"/></linearGradient></defs><path d="M20 3l14.7 8.5v17L20 37 5.3 28.5v-17L20 3z" fill="url(#g)"/><path d="M13 22.5l5 5 10-15" fill="none" stroke="white" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"/></svg>FB SelectPro</div>
  <div class="kpis">
    <span class="pill">今日新增 <b id="k-today">0</b></span>
    <span class="pill">HERO <b id="k-hero">0</b></span>
    <span class="pill">待审核 <b id="k-pending">0</b></span>
    <span class="pill">队列剩余 <b id="k-queue">0</b></span>
  </div>
  <div class="spacer"></div>
  <button class="theme" id="themeBtn">暗/亮</button>
</header>
<main class="wrap">
  <nav class="tabs">
    <button class="tab active" data-view="kanban">选品看板</button>
    <button class="tab" data-view="brands">品牌情报</button>
    <button class="tab" data-view="profit">利润计算器</button>
    <button class="tab" data-view="scanner">扫描控制台</button>
  </nav>

  <section id="kanban" class="view active">
    <div class="toolbar">
      <label class="control">国家 <select id="f-country"><option value="">全部</option><option>US</option><option>UK</option><option>CA</option><option>AU</option><option>DE</option><option>FR</option></select></label>
      <label class="control">最低分 <input id="f-score" type="number" value="45" min="0" max="100"></label>
      <label class="control">投放天数 ≥ <input id="f-days" type="number" value="0" min="0"></label>
      <button class="btn primary" id="refreshPicks">刷新</button>
    </div>
    <div class="kanban" id="kanbanBoard"></div>
  </section>

  <section id="brands" class="view">
    <div class="toolbar">
      <input class="control" id="brandSearch" placeholder="搜索域名 / 品牌">
      <button class="btn primary" id="refreshBrands">刷新</button>
    </div>
    <div class="tablewrap"><table id="brandTable"></table></div>
  </section>

  <section id="profit" class="view">
    <div class="profit-grid">
      <div class="panel">
        <div class="field"><label>售价</label><input id="priceR" type="range" min="9" max="299" value="49.99" step="1"><input id="priceN" type="number" value="49.99" step="0.01"></div>
        <div class="field"><label>货源价</label><input id="cogsR" type="range" min="1" max="120" value="8.5" step="0.5"><input id="cogsN" type="number" value="8.5" step="0.01"></div>
        <div class="field"><label>运费</label><input id="shipR" type="range" min="0" max="50" value="3.2" step="0.2"><input id="shipN" type="number" value="3.2" step="0.01"></div>
        <input class="control" id="sourcingKw" placeholder="Ali / 1688 搜索词">
        <div class="actions"><button class="btn primary" id="aliBtn">AliExpress 快搜</button><button class="btn" id="cnBtn">1688 快搜</button></div>
      </div>
      <div class="metrics" id="profitMetrics"></div>
    </div>
  </section>

  <section id="scanner" class="view">
    <div class="queue">
      <div class="panel">
        <p><span class="status-dot"></span><span id="daemonStatus">Daemon 状态读取中</span></p>
        <div class="toolbar"><input class="control" id="newKw" placeholder="添加关键词"><button class="btn" id="addKw">添加</button></div>
        <div class="queue-list" id="queueList"></div>
      </div>
      <div class="panel">
        <textarea id="scrapeKw" rows="4" placeholder="每行一个关键词"></textarea>
        <div class="toolbar">
          <label class="control">页数 <input id="scrapePages" type="number" value="10" min="1" max="30"></label>
          <label class="control">并发 <input id="scrapeParallel" type="number" value="3" min="1" max="8"></label>
          <button class="btn primary" id="scrapeNow">立即抓取</button>
        </div>
        <div class="log" id="scrapeLog"></div>
      </div>
    </div>
  </section>
</main>

<script>
const API='http://127.0.0.1:8000/api';
const state={picks:[],brands:[],queue:[],selected:null,brandSort:'ai_score',brandDir:-1};
const grades=['HERO','SOLID','WATCH','SKIP'];
const gradeTitle={HERO:'HERO🔥',SOLID:'SOLID✅',WATCH:'WATCH⚠️',SKIP:'SKIP❌'};
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
async function j(url,opt){const r=await fetch(url,opt);if(!r.ok)throw new Error(await r.text());return r.json()}
function setTab(id){document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.view===id));document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id===id))}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>setTab(b.dataset.view));
$('themeBtn').onclick=()=>document.documentElement.classList.toggle('light');

async function loadStats(){try{const d=await j(API+'/stats');$('k-today').textContent=d.today_new||0;$('k-hero').textContent=d.hero_count||0;$('k-pending').textContent=d.pending_reviews||0;$('k-queue').textContent=d.queue_remaining||0;const dm=d.daemon||{};$('daemonStatus').textContent=`${dm.in_cooldown?'冷却中':'运行正常'} · 上次 ${dm.last_scrape_at||'—'} · 下次 ${dm.next_cycle_eta_sec??'—'}s`;}catch(e){}}
async function loadPicks(){const d=await j(API+'/picks?limit=240&sort=score');state.picks=d.items||[];renderKanban()}
function countriesOf(p){const raw=p.target_countries||[];return raw.map(x=>typeof x==='string'?x:(x.country||x.code||x.name||'')).join(' ').toUpperCase()}
function filteredPicks(){const c=$('f-country').value;const min=Number($('f-score').value||0);const days=Number($('f-days').value||0);return state.picks.filter(p=>(p.score||0)>=min&&(p.active_days||0)>=days&&(!c||countriesOf(p).includes(c)))}
function ring(score){return `<div style="position:relative"><svg class="score-ring" viewBox="0 0 54 54"><circle class="bg" cx="27" cy="27" r="21"></circle><circle class="fg" style="--score:${score}" cx="27" cy="27" r="21" pathLength="100"></circle></svg><div class="score-text">${score}</div></div>`}
function card(p){const sig=(p.signals||[]).slice(0,3).map(s=>`<span class="tag">${esc(s)}</span>`).join('');const kw=(p.ali_keywords||[])[0]||p.keyword||'';return `<article class="card ${state.selected===p.ad_id?'selected':''}" tabindex="0" data-id="${esc(p.ad_id)}" onclick="selectCard('${esc(p.ad_id)}')"><div class="cardtop">${ring(p.score||0)}<div style="min-width:0"><div class="domain">${esc(p.brand_domain||'unknown')}</div><div class="meta">${p.active_days||0}d · ${esc(countriesOf(p)||'market ?')} · <span class="badge ${p.grade}">${p.grade}</span></div></div></div><div class="signals">${sig}</div><div class="meta">${esc(p.title||kw||'')}</div><div class="actions"><button class="btn small" onclick="event.stopPropagation();showDetails('${esc(p.ad_id)}')">详情</button><button class="btn good small" onclick="event.stopPropagation();review('${esc(p.brand_domain)}','approve','${esc(p.ad_id)}')">通过</button><button class="btn bad small" onclick="event.stopPropagation();review('${esc(p.brand_domain)}','skip','${esc(p.ad_id)}')">跳过</button></div></article>`}
function renderKanban(){const rows=filteredPicks();$('kanbanBoard').innerHTML=grades.map(g=>{const items=rows.filter(p=>p.grade===g);return `<div class="lane"><h2><span>${gradeTitle[g]}</span><span>${items.length}</span></h2><div class="cards">${items.map(card).join('')||'<div class="meta">暂无</div>'}</div></div>`}).join('')}
window.selectCard=id=>{state.selected=id;const p=state.picks.find(x=>x.ad_id===id);if(p&&p.ali_keywords?.[0])$('sourcingKw').value=p.ali_keywords[0];renderKanban()}
window.showDetails=id=>{const p=state.picks.find(x=>x.ad_id===id);if(!p)return;alert(`${p.brand_domain}\nScore ${p.score} ${p.grade}\nSignals: ${(p.signals||[]).join(', ')}\nFlags: ${(p.flags||[]).join(', ')}\nAli: ${(p.ali_keywords||[]).join(' | ')}`)}
async function review(domain,action,id){await j(API+'/review/'+encodeURIComponent(domain),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,note:'dashboard'})});const p=state.picks.find(x=>x.ad_id===id);if(p){p.grade=action==='skip'?'SKIP':action==='watch'?'WATCH':'SOLID'};renderKanban();loadStats()}
document.addEventListener('keydown',e=>{if(!state.selected)return;const p=state.picks.find(x=>x.ad_id===state.selected);if(!p)return;if(e.key.toLowerCase()==='a')review(p.brand_domain,'approve',p.ad_id);if(e.key.toLowerCase()==='s')review(p.brand_domain,'skip',p.ad_id);if(e.key.toLowerCase()==='w')review(p.brand_domain,'watch',p.ad_id)});
$('refreshPicks').onclick=loadPicks;['f-country','f-score','f-days'].forEach(id=>$(id).oninput=renderKanban);

async function loadBrands(){const q=$('brandSearch').value.trim();const d=await j(API+'/brands?limit=300'+(q?'&q='+encodeURIComponent(q):''));state.brands=d.items||[];renderBrands()}
function renderBrands(){const rows=[...state.brands].sort((a,b)=>((a[state.brandSort]||0)>(b[state.brandSort]||0)?1:-1)*state.brandDir);$('brandTable').innerHTML='<thead><tr>'+[['domain','域名'],['total_ads_across_scans','广告数'],['max_active_days','最长投放'],['orbit_score','目标市场'],['ai_score','AI分'],['status','状态'],['op','操作']].map(([k,t])=>`<th onclick="sortBrands('${k}')">${t}</th>`).join('')+'</tr></thead><tbody>'+rows.map(r=>`<tr onclick="toggleBrand(this,'${esc(r.domain)}')"><td>${esc(r.domain)}</td><td>${r.total_ads_across_scans||0}</td><td>${r.max_active_days||0}d</td><td>${r.orbit_score||0}</td><td><b>${r.ai_score||0}</b></td><td>${esc(r.status||'pending')}</td><td><button class="btn small" onclick="event.stopPropagation();scoreBrand('${esc(r.domain)}')">评分</button></td></tr>`).join('')+'</tbody>'}
window.sortBrands=k=>{state.brandSort=k;state.brandDir*=-1;renderBrands()}
window.toggleBrand=(tr,domain)=>{if(tr.nextSibling?.classList?.contains('detail-row')){tr.nextSibling.remove();return}const row=document.createElement('tr');row.className='detail-row';row.innerHTML=`<td colspan="7">域名 ${esc(domain)} · 点击“评分”会读取该品牌 ad_archive 并写入 product_scores。</td>`;tr.after(row)}
window.scoreBrand=async domain=>{await j(API+'/score-batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({brand_domain:domain,min_score:45})});await loadPicks();await loadBrands()}
$('refreshBrands').onclick=loadBrands;$('brandSearch').oninput=()=>setTimeout(loadBrands,120);

function syncPair(r,n){$(r).oninput=()=>{$(n).value=$(r).value;calcProfit()};$(n).oninput=()=>{$(r).value=$(n).value;calcProfit()}}
syncPair('priceR','priceN');syncPair('cogsR','cogsN');syncPair('shipR','shipN');
async function calcProfit(){const price=+$('priceN').value||0,cogs=+$('cogsN').value||0,shipping=+$('shipN').value||0;try{const d=await j(`${API}/profit?price=${price}&cogs=${cogs}&shipping=${shipping}`);$('profitMetrics').innerHTML=[['毛利率',d.margin_pct+'%'],['Max CPA','$'+d.max_cpa],['盈亏ROAS',d.breakeven_roas||'—'],['月利润估算','$'+d.monthly_est]].map(([a,b])=>`<div class="metric">${a}<b>${b}</b></div>`).join('')}catch(e){}}
function sourceKw(){return $('sourcingKw').value.trim()||state.picks.find(p=>p.ad_id===state.selected)?.ali_keywords?.[0]||'winning product'}
$('aliBtn').onclick=()=>open('https://www.aliexpress.com/wholesale?SearchText='+encodeURIComponent(sourceKw()),'_blank');
$('cnBtn').onclick=()=>open('https://s.1688.com/selloffer/offer_search.htm?keywords='+encodeURIComponent(sourceKw()),'_blank');

async function loadQueue(){const d=await j(API+'/scan-queue');state.queue=d.items||[];$('queueList').innerHTML=state.queue.slice(0,120).map(it=>`<div class="queue-item"><span>${esc(it.keyword||it)}</span><button class="btn small" onclick="delKw('${esc(it.keyword||it)}')">删除</button></div>`).join('')||'<div class="meta">队列为空</div>'}
$('addKw').onclick=async()=>{const kw=$('newKw').value.trim();if(!kw)return;await j(API+'/scan-queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keyword:kw})});$('newKw').value='';loadQueue();loadStats()}
window.delKw=async kw=>{await fetch(API+'/scan-queue/'+encodeURIComponent(kw),{method:'DELETE'});loadQueue();loadStats()}
$('scrapeNow').onclick=async()=>{const kws=$('scrapeKw').value.split(/\n|,/).map(s=>s.trim()).filter(Boolean);if(!kws.length)return;const log=$('scrapeLog');log.textContent='';const res=await fetch(API+'/scrape-now',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keywords:kws,pages:+$('scrapePages').value||10,parallel:+$('scrapeParallel').value||3})});const reader=res.body.getReader();const dec=new TextDecoder();let buf='';while(true){const {done,value}=await reader.read();if(done)break;buf+=dec.decode(value,{stream:true});let lines=buf.split('\n');buf=lines.pop();for(const line of lines){if(!line.trim())continue;log.textContent+=line+'\n';log.scrollTop=log.scrollHeight}}loadStats();loadPicks();loadQueue()}

async function boot(){await loadStats();await Promise.allSettled([loadPicks(),loadBrands(),loadQueue()]);calcProfit()}
boot();setInterval(loadStats,30000);
</script>
</body>
</html>"""

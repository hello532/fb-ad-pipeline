"""pipeline/server.py — FastAPI 接口. 仅在 --serve 时被 import (避免强依赖)."""
from __future__ import annotations

import json
import os
import re
import errno
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse, StreamingResponse

from core.logger import get_logger
from datetime import datetime, timedelta

from .cache import ttl_cache


V2_DASHBOARD_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>FB 选品 v2 · 2026 顶级信号 + 创意洞见</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root {
    --bg:#0f1720; --panel:#1a2330; --panel2:#141d28; --text:#e5eaf0; --muted:#7a8696;
    --accent:#5b9dff; --ok:#10b981; --warn:#f59e0b; --bad:#ef4444; --s:#a78bfa;
    --border:#2a3546; --grid:#293449; --pink:#ec4899; --teal:#14b8a6;
  }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,system-ui,sans-serif; background:var(--bg); color:var(--text); margin:0; padding:16px; }
  h1 { font-size:20px; margin:0 0 4px; display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  h2 { font-size:14px; margin:18px 0 8px; color:var(--muted); font-weight:600; letter-spacing:.5px; text-transform:uppercase; }
  .back { color:var(--accent); text-decoration:none; font-size:13px; }
  .badge { background:linear-gradient(135deg,#8b5cf6,#5b9dff); color:#fff; font-size:11px; padding:3px 8px; border-radius:6px; letter-spacing:.5px; font-weight:600; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:14px; line-height:1.55; }
  .stats { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:12px; }
  .stat { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:7px 11px; font-size:12px; }
  .stat b { color:var(--accent); font-size:14px; margin-right:4px; }
  .stat.warn b { color:var(--warn); }
  .stat.ok b { color:var(--ok); }
  .tabs { display:flex; gap:4px; margin-bottom:10px; border-bottom:1px solid var(--border); flex-wrap:wrap; }
  .tab { background:transparent; color:var(--muted); border:none; padding:8px 14px; cursor:pointer; font:inherit; border-bottom:2px solid transparent; font-size:13px; }
  .tab.active { color:var(--accent); border-bottom-color:var(--accent); font-weight:600; }
  .tab.insight.active { color:var(--s); border-bottom-color:var(--s); }
  .ctrl { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:10px; margin-bottom:12px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; font-size:13px; }
  .ctrl label { display:flex; gap:4px; align-items:center; }
  .ctrl input, .ctrl select { background:#0b131c; border:1px solid var(--border); color:var(--text); padding:4px 8px; border-radius:5px; font:inherit; }
  button { background:var(--accent); color:#fff; border:none; padding:6px 14px; border-radius:5px; cursor:pointer; font:inherit; font-weight:500; }
  button:hover { opacity:.9; }
  button.secondary { background:#334155; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th, td { padding:6px 7px; text-align:left; border-bottom:1px solid var(--border); vertical-align:top; }
  th { background:var(--panel); font-size:11px; color:var(--muted); font-weight:600; position:sticky; top:0; }
  tr:hover { background:rgba(91,157,255,.06); }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .tier { display:inline-block; padding:2px 8px; border-radius:4px; font-weight:700; font-size:11px; }
  .tier-S { background:#8b5cf6; color:#fff; }
  .tier-A { background:var(--ok); color:#fff; }
  .tier-B { background:var(--accent); color:#fff; }
  .tier-C { background:#475569; color:#cbd5e1; }
  .tier-D, .tier-F { background:var(--bad); color:#fff; }
  .tag { display:inline-block; background:#1e293b; color:#94a3b8; font-size:10px; padding:1px 6px; border-radius:3px; margin:1px 2px; white-space:nowrap; }
  .tag.lp { background:#14532d; color:#86efac; }
  .tag.bad { background:#450a0a; color:#fca5a5; }
  .tag.hot { background:#7c2d12; color:#fdba74; }
  .tag.reddit { background:#3b1570; color:#d8b4fe; }
  .tag.price { background:#1e3a8a; color:#93c5fd; }
  .brand { font-weight:600; max-width:200px; }
  .brand a { color:inherit; text-decoration:none; }
  .brand a:hover { color:var(--accent); }
  .loading { color:var(--muted); padding:30px; text-align:center; }
  .err { color:var(--bad); padding:20px; }
  .hist { display:flex; align-items:flex-end; gap:2px; height:40px; padding:6px; background:var(--panel); border:1px solid var(--border); border-radius:6px; margin-bottom:10px; }
  .hist > div { flex:1; background:var(--accent); border-radius:2px 2px 0 0; position:relative; min-height:2px; }
  .hist > div span { position:absolute; bottom:-16px; left:50%; transform:translateX(-50%); font-size:10px; color:var(--muted); }
  .hist > div.S { background:#8b5cf6; } .hist > div.A { background:var(--ok); }
  .hist > div.B { background:var(--accent); } .hist > div.C { background:#475569; }
  .hist > div.D, .hist > div.F { background:var(--bad); }
  .scraper { font-size:11px; color:var(--muted); margin-left:auto; }
  .scraper b { color:var(--text); }

  /* v3 insight panels */
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .grid3 { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
  @media (max-width:900px){ .grid2,.grid3 { grid-template-columns:1fr; } }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:14px; }
  .card h3 { margin:0 0 8px; font-size:13px; color:var(--muted); font-weight:600; letter-spacing:.5px; text-transform:uppercase; }
  .card .big { font-size:24px; font-weight:700; color:var(--accent); }
  .card .sub2 { color:var(--muted); font-size:11px; }
  .bars { display:flex; flex-direction:column; gap:4px; font-size:12px; }
  .bars .row { display:grid; grid-template-columns:120px 1fr 50px; gap:8px; align-items:center; }
  .bars .row .label { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .bars .row .bar { background:var(--grid); height:14px; border-radius:3px; position:relative; overflow:hidden; }
  .bars .row .bar > i { display:block; height:100%; background:linear-gradient(90deg,var(--accent),var(--s)); border-radius:3px; }
  .bars .row .num { text-align:right; font-variant-numeric:tabular-nums; color:var(--muted); }
  .bars-warm .row .bar > i { background:linear-gradient(90deg,var(--warn),var(--pink)); }
  .bars-cool .row .bar > i { background:linear-gradient(90deg,var(--teal),var(--ok)); }
  .heatmap { display:grid; grid-template-columns:80px repeat(3,1fr); gap:2px; font-size:12px; }
  .heatmap .hd { color:var(--muted); padding:6px; text-align:center; font-weight:600; }
  .heatmap .age { color:var(--muted); padding:6px; }
  .heatmap .cell { padding:8px; text-align:center; border-radius:3px; font-variant-numeric:tabular-nums; }
  .pie-legend { display:flex; flex-wrap:wrap; gap:6px; font-size:11px; color:var(--muted); }
  .pie-legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:middle; }
</style>
</head>
<body>
  <h1>🚀 FB 选品 v2 <span class="badge">2026 Q2 · 11 信号 + 6 创意洞见</span>
      <a class="back" href="/">← 主控台</a>
      <span class="scraper" id="scraper-status">—</span>
  </h1>
  <div class="sub">velocity · diversity · age-curve · cluster · LP 指纹 · geo · fatigue · engagement · reddit · price-band · dropship 惩罚<br>
       <b style="color:var(--s)">v9.1 创意洞见:</b> 创意构成 · 地理分布 · 受众画像 · 投放平台 · 创意疲劳 · 文案模式 (来自 GraphQL 嗅探的 ad-level 富数据)</div>

  <div class="stats" id="stats"></div>
  <div class="hist" id="tier-hist" title="tier 分布"></div>

  <div class="tabs">
    <button class="tab active" data-mode="top" onclick="switchTab('top')">🏆 顶级候选</button>
    <button class="tab" data-mode="hidden" onclick="switchTab('hidden')">💎 隐藏宝石</button>
    <button class="tab" data-mode="emerging" onclick="switchTab('emerging')">🌱 新兴 niche</button>
    <button class="tab" data-mode="warnings" onclick="switchTab('warnings')">⚠️ Dropship</button>
    <button class="tab insight" data-mode="creative" onclick="switchTab('creative')">🎬 创意构成</button>
    <button class="tab insight" data-mode="geo" onclick="switchTab('geo')">📍 地理分布</button>
    <button class="tab insight" data-mode="demo" onclick="switchTab('demo')">👥 受众画像</button>
    <button class="tab insight" data-mode="platforms" onclick="switchTab('platforms')">📱 投放平台</button>
    <button class="tab insight" data-mode="fatigue" onclick="switchTab('fatigue')">🔄 创意疲劳</button>
    <button class="tab insight" data-mode="copy" onclick="switchTab('copy')">📝 文案模式</button>
    <button class="tab insight" data-mode="latest" onclick="switchTab('latest')" style="color:#fdba74;border-bottom-color:transparent">📡 最新数据</button>
    <button class="tab insight" data-mode="veteran" onclick="switchTab('veteran')" style="color:#fbbf24;border-bottom-color:transparent">🎖️ 老兵直觉</button>
    <button class="tab insight" data-mode="saturation" onclick="switchTab('saturation')" style="color:#34d399;border-bottom-color:transparent">🌡️ 渗透率</button>
    <button class="tab insight" data-mode="follow" onclick="switchTab('follow')" style="color:#f472b6;border-bottom-color:transparent">🎯 跟买卡</button>
  </div>

  <div class="ctrl" id="ctrl-rerank">
    <label>最低 base <input id="f-min" type="number" value="40" min="0" max="120" style="width:55px"></label>
    <label>数量 <input id="f-limit" type="number" value="30" min="1" max="500" style="width:55px"></label>
    <label>Tier ≥ <select id="f-tier"><option value="F">全部</option><option value="C">C</option><option value="B" selected>B</option><option value="A">A</option><option value="S">S</option></select></label>
    <label><input id="f-shopify" type="checkbox" checked> 仅 Shopify</label>
    <label><input id="f-lponly" type="checkbox"> 有 LP 指纹</label>
    <label>价格区间
      <select id="f-price">
        <option value="">全部</option><option value="low">&lt;$20</option>
        <option value="sweet1">$20-40</option><option value="sweet2">$40-90</option>
        <option value="aov">$90-150</option><option value="premium">$150-300</option>
        <option value="luxury">$300+</option>
      </select>
    </label>
    <button onclick="load()">🔄 刷新</button>
    <button class="secondary" onclick="probe()">🔍 探 50 LP</button>
    <button class="secondary" onclick="snap()">📸 快照</button>
    <span id="msg" style="color:var(--muted); font-size:12px"></span>
  </div>

  <div class="ctrl" id="ctrl-insight" style="display:none">
    <label>窗口
      <select id="i-days">
        <option value="7">7 天</option>
        <option value="14" selected>14 天</option>
        <option value="30">30 天</option>
        <option value="60">60 天</option>
        <option value="90">90 天</option>
      </select>
    </label>
    <label>品牌域名 <input id="i-domain" type="text" placeholder="(可选)" style="width:160px"></label>
    <label>关键词 <input id="i-keyword" type="text" placeholder="(可选)" style="width:160px"></label>
    <button onclick="load()">🔄 刷新</button>
    <span id="i-msg" style="color:var(--muted); font-size:12px"></span>
  </div>

  <div class="ctrl" id="ctrl-latest" style="display:none">
    <label>条数 <input id="L-limit" type="number" value="50" min="10" max="500" style="width:60px"></label>
    <label>时间窗口
      <select id="L-hours">
        <option value="0" selected>不限</option>
        <option value="1">最近 1h</option>
        <option value="6">最近 6h</option>
        <option value="24">最近 24h</option>
        <option value="72">最近 3d</option>
        <option value="168">最近 7d</option>
      </select>
    </label>
    <label>关键词 <input id="L-keyword" type="text" placeholder="(可选)" style="width:140px"></label>
    <label>域名 <input id="L-domain" type="text" placeholder="(可选)" style="width:140px"></label>
    <label><input id="L-graphql" type="checkbox"> 仅 GraphQL 富数据</label>
    <button onclick="load()">🔄 刷新</button>
    <button class="secondary" onclick="scrapeNow()" style="background:#dc2626" title="向 daemon 提交一次性抓取任务">🔥 立即抓取关键词</button>
    <span id="L-msg" style="color:var(--muted); font-size:12px"></span>
  </div>

  <div class="ctrl" id="ctrl-veteran" style="display:none">
    <label>窗口
      <select id="V-days">
        <option value="14">14 天</option>
        <option value="30" selected>30 天</option>
        <option value="60">60 天</option>
        <option value="90">90 天</option>
      </select>
    </label>
    <label>显示数量 <input id="V-limit" type="number" value="40" min="5" max="200" style="width:60px"></label>
    <label>verdict
      <select id="V-verdict">
        <option value="">全部</option>
        <option value="GO_FOLLOW">🟢 GO_FOLLOW</option>
        <option value="OBSERVE">🟡 OBSERVE</option>
        <option value="WATCH">⚪ WATCH</option>
        <option value="AVOID">🔴 AVOID</option>
      </select>
    </label>
    <label><input id="V-active" type="checkbox" checked> 仅当前 active</label>
    <button onclick="load()">🔄 刷新</button>
    <span style="color:var(--muted);font-size:11px">点击品牌 → 跳转跟买卡</span>
  </div>

  <div class="ctrl" id="ctrl-saturation" style="display:none">
    <label>关键词 <input id="S-keyword" type="text" placeholder="例: hair removal" style="width:200px"></label>
    <label>窗口
      <select id="S-days">
        <option value="14">14 天</option>
        <option value="30" selected>30 天</option>
        <option value="60">60 天</option>
        <option value="90">90 天</option>
      </select>
    </label>
    <button onclick="load()">🔍 分析</button>
    <span style="color:var(--muted);font-size:11px">输入关键词 → 看 niche 是否还能进</span>
  </div>

  <div class="ctrl" id="ctrl-follow" style="display:none">
    <label>品牌域名 <input id="F-domain" type="text" placeholder="例: brand.com" style="width:200px"></label>
    <label>窗口
      <select id="F-days">
        <option value="30">30 天</option>
        <option value="60" selected>60 天</option>
        <option value="90">90 天</option>
      </select>
    </label>
    <button onclick="load()">🎯 出卡</button>
    <span style="color:var(--muted);font-size:11px">老兵给徒弟看的那张纸</span>
  </div>

  <div id="content" class="loading">加载中…</div>

<script>
const TIER_RANK = {F:0, D:1, C:2, B:3, A:4, S:5};
const RERANK_MODES = ['top','hidden','emerging','warnings'];
const INSIGHT_MODES = ['creative','geo','demo','platforms','fatigue','copy'];
const LATEST_MODE = 'latest';
const VETERAN_MODES = ['veteran','saturation','follow'];
let currentMode = 'top';
let lastItems = [];

async function fetchJSON(url, opts){
  // 2026-05-05 v2 reliability: 8s timeout via AbortController + 3 retries with
  // exponential backoff (300/600/1200ms). Retry only on network errors and 5xx;
  // 4xx aborts immediately. Aborted/timed-out fetches surface as Error('timeout').
  const TIMEOUT_MS = 8000;
  const MAX_ATTEMPTS = 3;
  let lastErr;
  for(let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++){
    const ac = new AbortController();
    const t = setTimeout(()=>ac.abort(), TIMEOUT_MS);
    try{
      const r = await fetch(url, {...(opts||{}), signal: ac.signal});
      clearTimeout(t);
      if(!r.ok){
        // Don't retry 4xx — request is wrong; do retry 5xx (server flapped).
        if(r.status >= 400 && r.status < 500) throw new Error(r.status);
        lastErr = new Error(r.status);
        if(attempt < MAX_ATTEMPTS) await new Promise(res=>setTimeout(res, 300 * Math.pow(2, attempt-1)));
        continue;
      }
      return await r.json();
    }catch(e){
      clearTimeout(t);
      // AbortError → treat as timeout
      const isAbort = e && (e.name === 'AbortError' || /abort/i.test(String(e.message||'')));
      lastErr = isAbort ? new Error('timeout') : e;
      // Don't retry hard 4xx errors thrown above
      if(e && /^4\\d\\d$/.test(String(e.message||''))) { throw e; }
      if(attempt < MAX_ATTEMPTS) await new Promise(res=>setTimeout(res, 300 * Math.pow(2, attempt-1)));
    }
  }
  throw lastErr || new Error('fetch failed');
}
function escapeHtml(s){ return String(s||'').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
const fmt = n => (n||0).toLocaleString();
const pct = (n,d) => d>0 ? ((n/d)*100).toFixed(1) : '0.0';

async function loadStats(){
  const [lp, health, sumv3] = await Promise.all([
    fetchJSON('/admin/lp-fingerprints/stats').catch(()=>null),
    fetchJSON('/health').catch(()=>null),
    fetchJSON('/admin/insights/v3/summary?days=14').catch(()=>null),
  ]);
  const el = document.getElementById('stats');
  const bits = [];
  if(lp){
    bits.push(`<div class="stat ok">LP 覆盖 <b>${lp.coverage_pct}%</b> <span style="color:var(--muted)">${lp.probed}/${lp.total_brands_with_url}</span></div>`);
    bits.push(`<div class="stat">Shopify <b>${lp.tech_distribution['lp:shopify']||0}</b></div>`);
    bits.push(`<div class="stat">Klaviyo <b>${lp.tech_distribution['lp:klaviyo']||0}</b></div>`);
    if(lp.dropship_count>0) bits.push(`<div class="stat warn">DropShip <b>${lp.dropship_count}</b></div>`);
  }
  if(sumv3 && !sumv3.error){
    bits.push(`<div class="stat">14d 广告 <b>${fmt(sumv3.total_ads)}</b></div>`);
    bits.push(`<div class="stat">品牌 <b>${fmt(sumv3.unique_brands)}</b></div>`);
    bits.push(`<div class="stat">视频/轮播 <b>${fmt(sumv3.video_ads)}</b>/<b>${fmt(sumv3.carousel_ads)}</b></div>`);
    if(sumv3.graphql_coverage_pct>0)
      bits.push(`<div class="stat ok" title="带 regional_distribution 的占比">GraphQL 覆盖 <b>${sumv3.graphql_coverage_pct}%</b></div>`);
  }
  if(health && health.scraper){
    const s = health.scraper;
    bits.push(`<div class="stat">今日抓 <b>${s.today_count}</b></div>`);
    bits.push(`<div class="stat">总计 <b>${s.total_scraped.toLocaleString()}</b></div>`);
    if(s.consecutive_challenges>0) bits.push(`<div class="stat warn">风控 <b>${s.consecutive_challenges}</b></div>`);
    const ss = document.getElementById('scraper-status');
    const eta = s.next_cycle_eta_sec ? ` · 下次 ${Math.round(s.next_cycle_eta_sec/60)}min` : '';
    ss.innerHTML = `🛡️ Scraper: <b>${s.today_count}/天</b>${eta}${s.in_cooldown?' · ❄️冷却':''}`;
  }
  el.innerHTML = bits.join('');
}

function switchTab(mode){
  currentMode = mode;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.mode===mode));
  // toggle controls
  const isInsight = INSIGHT_MODES.includes(mode);
  const isLatest = (mode === LATEST_MODE);
  const isVeteran = VETERAN_MODES.includes(mode);
  document.getElementById('ctrl-rerank').style.display  = (isInsight || isLatest || isVeteran) ? 'none' : 'flex';
  document.getElementById('ctrl-insight').style.display = isInsight ? 'flex' : 'none';
  document.getElementById('ctrl-latest').style.display  = isLatest ? 'flex' : 'none';
  document.getElementById('ctrl-veteran').style.display = (mode==='veteran') ? 'flex' : 'none';
  document.getElementById('ctrl-saturation').style.display = (mode==='saturation') ? 'flex' : 'none';
  document.getElementById('ctrl-follow').style.display = (mode==='follow') ? 'flex' : 'none';
  // hide tier histogram on insight + latest + veteran tabs
  document.getElementById('tier-hist').style.display = (isInsight || isLatest || isVeteran) ? 'none' : '';
  load();
}

function priceInBand(p, band){
  if(!p || !band) return true;
  if(band==='low') return p<20;
  if(band==='sweet1') return p>=20 && p<40;
  if(band==='sweet2') return p>=40 && p<90;
  if(band==='aov') return p>=90 && p<150;
  if(band==='premium') return p>=150 && p<300;
  if(band==='luxury') return p>=300;
  return true;
}

async function load(){
  if(RERANK_MODES.includes(currentMode)) return loadRerank();
  if(currentMode === LATEST_MODE) return loadLatest();
  if(currentMode === 'veteran') return loadVeteran();
  if(currentMode === 'saturation') return loadSaturation();
  if(currentMode === 'follow') return loadFollow();
  return loadInsight(currentMode);
}

// ── Rerank tabs (existing 4) ────────────────────────────────────────────────

async function loadRerank(){
  const min = document.getElementById('f-min').value;
  const limit = +document.getElementById('f-limit').value;
  const only_shopify = document.getElementById('f-shopify').checked;
  const lponly = document.getElementById('f-lponly').checked;
  const tierMin = document.getElementById('f-tier').value;
  const priceBand = document.getElementById('f-price').value;
  const content = document.getElementById('content');
  content.className = 'loading'; content.textContent = '加载中…';
  try{
    const d = await fetchJSON(`/recommendations/rerank-v2?limit=${Math.min(500,limit*5)}&min_base_score=${min}&only_shopify=${only_shopify}`);
    let items = d.items.slice();
    lastItems = items;
    renderHistogram(items);

    if(lponly) items = items.filter(it => it.v2_tags.some(t=>t.startsWith('lp:')));
    items = items.filter(it => TIER_RANK[it.v2_tier] >= TIER_RANK[tierMin]);
    if(priceBand){
      items = items.filter(it => {
        const pt = it.v2_tags.find(t=>t.startsWith('price:'));
        const m = pt && pt.match(/\\$(\\d+)/);
        if(!m) return priceBand === '';
        return priceInBand(+m[1], priceBand);
      });
    }
    if(currentMode==='hidden'){
      items = items.filter(it => it.base_score < 105 && it.v2_total >= 55
                              && it.v2_breakdown.lp_fingerprint >= 20);
    } else if(currentMode==='emerging'){
      items = items.filter(it => it.v2_tags.some(t=>t.includes('EMERGING')));
    } else if(currentMode==='warnings'){
      items = items.filter(it => (it.v2_breakdown.dropship_penalty||0) < 0
                              || it.v2_tags.some(t=>t.includes('SATURATED') || t.includes('DISCOUNT-STACK')));
    }
    items = items.slice(0, limit);
    renderTable(items, d.doctrine);
  }catch(e){
    content.className='err'; content.textContent = '加载失败: ' + e.message;
  }
}

function renderHistogram(items){
  const tiers = ['S','A','B','C','D','F'];
  const cnt = {S:0,A:0,B:0,C:0,D:0,F:0};
  items.forEach(it => cnt[it.v2_tier] = (cnt[it.v2_tier]||0)+1);
  const max = Math.max(...Object.values(cnt), 1);
  document.getElementById('tier-hist').innerHTML = tiers.map(t =>
    `<div class="${t}" style="height:${Math.round(cnt[t]/max*100)}%" title="${t}: ${cnt[t]}"><span>${t}·${cnt[t]}</span></div>`
  ).join('');
}

function renderTable(items, doctrine){
  const content = document.getElementById('content');
  if(!items.length){ content.innerHTML = '<div class="loading">无匹配项 · 试试降低 Tier 或换模式</div>'; return; }
  let html = `<div class="sub" style="margin:10px 0 6px">策略: ${doctrine} · 当前模式: <b>${currentMode}</b> · ${items.length} 条</div>`;
  html += '<table><thead><tr>';
  html += '<th>#</th><th>品牌</th><th>关键词</th><th class="num">Base</th><th class="num">V2</th><th>Tier</th><th class="num">综合</th>';
  html += '<th>价格</th><th>Velocity</th><th>Age</th><th>Cluster</th><th>Geo</th><th>LP / Reddit</th><th>核心信号</th>';
  html += '</tr></thead><tbody>';
  items.forEach((it, i) => {
    const tags = it.v2_tags || [];
    const lpTags = tags.filter(t => t.startsWith('lp:'));
    const redditTags = tags.filter(t => t.startsWith('reddit:'));
    const findTag = (prefix) => (tags.find(t=>t.startsWith(prefix))||'').replace(prefix,'');
    const vel = findTag('velocity:');
    const age = findTag('age:');
    const cls = findTag('cluster:');
    const geo = findTag('geo:');
    const eng = findTag('engage:');
    const price = findTag('price:');
    const ds = it.v2_breakdown.dropship_penalty || 0;
    const lp = it.v2_breakdown.lp_fingerprint || 0;
    const rb = it.v2_breakdown.reddit_signals || 0;
    const pb = it.v2_breakdown.price_band || 0;
    const lpStr = lpTags.slice(0,4).map(t => `<span class="tag lp">${t.replace('lp:','')}</span>`).join('');
    const rStr = redditTags.slice(0,3).map(t => {
      const txt = t.replace('reddit:','');
      const cls = txt.includes('DISCOUNT-STACK') || txt.includes('short-return') ? 'bad' : 'reddit';
      return `<span class="tag ${cls}">${txt}</span>`;
    }).join('');
    const priceStr = price ? `<span class="tag price">${price}</span>` : '—';
    const coreStr = [
      vel && (vel.startsWith('rocket')||vel.startsWith('fast')) && `<span class="tag hot">⚡${vel}</span>`,
      eng && eng.startsWith('VIRAL') && `<span class="tag hot">👀${eng}</span>`,
      cls.startsWith('EMERGING') && `<span class="tag hot">🎯${cls}</span>`,
      cls.startsWith('SATURATED') && `<span class="tag bad">${cls}</span>`,
      ds < 0 && `<span class="tag bad">DROPSHIP -25</span>`,
    ].filter(Boolean).join('');
    html += `<tr>
      <td>${i+1}</td>
      <td class="brand"><a href="${it.landing_url||'#'}" target="_blank">${escapeHtml(it.brand_name||'?')}</a><br><span style="color:var(--muted);font-size:11px">${it.domain}</span></td>
      <td style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(it.keyword||'')}">${escapeHtml(it.keyword||'')}</td>
      <td class="num">${it.base_score}</td>
      <td class="num" style="color:var(--accent);font-weight:600">${it.v2_total}</td>
      <td><span class="tier tier-${it.v2_tier}">${it.v2_tier}</span></td>
      <td class="num" style="color:var(--s);font-weight:700">${it.combined_score.toFixed(1)}</td>
      <td>${priceStr}${pb?`<br><span style="color:var(--muted);font-size:10px">+${pb}</span>`:''}</td>
      <td>${escapeHtml(vel||'—')}</td>
      <td>${escapeHtml(age||'—')}</td>
      <td>${escapeHtml(cls||'—')}</td>
      <td>${escapeHtml(geo||'—')}</td>
      <td>${lp>0?`<b style="color:var(--ok)">+${lp}</b>`:''} ${rb?`<b style="color:var(--s)">R+${rb}</b>`:''}<br>${lpStr} ${rStr}</td>
      <td>${coreStr||'—'}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  content.className = ''; content.innerHTML = html;
}

// ── Insight tabs (v3) ───────────────────────────────────────────────────────

function _insightFilters(){
  const days = +document.getElementById('i-days').value || 14;
  const dom = (document.getElementById('i-domain').value||'').trim();
  const kw = (document.getElementById('i-keyword').value||'').trim();
  let q = `days=${days}`;
  if(dom) q += `&domain=${encodeURIComponent(dom)}`;
  if(kw) q += `&keyword=${encodeURIComponent(kw)}`;
  return q;
}

async function loadInsight(mode){
  const content = document.getElementById('content');
  content.className = 'loading'; content.textContent = '加载中…';
  const q = _insightFilters();
  const url = {
    creative:  '/admin/insights/v3/creative-mix',
    geo:       '/admin/insights/v3/geo-distribution',
    demo:      '/admin/insights/v3/demographics',
    platforms: '/admin/insights/v3/platforms',
    fatigue:   '/admin/insights/v3/creative-fatigue',
    copy:      '/admin/insights/v3/copy-patterns',
  }[mode];
  if(!url){ content.className='err'; content.textContent='unknown insight mode'; return; }
  try{
    const data = await fetchJSON(url + '?' + q);
    if(data.error){
      content.className='err';
      content.textContent = '尚无数据: ' + data.error
        + ' · 等下一次扫描周期完成后才有 ad-level 数据 (ad_archive 表为空)';
      return;
    }
    const renderer = {
      creative:  renderCreative,
      geo:       renderGeo,
      demo:      renderDemo,
      platforms: renderPlatforms,
      fatigue:   renderFatigue,
      copy:      renderCopy,
    }[mode];
    content.className = ''; content.innerHTML = renderer(data);
  }catch(e){
    content.className='err'; content.textContent = '加载失败: ' + e.message;
  }
}

function _bars(rows, opts){
  if(!rows || !rows.length) return '<div style="color:var(--muted);font-size:12px">无数据</div>';
  const maxN = Math.max(...rows.map(r=>r.value||0), 1);
  const cls = opts && opts.warm ? 'bars bars-warm' : (opts && opts.cool ? 'bars bars-cool' : 'bars');
  return `<div class="${cls}">` + rows.map(r => `
    <div class="row" title="${escapeHtml(r.label)}">
      <div class="label">${escapeHtml(r.label)}</div>
      <div class="bar"><i style="width:${Math.round((r.value||0)/maxN*100)}%"></i></div>
      <div class="num">${r.display!=null?r.display:fmt(r.value||0)}</div>
    </div>
  `).join('') + '</div>';
}

function renderCreative(d){
  const m = d.media_mix || {};
  const total = m.total || 0;
  const fmtRows = (d.format_mix||[]).slice(0,10).map(r => ({label: r.format||'unknown', value: r.count, display: fmt(r.count)}));
  const ctaRows = (d.cta_distribution||[]).slice(0,12).map(r => ({label: r.cta, value: r.count, display: fmt(r.count)}));
  const durRows = (d.video_duration_histogram||[]).map(r => ({label: r.range, value: r.count, display: fmt(r.count)}));
  const tl = (d.text_length_percentiles && d.text_length_percentiles.title) || {};
  const bl = (d.text_length_percentiles && d.text_length_percentiles.body) || {};
  return `
    <div class="grid3" style="margin-bottom:12px">
      <div class="card"><h3>媒体构成 (${d.window_days||14}d)</h3>
        <div style="display:flex;gap:14px;flex-wrap:wrap">
          <div><div class="big">${pct(m.video,total)}%</div><div class="sub2">视频 ${fmt(m.video)}</div></div>
          <div><div class="big" style="color:var(--s)">${pct(m.image,total)}%</div><div class="sub2">单图 ${fmt(m.image)}</div></div>
          <div><div class="big" style="color:var(--warn)">${pct(m.carousel,total)}%</div><div class="sub2">轮播 ${fmt(m.carousel)}</div></div>
        </div>
        <div class="sub2" style="margin-top:6px">总样本 ${fmt(total)}</div>
      </div>
      <div class="card"><h3>显示格式</h3>${_bars(fmtRows)}</div>
      <div class="card"><h3>CTA 分布</h3>${_bars(ctaRows, {warm:true})}</div>
    </div>
    <div class="grid2">
      <div class="card"><h3>视频时长分布</h3>${_bars(durRows, {cool:true})}</div>
      <div class="card"><h3>文案长度分位</h3>
        <table style="font-size:13px">
          <tr><th>类型</th><th class="num">P25</th><th class="num">P50</th><th class="num">P75</th><th class="num">P95</th></tr>
          <tr><td>title</td><td class="num">${tl.p25||0}</td><td class="num">${tl.p50||0}</td><td class="num">${tl.p75||0}</td><td class="num">${tl.p95||0}</td></tr>
          <tr><td>body</td><td class="num">${bl.p25||0}</td><td class="num">${bl.p50||0}</td><td class="num">${bl.p75||0}</td><td class="num">${bl.p95||0}</td></tr>
        </table>
        <div class="sub2" style="margin-top:8px">字符数 (含空格)</div>
      </div>
    </div>`;
}

function renderGeo(d){
  const cRows = (d.top_countries||[]).slice(0,20).map(r => ({label: r.country, value: r.weight, display: r.weight.toFixed(1)}));
  const sRows = (d.top_us_states||[]).slice(0,15).map(r => ({label: r.state, value: r.weight, display: r.weight.toFixed(1)}));
  return `
    <div class="grid3" style="margin-bottom:12px">
      <div class="card"><h3>EU 总曝光 (${d.window_days||14}d)</h3>
        <div class="big">${fmt(d.eu_total_reach_sum||0)}</div>
        <div class="sub2">${fmt(d.eu_political_ad_count||0)} 条政治/敏感广告</div>
      </div>
      <div class="card" style="grid-column:span 2"><h3>顶级国家 / 地区 (regional_distribution + target_locations)</h3>${_bars(cRows)}</div>
    </div>
    <div class="card"><h3>美国州分布</h3>${_bars(sRows, {warm:true})}</div>`;
}

function _heatColor(v, max){
  if(!v) return '#0b131c';
  const t = Math.min(1, v/max);
  // gradient: dark blue -> teal -> warn -> pink
  const r = Math.round(91 + (236-91)*t);
  const g = Math.round(157 + (72-157)*t);
  const b = Math.round(255 + (153-255)*t);
  return `rgba(${r},${g},${b},${0.3 + 0.7*t})`;
}

function renderDemo(d){
  const grid = d.age_gender_heatmap || [];
  const allVals = grid.flatMap(r => [r.male, r.female, r.unknown]);
  const max = Math.max(...allVals, 1);
  let html = '<div class="grid2">';
  html += '<div class="card"><h3>年龄 × 性别 热图 (% reach)</h3>';
  html += '<div class="heatmap">';
  html += '<div class="hd">年龄</div><div class="hd">男</div><div class="hd">女</div><div class="hd">未知</div>';
  grid.forEach(r => {
    html += `<div class="age">${r.age}</div>`;
    html += `<div class="cell" style="background:${_heatColor(r.male,max)}">${r.male.toFixed(1)}%</div>`;
    html += `<div class="cell" style="background:${_heatColor(r.female,max)}">${r.female.toFixed(1)}%</div>`;
    html += `<div class="cell" style="background:${_heatColor(r.unknown,max)}">${r.unknown.toFixed(1)}%</div>`;
  });
  html += '</div></div>';
  const aRows = (d.top_target_ages||[]).map(r => ({label: r.value, value: r.count, display: fmt(r.count)}));
  const gRows = (d.top_target_genders||[]).map(r => ({label: r.value, value: r.count, display: fmt(r.count)}));
  html += '<div class="card"><h3>定向年龄 / 性别 (target_*)</h3>'
       + '<h4 style="margin:0 0 4px;font-size:11px;color:var(--muted)">target_ages</h4>' + _bars(aRows.slice(0,8))
       + '<h4 style="margin:10px 0 4px;font-size:11px;color:var(--muted)">target_genders</h4>' + _bars(gRows, {warm:true})
       + '</div>';
  html += '</div>';
  return html;
}

function renderPlatforms(d){
  const pRows = (d.platforms||[]).map(r => ({label: r.platform, value: r.count, display: `${fmt(r.count)} (${r.pct}%)`}));
  const cRows = (d.top_combinations||[]).slice(0,10).map(r => ({label: r.combo, value: r.count, display: fmt(r.count)}));
  return `
    <div class="grid2">
      <div class="card"><h3>单平台计数 (${fmt(d.total_ads||0)} 广告)</h3>${_bars(pRows)}</div>
      <div class="card"><h3>组合投放</h3>${_bars(cRows, {cool:true})}</div>
    </div>`;
}

function renderFatigue(d){
  const histRows = (d.active_days_histogram||[]).map(r => ({label: r.range, value: r.count, display: fmt(r.count)}));
  let html = '<div class="grid2" style="margin-bottom:12px">';
  html += `<div class="card"><h3>active_days 分布</h3>${_bars(histRows)}</div>`;
  html += '<div class="card"><h3>长寿赢家 (≥60d 仍在跑)</h3>';
  const winners = (d.long_running_winners||[]).slice(0,15);
  if(!winners.length){ html += '<div class="sub2">暂无</div>'; }
  else {
    html += '<table style="font-size:12px"><tr><th>品牌</th><th class="num">天数</th><th>开播</th></tr>';
    winners.forEach(w => {
      html += `<tr><td>${escapeHtml(w.page_name||'?')}<br><span class="sub2">${escapeHtml(w.domain||'')}</span></td>`
            + `<td class="num"><b style="color:var(--ok)">${w.active_days}</b></td>`
            + `<td><span class="sub2">${w.start_date_iso||'—'}</span></td></tr>`;
    });
    html += '</table>';
  }
  html += '</div></div>';
  const turn = d.brand_turnover || [];
  html += '<div class="card"><h3>品牌创意周转 (高分=快速换素材)</h3>';
  if(!turn.length){ html += '<div class="sub2">无</div>'; }
  else {
    html += '<table style="font-size:13px"><tr><th>域名</th><th class="num">广告数</th><th class="num">平均天数</th><th class="num">turnover</th></tr>';
    turn.slice(0,15).forEach(t => {
      html += `<tr><td>${escapeHtml(t.domain)}</td><td class="num">${t.ads_in_window}</td>`
            + `<td class="num">${t.avg_active_days}</td>`
            + `<td class="num"><b style="color:var(--s)">${t.turnover_score}</b></td></tr>`;
    });
    html += '</table>';
  }
  html += '</div>';
  return html;
}

function renderCopy(d){
  const titRows = (d.top_titles||[]).slice(0,15).map(r => ({label: r.title, value: r.count, display: fmt(r.count)}));
  const ctRows = (d.top_cta_texts||[]).slice(0,12).map(r => ({label: r.cta_text, value: r.count, display: fmt(r.count)}));
  const dRows = (d.top_discount_pcts||[]).map(r => ({label: r.pct + '% off', value: r.count, display: fmt(r.count)}));
  return `
    <div class="grid3" style="margin-bottom:12px">
      <div class="card"><h3>紧迫感词频 (${fmt(d.total_ads||0)} 广告)</h3>
        <div class="big" style="color:var(--warn)">${d.urgency_rate_pct||0}%</div>
        <div class="sub2">含 today/limited/free/now/exclusive...</div>
      </div>
      <div class="card"><h3>价格出现率</h3>
        <div class="big" style="color:var(--ok)">${d.price_mention_rate_pct||0}%</div>
        <div class="sub2">含 $XX 的广告</div>
      </div>
      <div class="card"><h3>折扣报价率</h3>
        <div class="big" style="color:var(--pink)">${d.discount_offer_rate_pct||0}%</div>
        <div class="sub2">含 X% off 的广告</div>
      </div>
    </div>
    <div class="grid2">
      <div class="card"><h3>顶级 headline (重复出现)</h3>${_bars(titRows)}</div>
      <div class="card"><h3>顶级 CTA 文本</h3>${_bars(ctRows, {warm:true})}<h4 style="margin:14px 0 4px;font-size:11px;color:var(--muted)">折扣区间分布</h4>${_bars(dRows, {cool:true})}</div>
    </div>`;
}

// ── 老兵直觉 / 渗透率 / 跟买卡 (v9.2) ─────────────────────────────────────

const VERDICT_STYLE = {
  'GO_FOLLOW': {color:'#10b981', emoji:'🟢', label:'立即跟'},
  'OBSERVE':   {color:'#f59e0b', emoji:'🟡', label:'跟进观察'},
  'WATCH':     {color:'#94a3b8', emoji:'⚪', label:'再观察'},
  'AVOID':     {color:'#ef4444', emoji:'🔴', label:'坚决不碰'},
  'NO_DATA':   {color:'#94a3b8', emoji:'⚪', label:'无数据'},
};

const SAT_STYLE = {
  'OPEN':     {color:'#10b981', emoji:'🟢', desc:'新人持续涌入 + 分散, 可冲'},
  'GROWING':  {color:'#5b9dff', emoji:'🔵', desc:'渐进式增长'},
  'TIGHT':    {color:'#f59e0b', emoji:'🟡', desc:'集中度偏高, 需差异化'},
  'LOCKED':   {color:'#ef4444', emoji:'🔴', desc:'头部锁死, 不推荐'},
  'STAGNANT': {color:'#94a3b8', emoji:'⚪', desc:'几无新血, 老牌坐庄'},
  'NO_DATA':  {color:'#94a3b8', emoji:'⚪', desc:'无数据'},
};

async function loadVeteran(){
  const days = +document.getElementById('V-days').value || 30;
  const limit = +document.getElementById('V-limit').value || 40;
  const verdict = document.getElementById('V-verdict').value;
  const active = document.getElementById('V-active').checked;
  const content = document.getElementById('content');
  content.className = 'loading'; content.textContent = '加载老兵直觉…';
  let q = `days=${days}&limit=${limit}&only_active=${active}`;
  if(verdict) q += `&verdict=${verdict}`;
  try{
    const d = await fetchJSON('/admin/insights/v3/veteran-flags?' + q);
    if(d.error){
      content.className='err'; content.textContent='尚无数据: ' + d.error;
      return;
    }
    content.className=''; content.innerHTML = renderVeteran(d);
  }catch(e){
    content.className='err'; content.textContent = '加载失败: ' + e.message;
  }
}

function renderVeteran(d){
  const brands = d.brands || [];
  if(!brands.length){
    return '<div class="loading">无符合条件品牌, 试试放宽 verdict 或扩窗口</div>';
  }
  let html = `<div class="sub" style="margin:10px 0 6px">老兵直觉 · ${brands.length} 品牌 · 窗口 ${d.window_days}d · 按综合分排序 (基础 30 分, 命中信号加减)</div>`;
  html += '<table><thead><tr>';
  html += '<th>#</th><th>品牌</th><th class="num">分</th><th>判断</th><th>规模</th><th>老兵 flags</th><th>关键词覆盖</th>';
  html += '</tr></thead><tbody>';
  brands.forEach((b, i) => {
    const v = VERDICT_STYLE[b.verdict] || VERDICT_STYLE.WATCH;
    const flagsHtml = (b.flags || []).map(f =>
      `<span class="tag" title="${escapeHtml(f.reason)}" style="background:#1e293b;color:#cbd5e1">${f.emoji} ${escapeHtml(f.code)}</span>`
    ).join(' ') || '<span class="sub2">—</span>';
    const ig = b.ig_handle ? `<br><span class="sub2">@${escapeHtml(b.ig_handle)}${b.ig_followers?` · ${fmt(b.ig_followers)}fol`:''}</span>` : '';
    html += `<tr>
      <td>${i+1}</td>
      <td class="brand">
        <a href="javascript:void(0)" onclick="goFollow('${escapeHtml(b.domain)}')" style="color:var(--accent)">${escapeHtml(b.page_name||b.domain)}</a><br>
        <span class="sub2">${escapeHtml(b.domain)}${b.page_likes_max?` · ${fmt(b.page_likes_max)}赞`:''}</span>${ig}
      </td>
      <td class="num"><b style="color:${v.color};font-size:18px">${b.score}</b></td>
      <td><span class="tag" style="background:${v.color}33;color:${v.color};font-weight:600">${v.emoji} ${v.label}</span></td>
      <td>
        <span class="sub2">${b.ads_active}/${b.ads_total} active</span><br>
        <span class="sub2">最长 ${b.max_active_days}d · 入库 ${b.days_since_first}d 前</span>
      </td>
      <td>${flagsHtml}</td>
      <td><span class="sub2">${b.keywords_count} 关键词 · ${b.targets_countries} 国 · 变体最多 ${b.collation_max}</span></td>
    </tr>`;
  });
  html += '</tbody></table>';
  return html;
}

async function loadSaturation(){
  const kw = (document.getElementById('S-keyword').value || '').trim();
  const days = +document.getElementById('S-days').value || 30;
  const content = document.getElementById('content');
  if(!kw){
    content.className='loading'; content.textContent='输入一个关键词然后点 🔍 分析';
    return;
  }
  content.className='loading'; content.textContent='分析渗透率…';
  try{
    const d = await fetchJSON(`/admin/insights/v3/saturation?keyword=${encodeURIComponent(kw)}&days=${days}`);
    if(d.error){ content.className='err'; content.textContent=d.error; return; }
    content.className=''; content.innerHTML = renderSaturation(d);
  }catch(e){
    content.className='err'; content.textContent = '加载失败: ' + e.message;
  }
}

function renderSaturation(d){
  const v = SAT_STYLE[d.verdict] || SAT_STYLE.NO_DATA;
  const top = d.top_brands || [];
  const hhiInterp = d.hhi >= 4000 ? '高度集中' : d.hhi >= 2500 ? '偏紧' : d.hhi >= 1500 ? '中等' : '分散';
  let html = `
    <div class="grid3" style="margin-bottom:14px">
      <div class="card" style="border:2px solid ${v.color}">
        <h3 style="color:${v.color}">渗透率判定</h3>
        <div class="big" style="color:${v.color};font-size:28px">${v.emoji} ${d.verdict}</div>
        <div class="sub2">${v.desc}</div>
        <div class="sub2" style="margin-top:6px"><b>${escapeHtml(d.keyword)}</b> · ${d.window_days}d · ${fmt(d.total_ads)} 广告 / ${d.brands} 品牌</div>
      </div>
      <div class="card">
        <h3>头部集中度</h3>
        <div class="big">${d.top3_share_pct}%</div>
        <div class="sub2">Top-3 品牌占总广告</div>
        <div class="sub2" style="margin-top:6px">HHI <b style="color:${v.color}">${d.hhi}</b> (${hhiInterp})</div>
      </div>
      <div class="card">
        <h3>新进入者 (7d)</h3>
        <div class="big" style="color:#10b981">${d.new_entrants_7d}</div>
        <div class="sub2">${d.new_entrants_7d>=3?'活跃涌入, niche 有水分':d.new_entrants_7d>=1?'有新血':'几无新人, 老牌封锁'}</div>
      </div>
    </div>
    <div class="card">
      <h3>头部品牌 (按广告数)</h3>
      <table style="font-size:13px">
        <tr><th>#</th><th>域名</th><th class="num">广告数</th><th class="num">占比</th><th>首次入库</th><th></th></tr>`;
  top.forEach((b, i) => {
    html += `<tr>
      <td>${i+1}</td>
      <td><a href="javascript:void(0)" onclick="goFollow('${escapeHtml(b.domain)}')" style="color:var(--accent)">${escapeHtml(b.domain)}</a></td>
      <td class="num">${b.ads}</td>
      <td class="num"><b>${b.share_pct}%</b></td>
      <td><span class="sub2">${b.first_seen}</span></td>
      <td><button class="secondary" style="font-size:11px;padding:2px 8px" onclick="goFollow('${escapeHtml(b.domain)}')">🎯 跟买卡</button></td>
    </tr>`;
  });
  html += '</table></div>';
  return html;
}

function goFollow(domain){
  document.getElementById('F-domain').value = domain;
  switchTab('follow');
}

async function loadFollow(){
  const dom = (document.getElementById('F-domain').value || '').trim();
  const days = +document.getElementById('F-days').value || 60;
  const content = document.getElementById('content');
  if(!dom){
    content.className='loading'; content.textContent='输入品牌域名然后点 🎯 出卡 (或在 老兵直觉/渗透率 tab 点品牌跳过来)';
    return;
  }
  content.className='loading'; content.textContent='生成跟买卡…';
  try{
    const d = await fetchJSON(`/admin/insights/v3/follow-card?domain=${encodeURIComponent(dom)}&days=${days}`);
    if(d.error){ content.className='err'; content.textContent=d.error; return; }
    content.className=''; content.innerHTML = renderFollowCard(d);
  }catch(e){
    content.className='err'; content.textContent = '加载失败: ' + e.message;
  }
}

function renderFollowCard(d){
  const sig = d.signal || {};
  const port = d.portfolio || {};
  const v = VERDICT_STYLE[d.verdict] || VERDICT_STYLE.WATCH;
  const flagsHtml = (sig.flags || []).map(f =>
    `<div style="display:flex;gap:8px;align-items:start;margin:4px 0"><span style="font-size:18px">${f.emoji}</span><div><b style="color:#fbbf24">${f.code}</b><br><span class="sub2">${escapeHtml(f.reason)}</span></div></div>`
  ).join('') || '<span class="sub2">无强信号</span>';

  const playsHtml = (d.plays || []).map(p =>
    `<li style="margin:6px 0;line-height:1.5">${escapeHtml(p)}</li>`
  ).join('');

  const winners = d.long_running_winners || [];
  const winnersHtml = winners.length ? winners.map((w, i) => `
    <div class="card" style="margin:6px 0;padding:10px;background:var(--panel2)">
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <span class="tag" style="background:#14532d;color:#86efac">#${i+1} 长寿 ${w.active_days}d</span>
        <span class="tag price">${escapeHtml(w.cta_type||'')}</span>
        <span class="tag">${w.has_video?'视频':w.is_carousel?'轮播':'单图'}</span>
        ${w.format?`<span class="sub2">${escapeHtml(w.format)}</span>`:''}
        <a href="${w.fb_url}" target="_blank" class="sub2" style="margin-left:auto">↗ FB原图</a>
      </div>
      <div style="margin-top:6px"><b>${escapeHtml(w.title)||'(无 title)'}</b></div>
      <div class="sub2" style="margin-top:4px">${escapeHtml(w.body)}${w.body && w.body.length>=200?'…':''}</div>
      ${w.cta_text?`<div class="sub2" style="margin-top:4px">CTA 文案: <i>${escapeHtml(w.cta_text)}</i></div>`:''}
    </div>
  `).join('') : '<div class="sub2">无 ≥60d 长寿广告</div>';

  return `
    <div class="grid2" style="margin-bottom:14px">
      <div class="card" style="border:2px solid ${v.color}">
        <h3>${escapeHtml(sig.page_name||d.domain)} <span class="sub2">(${escapeHtml(d.domain)})</span></h3>
        <div style="display:flex;gap:14px;flex-wrap:wrap;margin:10px 0">
          <div><div class="big" style="color:${v.color};font-size:32px">${v.emoji}</div><div class="sub2">${v.label}</div></div>
          <div><div class="big">${sig.score}</div><div class="sub2">综合分 (0-100)</div></div>
          <div><div class="big">${port.ads_active}/${port.ads_total}</div><div class="sub2">active / 总数</div></div>
        </div>
        <div class="sub2">入库 ${sig.days_since_first}d 前 · 最长 ${sig.max_active_days}d · 平均 ${sig.avg_active_days}d active</div>
        ${sig.ig_handle?`<div class="sub2">IG: @${escapeHtml(sig.ig_handle)}${sig.ig_followers?` · ${fmt(sig.ig_followers)} 粉丝`:''}</div>`:''}
        ${sig.page_likes_max?`<div class="sub2">FB 主页 ${fmt(sig.page_likes_max)} 赞</div>`:''}
      </div>
      <div class="card">
        <h3>🎖️ 老兵 flags</h3>
        ${flagsHtml}
      </div>
    </div>

    <div class="card" style="margin-bottom:14px;border-left:3px solid #fbbf24">
      <h3>📋 老兵建议 (Plays)</h3>
      <ul style="margin:6px 0 6px 18px;padding:0">${playsHtml}</ul>
      <div class="sub2" style="margin-top:8px">💵 进入价格点: ${escapeHtml(d.suggested_entry_price)}</div>
    </div>

    <div class="grid2" style="margin-bottom:14px">
      <div class="card">
        <h3>🪝 顶级 hooks (重复出现)</h3>
        ${(d.top_hooks || []).slice(0,5).map(h => `
          <div style="margin:6px 0">
            <div><b>${escapeHtml(h.hook)}</b></div>
            <div class="sub2">出现 ${h.count} 次</div>
          </div>
        `).join('') || '<span class="sub2">无</span>'}
      </div>
      <div class="card">
        <h3>📞 CTA 类型 / 文案 / 平台</h3>
        <h4 style="margin:0 0 4px;font-size:11px;color:var(--muted)">CTA TYPE</h4>
        ${(d.top_ctas || []).slice(0,5).map(c => `<span class="tag price">${escapeHtml(c.cta_type)} (${c.count})</span>`).join(' ') || '—'}
        <h4 style="margin:10px 0 4px;font-size:11px;color:var(--muted)">CTA 文案</h4>
        ${(d.top_cta_texts || []).slice(0,5).map(c => `<div class="sub2">"${escapeHtml(c.cta_text)}" × ${c.count}</div>`).join('') || '<span class="sub2">—</span>'}
        <h4 style="margin:10px 0 4px;font-size:11px;color:var(--muted)">投放平台</h4>
        ${(d.platform_mix || []).slice(0,8).map(p => `<span class="tag">${escapeHtml(p.platform)} ${p.count}</span>`).join(' ') || '—'}
      </div>
    </div>

    <div class="card" style="margin-bottom:14px">
      <h3>📊 portfolio 构成</h3>
      <div style="display:flex;gap:14px;flex-wrap:wrap">
        <div><div class="big">${port.video_count}</div><div class="sub2">视频</div></div>
        <div><div class="big" style="color:var(--s)">${port.image_count}</div><div class="sub2">单图</div></div>
        <div><div class="big" style="color:var(--warn)">${port.carousel_count}</div><div class="sub2">轮播</div></div>
        <div><div class="big">${port.price_mention_rate_pct}%</div><div class="sub2">含价格 $XX</div></div>
        ${port.avg_discount_pct?`<div><div class="big">${port.avg_discount_pct}%</div><div class="sub2">平均折扣</div></div>`:''}
      </div>
      <h4 style="margin:10px 0 4px;font-size:11px;color:var(--muted)">主投关键词</h4>
      ${(d.top_keywords || []).slice(0,8).map(k => `<span class="tag">${escapeHtml(k.keyword)} ×${k.count}</span>`).join(' ') || '—'}
    </div>

    <div class="card">
      <h3>🏆 长寿赢家 (≥60d 仍 active, 这是要抄的模板)</h3>
      ${winnersHtml}
    </div>
  `;
}

// ── Actions ─────────────────────────────────────────────────────────────────

// ── Latest data tab (📡) ────────────────────────────────────────────────────

async function loadLatest(){
  const limit = +document.getElementById('L-limit').value || 50;
  const hours = +document.getElementById('L-hours').value || 0;
  const kw = (document.getElementById('L-keyword').value||'').trim();
  const dom = (document.getElementById('L-domain').value||'').trim();
  const onlyGq = document.getElementById('L-graphql').checked;
  const content = document.getElementById('content');
  content.className = 'loading'; content.textContent = '加载最新数据…';
  let q = `limit=${limit}&hours=${hours}&only_with_graphql=${onlyGq}`;
  if(kw) q += `&keyword=${encodeURIComponent(kw)}`;
  if(dom) q += `&domain=${encodeURIComponent(dom)}`;
  try{
    const d = await fetchJSON('/admin/insights/v3/latest-ads?' + q);
    if(d.error){
      content.className='err';
      content.textContent = '尚无数据: ' + d.error
        + ' · 等下一次扫描周期完成后才有 ad-level 数据';
      return;
    }
    content.className = ''; content.innerHTML = renderLatest(d);
  }catch(e){
    content.className='err'; content.textContent = '加载失败: ' + e.message;
  }
}

function renderLatest(d){
  const items = d.items || [];
  if(!items.length){
    return `<div class="loading">暂无数据 (窗口 ${d.hours||'∞'}h, 总共 ${d.total_in_window||0} 条) ·
            等 daemon 跑下一轮抓取或换个时间窗口</div>`;
  }
  let html = `<div class="sub" style="margin:10px 0 6px">最近 <b style="color:#fdba74">${items.length}</b> 条 · 窗口内总 ${fmt(d.total_in_window)} · 按 <code>inserted_at</code> 倒序</div>`;
  html += '<table><thead><tr>';
  html += '<th>#</th><th>品牌 / 域名</th><th>关键词</th><th>开播 / 状态</th><th>媒体</th>'
       + '<th>标题 / 文案</th><th>CTA / 落地页</th><th>受众</th><th>合规</th><th>来源</th>';
  html += '</tr></thead><tbody>';
  items.forEach((it, i) => {
    const fbUrl = it.ad_library_url || '#';
    const mediaTag = it.is_carousel ? `<span class="tag hot">轮播 ${it.cards_count||0}</span>`
                    : (it.has_video ? `<span class="tag" style="background:#1e40af;color:#bfdbfe">视频 ${it.video_duration_sec? '·'+Math.round(it.video_duration_sec)+'s' : ''}</span>`
                    : (it.has_image ? `<span class="tag">单图</span>` : `<span class="tag">—</span>`));
    const audBits = [];
    if(it.target_ages) audBits.push(escapeHtml(it.target_ages));
    if(it.target_genders) audBits.push(escapeHtml(it.target_genders));
    const regionalCnt = Array.isArray(it.regional_distribution) ? it.regional_distribution.length : 0;
    if(regionalCnt) audBits.push(`<span class="tag" style="background:#1e3a8a;color:#bfdbfe">${regionalCnt} 区域</span>`);
    const compBits = [];
    if(it.eu_political_ad) compBits.push('<span class="tag bad">EU政治</span>');
    if(it.has_user_reported) compBits.push('<span class="tag bad">user reported</span>');
    if(it.contains_sensitive_content) compBits.push('<span class="tag bad">敏感</span>');
    if(it.branded_content) compBits.push('<span class="tag" style="background:#581c87;color:#e9d5ff">branded</span>');
    if(it.contains_digital_created_media) compBits.push('<span class="tag" style="background:#7c2d12;color:#fdba74">AI</span>');
    const srcCls = (it.source||'').includes('graphql') ? 'lp' : '';
    const titleSnip = (it.title||'').slice(0,80) || '<i style="color:var(--muted)">(无 title)</i>';
    const bodySnip = (it.body||'').slice(0,140);
    const start = it.start_date_iso || '—';
    const activeChip = it.is_active
      ? `<span class="tag" style="background:#14532d;color:#86efac">active ${it.active_days||0}d</span>`
      : `<span class="tag" style="background:#3f3f46;color:#a1a1aa">stopped</span>`;
    const ig = it.ig_handle ? `<br><span class="sub2">@${escapeHtml(it.ig_handle)}${it.ig_followers?` · ${fmt(it.ig_followers)}fol`:''}</span>` : '';
    html += `<tr>
      <td>${i+1}</td>
      <td class="brand"><a href="${fbUrl}" target="_blank" rel="noopener">${escapeHtml(it.page_name||'?')}</a><br>
          <span class="sub2">${escapeHtml(it.domain||'')}</span>${ig}</td>
      <td style="max-width:130px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(it.keyword||'')}">${escapeHtml(it.keyword||'')}</td>
      <td>${start} ${activeChip}<br><span class="sub2">入库 ${(it.inserted_at||'').replace('T',' ').slice(0,16)}</span></td>
      <td>${mediaTag}</td>
      <td style="max-width:280px"><b>${escapeHtml(titleSnip)}</b><br><span class="sub2">${escapeHtml(bodySnip)}${bodySnip.length>=140?'…':''}</span></td>
      <td>${it.cta_type?`<span class="tag price">${escapeHtml(it.cta_type)}</span>`:''}${it.cta_text?`<br><span class="sub2">${escapeHtml((it.cta_text||'').slice(0,30))}</span>`:''}${it.link_url?`<br><a href="${it.link_url}" target="_blank" rel="noopener" class="sub2">↗ LP</a>`:''}</td>
      <td>${audBits.join(' ')||'—'}</td>
      <td>${compBits.join(' ')||'<span class="sub2">clean</span>'}</td>
      <td><span class="tag ${srcCls}">${escapeHtml(it.source||'?')}</span></td>
    </tr>`;
  });
  html += '</tbody></table>';
  return html;
}

async function scrapeNow(){
  const kw = prompt('输入要立即抓取的 FB Ad Library 关键词 (后台执行 60-120s):', '');
  if(!kw || kw.trim().length < 2) return;
  const msg = document.getElementById('L-msg');
  msg.textContent = '提交中…';
  try{
    const r = await fetch('/admin/scrape-keyword?keyword=' + encodeURIComponent(kw.trim()), {
      method: 'POST',
    });
    const d = await r.json().catch(()=>({}));
    if(!r.ok){
      msg.textContent = `提交失败: ${r.status} ${d.detail||d.error||''}`;
      return;
    }
    msg.innerHTML = `🔥 已下单 <b style="color:#fdba74">${escapeHtml(kw)}</b> · 60-120s 后回这里点 🔄 查看新数据`;
  }catch(e){ msg.textContent = '提交失败: ' + e.message; }
}

async function probe(){
  const msg = document.getElementById('msg'); msg.textContent = '探针中 (可能 60s)…';
  try{
    const r = await fetch('/admin/lp-probe?limit=50&workers=10', {method:'POST'});
    const d = await r.json();
    msg.textContent = `探针 ${d.probed} · 成功 ${d.success_200||0} · ${d.elapsed_sec}s · ${d.throughput_per_sec}/s`;
    setTimeout(loadStats, 500);
  }catch(e){ msg.textContent = '探针失败: ' + e.message; }
}

async function snap(){
  const msg = document.getElementById('msg'); msg.textContent = '快照中…';
  try{
    const r = await fetch('/admin/snapshot-ad-count', {method:'POST'});
    const d = await r.json();
    msg.textContent = `快照: +${d.inserted} 新增 / ${d.total_history_rows} 历史 · velocity_ready=${d.velocity_ready}`;
  }catch(e){ msg.textContent = '快照失败: ' + e.message; }
}

loadStats();
load();
setInterval(loadStats, 30000);
</script>
</body>
</html>
"""


from . import __version__, RELEASE_NAME
from .paths import RESULTS_DIR, SCAN_FILE
from .persistence import get_persistence
from .recommendations import list_recommendations, rebuild_recommendations
from .models import (
    RawScanRequest, BrandResponse, BrandSummary,
    LearnedKeyword, ScanSummary, HealthResponse, V2ScrapeFastRequest,
)
from .server_additions import (
    ensure_selectpro_schema,
    register_selectpro_routes,
)
from .v3_dashboard import V3_DASHBOARD_HTML
from .defaults import scrub_output, DASH

log = get_logger(__name__)

# ── 全域开关: 排除非独立站 (平台 / 市集 / 跨境). 匹配 host == pattern 或以 '.'+pattern 结尾.
EXCLUDE_DOMAIN_PATTERNS = (
    # 低价平台
    'temu.com', 'shein.com',
    # 欧美主流市集 (Amazon 覆盖所有地区前缀)
    'amazon.com', 'amazon.co.uk', 'amazon.de', 'amazon.fr', 'amazon.it', 'amazon.es',
    'amazon.nl', 'amazon.ca', 'amazon.com.au', 'amazon.com.br', 'amazon.co.jp', 'amazon.in',
    'walmart.com', 'ebay.com', 'ebay.co.uk', 'etsy.com', 'bestbuy.com',
    'target.com', 'costco.com', 'homedepot.com', 'lowes.com',
    # 跨境平台
    'aliexpress.com', 'alibaba.com', 'tiktok.com', 'taobao.com', 'tmall.com',
    'jd.com', '1688.com',
)
_EXCLUDE_SET = frozenset(p.lower().strip('.') for p in EXCLUDE_DOMAIN_PATTERNS)
_EXCLUDE_SUFFIXES = tuple('.' + p for p in _EXCLUDE_SET)

def _clean_url(u):
    """归一化 URL: 去转义反斜杠、合并重复斜杠、容错空值."""
    if not u:
        return ''
    u = str(u).replace('\\/', '/').replace('\\\\', '\\')
    u = re.sub(r'(https?:)/{2,}', r'\1//', u)
    u = re.sub(r'([^:])/{2,}', r'\1/', u)
    return u

def _is_excluded_domain(d):
    if not d: return False
    dl = str(d).lower().strip().lstrip('.')
    if dl in _EXCLUDE_SET:
        return True
    for s in _EXCLUDE_SUFFIXES:
        if dl.endswith(s):
            return True
    return False

def _fb_ready_count(conn) -> int:
    """Count recommendations with fb_fitness >= 60."""
    try:
        return conn.execute("SELECT COUNT(*) FROM recommendations WHERE fb_fitness >= 60").fetchone()[0]
    except Exception:
        return 0

def _host_of(url):
    """从 URL 提取 host (小写, 去 www.)."""
    if not url:
        return ''
    try:
        from urllib.parse import urlparse
        h = urlparse(str(url)).hostname or ''
        h = h.lower()
        if h.startswith('www.'):
            h = h[4:]
        return h
    except Exception:
        return ''

def _sanitize_rows(rows, exclude_platforms: bool = True):
    """对输出行统一清洗: 修 URL; exclude_platforms=True 时还过滤 domain / landing_url / link_url 指向黑名单平台的行; scrub None/空值."""
    if not rows: return rows
    out = []
    for r in rows:
        if exclude_platforms:
            if _is_excluded_domain(r.get('domain')): continue
            if _is_excluded_domain(_host_of(r.get('landing_url'))): continue
            if _is_excluded_domain(_host_of(r.get('link_url'))): continue
        if 'landing_url' in r: r['landing_url'] = _clean_url(r.get('landing_url'))
        if 'link_url' in r:    r['link_url']    = _clean_url(r.get('link_url'))
        out.append(scrub_output(r))
    return out

_DATE_FIELDS = ('scan_date', 'created_at', 'scanned_at', 'first_seen_at', 'updated_at')

def _apply_date_filter(rows, date):
    """后置过滤: 只保留某一天 (YYYY-MM-DD) 的行; 取第一个非空的日期候选字段比对前 10 字符."""
    if not date or not rows:
        return rows
    out = []
    for r in rows:
        for f in _DATE_FIELDS:
            v = r.get(f)
            if v:
                if str(v)[:10] == date:
                    out.append(r)
                break
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(f"FastAPI startup — pipeline {__version__} ({RELEASE_NAME})")
    db = get_persistence()  # eager init of schema
    ensure_selectpro_schema()
    # Warm one pool connection on startup so the first request doesn't pay
    # the ATTACH+CREATE-VIEW cost. The threadpool worker that handles the
    # first request will still build its own thread-local conn, but the OS
    # page cache and shard schema metadata will already be hot.
    try:
        with db._conn() as _c:
            _c.execute("SELECT 1").fetchone()
    except Exception as e:
        log.warning(f"lifespan: pool warm failed: {e}")
    # Pre-warm the dashboard's hot endpoint caches in a background thread so
    # the first browser load doesn't fan out to N parallel cold queries.
    # The same thread also runs a refresh loop on a cadence shorter than the
    # cache TTL so users never observe a cold-miss after the initial warm.
    # Singleflight in ttl_cache prevents stampedes once the cache has *any*
    # entry; the refresher keeps that "any entry" condition true.
    import threading as _th

    _warm_stop = _th.Event()
    app.state._warm_stop = _warm_stop  # type: ignore[attr-defined]

    def _warm_once() -> None:
        # cache_warm bypasses the TTL check and goes through the wrapper's
        # singleflight lock, so the old cached entry stays visible to readers
        # until the new one is computed and atomically swapped in. No
        # empty-slot window where bursts would serialize on a cold key.
        insights_v3_summary.cache_warm(days=14)
        insights_v3_veteran_flags.cache_warm(days=30, limit=50,
                                              only_active=True, min_ads=2,
                                              verdict=None)
        insights_v3_latest_ads.cache_warm(limit=50, hours=0, keyword=None,
                                          domain=None, only_with_graphql=False)
        insights_v3_latest_ads.cache_warm(limit=50, hours=24, keyword=None,
                                          domain=None, only_with_graphql=False)
        insights_v3_creative_mix.cache_warm(days=14, domain=None, keyword=None)
        insights_v3_geo.cache_warm(days=14, domain=None, keyword=None)
        insights_v3_demographics.cache_warm(days=14, domain=None, keyword=None)
        insights_v3_platforms.cache_warm(days=14, domain=None, keyword=None)
        insights_v3_fatigue.cache_warm(days=14, domain=None, keyword=None)
        insights_v3_copy.cache_warm(days=14, domain=None, keyword=None)
        recommendations_top.cache_warm(n=20, min_score=70,
                                        only_shopify=True,
                                        exclude_platforms=True)
        recommendations_top.cache_warm(n=5, min_score=70,
                                        only_shopify=True,
                                        exclude_platforms=True)
        admin_scraper_throughput.cache_warm()

    def _warm_loop() -> None:
        # Cycle should be < TTL so an entry never expires before refresh
        # lands a new value. Heaviest endpoint TTL is 180s; cycle 90s gives
        # 2x safety margin.
        cycle_seconds = 90.0
        try:
            log.info("lifespan: prewarm starting")
            _warm_once()
            log.info("lifespan: prewarm done; entering refresh loop")
        except Exception as e:
            log.warning(f"lifespan: initial prewarm failed: {e}")
        while not _warm_stop.wait(cycle_seconds):
            try:
                _warm_once()
            except Exception as e:
                log.warning(f"lifespan: refresh cycle failed: {e}")

    _th.Thread(target=_warm_loop, daemon=True, name="cache-warm-loop").start()
    yield
    log.info("FastAPI shutdown")
    _warm_stop.set()
    try:
        db.close_pool()
    except Exception:
        pass


app = FastAPI(
    title="FB Ad Library Pipeline",
    version=__version__,
    description=f"Query brands / keywords / scans and submit raw ads for processing. {RELEASE_NAME}",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

register_selectpro_routes(app)


# 2026-05-05: per-request access log middleware. Logs method, path, status, ms.
# Errors get full traceback via the global Exception handler below; this only
# tracks normal request lifecycle so we can spot 5xx storms in pipeline.log.
@app.middleware("http")
async def _access_log_middleware(request: Request, call_next):
    import time as _time
    t0 = _time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        ms = int((_time.perf_counter() - t0) * 1000)
        log.exception(f"{request.method} {request.url.path} → 500 ({ms}ms) [middleware caught]")
        raise
    ms = int((_time.perf_counter() - t0) * 1000)
    if response.status_code >= 500:
        log.error(f"{request.method} {request.url.path} → {response.status_code} ({ms}ms)")
    elif response.status_code >= 400:
        log.warning(f"{request.method} {request.url.path} → {response.status_code} ({ms}ms)")
    else:
        log.info(f"{request.method} {request.url.path} → {response.status_code} ({ms}ms)")
    return response


# ── V9.0 统一错误响应 ────────────────────────────────────────────────────────
@app.exception_handler(HTTPException)
async def _http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "error": exc.__class__.__name__,
            "detail": exc.detail if exc.detail else DASH,
            "status": exc.status_code,
            "path": str(request.url.path),
        },
    )


@app.exception_handler(Exception)
async def _unhandled_exc_handler(request: Request, exc: Exception):
    log.exception(f"unhandled {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": exc.__class__.__name__,
            "detail": str(exc) or DASH,
            "status": 500,
            "path": str(request.url.path),
        },
    )


@app.get("/version")
def version():
    """V9.0 元数据端点 — 供前端/监控读取版本与构建信息."""
    return {
        "ok": True,
        "version": __version__,
        "release": RELEASE_NAME,
        "api": "v1",
        "now": datetime.utcnow().isoformat(timespec='seconds') + 'Z',
    }


def _utc_now() -> str:
    try:
        return datetime.utcnow().isoformat(timespec='seconds') + 'Z'
    except Exception:
        return datetime.utcnow().isoformat() + 'Z'


def _safe_scraper_status(warnings: list[str]) -> dict:
    try:
        from .scraper import get_status
        status = get_status()
        return status if isinstance(status, dict) else {"in_cooldown": False, "raw": status}
    except Exception as e:
        log.exception("health: scraper status degraded")
        warnings.append(f"scraper_status: {e.__class__.__name__}: {e}")
        return {
            "in_cooldown": False,
            "today_count": 0,
            "total_scraped": 0,
            "consecutive_challenges": 0,
            "error": str(e),
        }


def _safe_queue_pending(scan_file: Path, warnings: list[str]) -> int:
    queue_size = 0
    try:
        if not scan_file.exists():
            return 0
        with scan_file.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "已扫描" not in line:
                    queue_size += 1
        return queue_size
    except OSError as e:
        log.warning(f"health: queue read degraded: {e}")
        warnings.append(f"queue_file: {e.__class__.__name__}: {e}")
        return 0
    except Exception as e:
        log.exception("health: queue read failed")
        warnings.append(f"queue_file: {e.__class__.__name__}: {e}")
        return 0


def _safe_count(conn, sql: str, warnings: list[str], label: str) -> int:
    try:
        row = conn.execute(sql).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception as e:
        warnings.append(f"{label}: {e.__class__.__name__}: {e}")
        return 0


def _safe_close_pool(db, reason: str) -> None:
    try:
        db.close_pool()
        log.warning(f"health: closed read pool after {reason}")
    except Exception as e:
        log.warning(f"health: close_pool failed after {reason}: {e}")


@app.get("/health")
def health():
    from .paths import SCAN_FILE
    warnings: list[str] = []
    status = _safe_scraper_status(warnings)
    queue_size = _safe_queue_pending(SCAN_FILE, warnings)
    db = None
    db_file = ""
    scans_n = 0
    recs_n = 0
    fb_ready_n = 0
    pool_stats = {}
    try:
        db = get_persistence()
        db_file = str(db.db_file)
        with db._conn() as conn:
            scans_n = _safe_count(conn, "SELECT COUNT(*) FROM scans", warnings, "scans_count")
            recs_n = _safe_count(conn, "SELECT COUNT(*) FROM recommendations", warnings, "recommendations_count")
            fb_ready_n = _fb_ready_count(conn)
        try:
            pool = getattr(db, "_pool_inst", None)
            if pool is not None and hasattr(pool, "stats"):
                pool_stats = pool.stats()
        except Exception as e:
            warnings.append(f"pool_stats: {e.__class__.__name__}: {e}")
    except OSError as e:
        log.exception("health: db degraded by os error")
        warnings.append(f"db: {e.__class__.__name__}: {e}")
        if db is not None and getattr(e, "errno", None) == errno.EMFILE:
            _safe_close_pool(db, "EMFILE")
    except Exception as e:
        log.exception("health: db degraded")
        warnings.append(f"db: {e.__class__.__name__}: {e}")
    in_cooldown = bool(status.get("in_cooldown")) if isinstance(status, dict) else False
    payload = {
        "ok": True,
        "status": "cooldown" if in_cooldown else ("degraded" if warnings else "ok"),
        "data_count": scans_n,
        "db_file": db_file,
        "pipeline_version": __version__,
        "release": RELEASE_NAME,
        "scraper": status,
        "queue_pending": queue_size,
        "scans_total": scans_n,
        "recommendations_total": recs_n,
        "fb_ready_count": fb_ready_n,
        "pool": pool_stats,
        "warnings": warnings,
        "now": _utc_now(),
    }
    # V9.0: 所有 None/'' → '—' (前端不再出现 null/undefined)
    return scrub_output(payload)


@app.get("/ops/status")
def ops_status():
    return health()


@app.get("/api/ops/status")
def api_ops_status():
    return health()


@app.get("/brands", response_model=list[BrandSummary])
def list_brands(
    response: Response,
    min_appearance: int = Query(1, ge=1),
    limit: int = Query(200, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    days: int = Query(0, ge=0, le=365),
    date: Optional[str] = Query(None, pattern=r'^\d{4}-\d{2}-\d{2}$'),
    exclude_platforms: bool = Query(True),
):
    db = get_persistence()
    if exclude_platforms or date:
        # 后置过滤: 拉 2x limit 再切片, 比 10000 节省内存
        fetch = limit + offset + 1000
        raw = list(db.list_orbit_brands(min_appearance, fetch, days=days, offset=0))
        raw = _apply_date_filter(raw, date)
        raw = _sanitize_rows(raw, exclude_platforms=exclude_platforms)
        total = len(raw)
        rows = raw[offset:offset+limit]
    else:
        total = db.count_brands(min_appearance, days=days)
        rows = db.list_orbit_brands(min_appearance, limit, days=days, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return [BrandSummary(**b) for b in rows]


@app.get("/brands/search", response_model=list[BrandSummary])
def search_brands(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=500),
    exclude_platforms: bool = Query(True),
):
    rows = list(get_persistence().search_brands(q, limit * 5 if exclude_platforms else limit))
    rows = _sanitize_rows(rows, exclude_platforms=exclude_platforms)[:limit]
    return [BrandSummary(**b) for b in rows]


@app.get("/brands/{domain}", response_model=BrandResponse)
def get_brand(domain: str, exclude_platforms: bool = Query(True)):
    if exclude_platforms and _is_excluded_domain(domain):
        raise HTTPException(404, f"excluded platform domain: {domain}")
    brand = get_persistence().get_brand(domain)
    if not brand:
        raise HTTPException(404, f"brand not found: {domain}")
    return BrandResponse(
        domain=brand['domain'],
        canonical_name=brand['canonical_name'] or DASH,
        orbit_score=brand.get('orbit_score') or 0,
        appearance_count=brand.get('appearance_count') or 0,
        highest_likes=brand.get('highest_likes') or 0,
        total_ads_across_scans=brand.get('total_ads_across_scans') or 0,
        keywords=brand.get('keywords') or [],
        page_names=brand.get('page_names') or [],
    )


@app.get("/orbits", response_model=list[BrandSummary])
def list_orbits(min_appearance: int = Query(3, ge=1), limit: int = Query(50, ge=1, le=500)):
    return [BrandSummary(**b) for b in get_persistence().list_orbit_brands(min_appearance, limit)]


@app.get("/keywords", response_model=list[LearnedKeyword])
def list_keywords(
    response: Response,
    type: Optional[str] = Query(None, description="pain/scene/product/brand"),
    q: Optional[str] = None,
    limit: int = Query(200, ge=1, le=20000),
    offset: int = Query(0, ge=0),
    days: int = Query(0, ge=0, le=365),
    date: Optional[str] = Query(None, pattern=r'^\d{4}-\d{2}-\d{2}$'),
    exclude_platforms: bool = Query(True),
):
    db = get_persistence()
    if exclude_platforms or date:
        raw = list(db.search_learned(type, q, 20000, days=days, offset=0))
        raw = _apply_date_filter(raw, date)
        raw = _sanitize_rows(raw, exclude_platforms=exclude_platforms)
        total = len(raw)
        rows = raw[offset:offset+limit]
    else:
        total = db.count_learned(type, q, days=days)
        rows = db.search_learned(type, q, limit, days=days, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return [LearnedKeyword(**r) for r in rows]


@app.get("/scans", response_model=list[ScanSummary])
def recent_scans(
    response: Response,
    limit: int = Query(200, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    days: int = Query(0, ge=0, le=365),
    date: Optional[str] = Query(None, pattern=r'^\d{4}-\d{2}-\d{2}$'),
    exclude_platforms: bool = Query(True),
):
    db = get_persistence()
    if exclude_platforms or date:
        raw = list(db.recent_scans(10000, days=days, offset=0))
        raw = _apply_date_filter(raw, date)
        raw = _sanitize_rows(raw, exclude_platforms=exclude_platforms)
        total = len(raw)
        rows = raw[offset:offset+limit]
    else:
        total = db.count_scans(days=days)
        rows = db.recent_scans(limit, days=days, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return [ScanSummary(**scrub_output(r) if isinstance(r, dict) else r) for r in rows]


@app.get("/scans/{keyword}")
def get_scan(keyword: str):
    """Returns the latest _corrected.json for a keyword."""
    safe = keyword.replace(' ', '-').replace('+', 'plus')
    matches = sorted(RESULTS_DIR.glob(f'*_{safe}_corrected.json'))
    if not matches:
        raise HTTPException(404, f"no scan found for keyword: {keyword}")
    with open(matches[-1], encoding='utf-8') as f:
        data = json.load(f)
    return scrub_output(data)


@app.post("/scans")
def submit_scan(req: RawScanRequest):
    """Process raw ads JSON and return the corrected result."""
    from .scan_result import save_scan_result
    try:
        filepath, competitors, side = save_scan_result(
            req.keyword, req.raw_ads, source_raw=req.source_raw, apply_side_effects=True
        )
    except Exception as e:
        log.exception("submit_scan failed")
        raise HTTPException(500, f"processing failed: {e}")
    with open(filepath, encoding='utf-8') as f:
        result = json.load(f)
    return {"corrected_file": filepath, "result": result}


@app.get("/queue")
def list_queue():
    from .scan_instructions import scan_queue_loader
    return {"items": scan_queue_loader()}


@app.get("/recommendations")
def recommendations(
    response: Response,
    limit: int = Query(200, ge=1, le=20000),
    offset: int = Query(0, ge=0),
    min_score: int = Query(60, ge=0, le=120),
    only_shopify: bool = Query(False),
    tier: str = Query("all", pattern="^(all|testing|validated|proven)$"),
    days: int = Query(0, ge=0, le=365),
    date: Optional[str] = Query(None, pattern=r'^\d{4}-\d{2}-\d{2}$'),
    exclude_platforms: bool = Query(True),
):
    from .recommendations import count_recommendations
    tier_val = None if tier == "all" else tier
    excl = tuple(_EXCLUDE_SET) if exclude_platforms else None
    needs_post_filter = bool(date)
    fetch_limit = max(limit, 50000) if needs_post_filter else limit
    rows = list_recommendations(limit=fetch_limit, min_score=min_score,
                                only_shopify=only_shopify, days=days,
                                offset=0 if needs_post_filter else offset,
                                tier=tier_val, exclude_domains=excl)
    rows = _apply_date_filter(rows, date)
    rows = _sanitize_rows(rows, exclude_platforms=exclude_platforms)
    if needs_post_filter:
        total = len(rows)
        rows = rows[offset:offset+limit]
    else:
        total = count_recommendations(min_score=min_score, only_shopify=only_shopify,
                                      days=days, tier=tier_val)
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return rows


@app.get("/recommendations.csv", response_class=StreamingResponse)
def recommendations_csv(
    min_score: int = Query(60, ge=0, le=100),
    only_shopify: bool = Query(False),
    exclude_platforms: bool = Query(True),
    tier: Optional[str] = Query(None, pattern="^(testing|validated|proven)$"),
):
    """流式 CSV 导出 — 逐批拉取, 内存恒定."""
    import csv, io

    def _stream():
        batch = 5000
        offset = 0
        header_done = False
        while True:
            rows = list_recommendations(limit=batch, min_score=min_score,
                                        only_shopify=only_shopify,
                                        offset=offset, tier=tier)
            if not rows:
                break
            rows = _sanitize_rows(rows, exclude_platforms=exclude_platforms)
            if not rows:
                offset += batch
                continue
            if not header_done:
                fields = list(rows[0].keys())
                yield ','.join(fields) + '\n'
                header_done = True
            for r in rows:
                vals = [str(r.get(k, DASH) or DASH).replace('"', '""') for k in fields]
                yield ','.join(f'"{v}"' if ',' in v or '"' in v or '\n' in v else v
                               for v in vals) + '\n'
            offset += batch

    return StreamingResponse(
        _stream(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="recommendations.csv"'},
    )


@app.get("/recommendations.xlsx")
def recommendations_xlsx(
    min_score: int = Query(60, ge=0, le=100),
    only_shopify: bool = Query(False),
    exclude_platforms: bool = Query(True),
    tier: Optional[str] = Query(None, pattern="^(testing|validated|proven)$"),
):
    """Excel 导出 (write_only 流式, 逐批拉取, 支持 10万+ 行)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.cell import WriteOnlyCell
    import io

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("推荐产品")

    # 中文表头 + 列宽
    headers = [
        ("domain", "域名", 22),
        ("brand_name", "品牌", 22),
        ("landing_url", "独立站", 40),
        ("product_slug", "产品 slug", 18),
        ("keyword", "来源关键词", 22),
        ("score", "分数", 8),
        ("ads_count", "广告数", 8),
        ("ad_age_days", "广告龄(天)", 10),
        ("is_active", "活跃", 6),
        ("is_shopify_shape", "Shopify", 8),
        ("orbit_score", "轨道分", 8),
        ("price_main", "价格", 10),
        ("discount_pct", "折扣%", 8),
        ("review_count", "评论", 8),
        ("star_rating", "星级", 6),
        ("signal_score", "商信号", 8),
        ("top_pain", "痛点词", 25),
        ("top_scene", "场景词", 25),
        ("top_product", "产品词", 20),
        ("spend_estimate", "花费估算", 12),
        ("reasons", "打分依据", 40),
        ("scanned_at", "扫描时间", 18),
        ("first_seen_at", "首次入库", 18),
        ("updated_at", "更新时间", 18),
    ]

    fill = PatternFill(start_color='2F5496', end_color='2F5496', fill_type='solid')
    font = Font(bold=True, color='FFFFFF')
    header_row = []
    for k, label, w in headers:
        c = WriteOnlyCell(ws, value=label)
        c.fill = fill
        c.font = font
        header_row.append(c)
    ws.append(header_row)
    for idx, (_, _, w) in enumerate(headers, 1):
        from openpyxl.utils import get_column_letter
        ws.column_dimensions[get_column_letter(idx)].width = w

    # 逐批拉取写行, 内存恒定
    batch = 5000
    offset = 0
    while True:
        batch_rows = list_recommendations(limit=batch, min_score=min_score,
                                          only_shopify=only_shopify,
                                          offset=offset, tier=tier)
        if not batch_rows:
            break
        batch_rows = _sanitize_rows(batch_rows, exclude_platforms=exclude_platforms)
        for r in batch_rows:
            ws.append([r.get(k) if r.get(k) is not None else DASH for k, _, _ in headers])
        offset += batch

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"recommendations-{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/recommendations/latest")
def recommendations_latest(
    hours: int = Query(48, ge=1, le=720),
    limit: int = Query(8, ge=1, le=100),
    exclude_platforms: bool = Query(True),
):
    """最近 N 小时首次入库的推荐 (按 first_seen_at DESC)."""
    db = get_persistence()
    with db._conn() as conn:
        try:
            rows = [dict(r) for r in conn.execute(
                """SELECT * FROM recommendations
                   WHERE first_seen_at >= datetime('now', '-' || ? || ' hours')
                   ORDER BY first_seen_at DESC LIMIT ?""",
                (hours, limit * 5 if exclude_platforms else limit),
            )]
        except Exception:
            rows = []
    return _sanitize_rows(rows, exclude_platforms=exclude_platforms)[:limit]


@app.post("/recommendations/rebuild")
def rebuild_recs():
    n = rebuild_recommendations()
    return {"count": n}


# ── Ad Archive endpoints ─────────────────────────────────────────────

@app.get("/ads")
def list_ads_endpoint(
    response: Response,
    limit: int = Query(200, ge=1, le=20000),
    offset: int = Query(0, ge=0),
    keyword: Optional[str] = Query(None, min_length=1),
    domain: Optional[str] = Query(None, min_length=1),
    days: int = Query(0, ge=0, le=365),
    is_active: Optional[int] = Query(None, ge=0, le=1),
    exclude_platforms: bool = Query(True),
):
    """分页查询广告归档, 支持 keyword/domain/days/active 过滤."""
    db = get_persistence()
    total = db.count_ads(keyword=keyword, domain=domain, days=days, is_active=is_active)
    rows = db.list_ads(keyword=keyword, domain=domain, days=days,
                       is_active=is_active, limit=limit, offset=offset)
    rows = _sanitize_rows(rows, exclude_platforms=exclude_platforms)
    if exclude_platforms:
        total = len(rows) if total > len(rows) else total
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return rows


@app.get("/ads/search")
def search_ads_endpoint(
    response: Response,
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(50, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    days: int = Query(0, ge=0, le=365),
    exclude_platforms: bool = Query(True),
):
    """FTS5 全文搜索广告标题/文案. q 为搜索词."""
    db = get_persistence()
    rows = db.search_ads_fts(query=q, limit=limit, offset=offset, days=days)
    rows = _sanitize_rows(rows, exclude_platforms=exclude_platforms)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return rows


@app.post("/admin/rebuild-fts")
def rebuild_fts():
    """重建 FTS5 索引 (批量导入后调用)."""
    db = get_persistence()
    n = db.rebuild_fts()
    return {"indexed": n}


@app.post("/admin/run-strategies")
def admin_run_strategies():
    """一键跑三种策略选品 (爆款反查 + 痛点空白 + 季节提前)."""
    from .strategies import run_all_strategies
    r = run_all_strategies()
    return r


@app.get("/strategies/preview")
def strategies_preview():
    """返回三种策略当前 scan_queue 文件的内容摘要."""
    from .strategies import LOOKALIKE_FILE, PAINGAP_FILE, SEASONAL_FILE
    out = {}
    for key, path in (("lookalike", LOOKALIKE_FILE),
                      ("paingap", PAINGAP_FILE),
                      ("seasonal", SEASONAL_FILE)):
        if not path.exists():
            out[key] = {"count": 0, "items": [], "updated_at": None}
            continue
        lines = path.read_text(encoding='utf-8').splitlines()
        items = []
        for ln in lines:
            ln = ln.strip()
            if not ln or ln.startswith('#'):
                continue
            parts = ln.split('|', 1)
            items.append({
                "keyword": parts[0].strip(),
                "reason": (parts[1].strip() if len(parts) > 1 else ""),
            })
        from datetime import datetime as _dt
        out[key] = {
            "count": len(items),
            "items": items[:20],
            "updated_at": _dt.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds'),
        }
    return out


@app.post("/admin/scrape")
def admin_scrape_now(limit: int = 3):
    """立即启动一次采集 (后台线程)."""
    from threading import Thread
    from .scraper import scrape_queue
    def _run():
        try:
            scrape_queue(limit=limit, headless=True)
        except Exception:
            log.exception("admin scrape failed")
    Thread(target=_run, daemon=True).start()
    return {"started": True, "limit": limit}


@app.post("/admin/scrape-keyword")
def admin_scrape_keyword(
    keyword: str = Query(..., min_length=2, max_length=120),
    country: Optional[str] = Query(None),
    headless: bool = Query(True),
):
    """v9.1: 单条关键词按需抓取 — V2 dashboard 的 🔥 立即抓取 按钮调用这个端点.
    后台线程执行, 不阻塞 API. 抓完后续走标准 raw_processor → ad_archive 链路.
    """
    from threading import Thread
    from .scraper import scrape_keyword
    kw = keyword.strip()
    if not kw or len(kw) < 2:
        raise HTTPException(status_code=400, detail="keyword too short")
    if any(c in kw for c in '\n\r\t'):
        raise HTTPException(status_code=400, detail="keyword contains illegal chars")

    def _run():
        try:
            log.info(f"on-demand scrape requested via API: {kw} (country={country})")
            res = scrape_keyword(kw, headless=headless, country=country)
            if res == 'CHALLENGE':
                log.warning(f"on-demand {kw}: CHALLENGE")
            elif res is None:
                log.warning(f"on-demand {kw}: returned None")
            else:
                log.info(f"on-demand {kw}: saved {res}")
        except Exception:
            log.exception(f"on-demand scrape failed: {kw}")

    Thread(target=_run, daemon=True).start()
    return {"started": True, "keyword": kw, "country": country, "headless": headless,
            "note": "后台执行 60-120s, 完成后通过 raw_processor 流入 ad_archive"}


@app.post("/admin/ingest-keywords")
def admin_ingest():
    from .scraper import ingest_rabbit_hole_into_queue
    n = ingest_rabbit_hole_into_queue()
    return {"ingested": n}


def _parse_keyword_payload(payload: dict) -> list[str]:
    raw_items = payload.get('keywords') or payload.get('keyword') or payload.get('q') or payload.get('text') or []
    if isinstance(raw_items, str):
        raw_items = re.split(r'[\n\r,;\t|]+', raw_items)
    if not isinstance(raw_items, list):
        raise HTTPException(400, "keywords/text 必须是字符串或数组")
    out = []
    seen = set()
    for item in raw_items:
        kw = item.get('keyword') if isinstance(item, dict) else item
        kw = str(kw or '').strip().lower().strip('- ')
        if 2 <= len(kw) <= 120 and kw not in seen:
            seen.add(kw)
            out.append(kw)
    if not out:
        raise HTTPException(400, "没有有效关键词")
    return out


V3_AUTO_DEFAULT_SEEDS = [
    'vagus nerve', 'cortisol reset', 'red light therapy', 'lymph drainage',
    'somatic therapy', 'glp-1 alternative', 'creatine for women',
    'perimenopause relief', 'non toxic skincare', 'grounding mat',
]


def _parse_seed_payload(payload: dict) -> list[str]:
    raw_items = payload.get('seeds') or payload.get('seed') or payload.get('q') or payload.get('text') or V3_AUTO_DEFAULT_SEEDS
    if isinstance(raw_items, str):
        raw_items = re.split(r'[\n\r,;\t|]+', raw_items)
    if not isinstance(raw_items, list):
        raise HTTPException(400, "seeds/text 必须是字符串或数组")
    out = []
    seen = set()
    for item in raw_items:
        seed = item.get('keyword') if isinstance(item, dict) else item
        seed = str(seed or '').strip().lower().strip('- ')
        if 2 <= len(seed) <= 120 and seed not in seen:
            seen.add(seed)
            out.append(seed)
    return out or V3_AUTO_DEFAULT_SEEDS


def _bool_payload(payload: dict, key: str, default: bool) -> bool:
    v = payload.get(key, default)
    if isinstance(v, str):
        return v.strip().lower() not in ('0', 'false', 'no', 'off', '')
    return bool(v)


@app.post("/v3")
def v3_scrape_keywords(payload: dict):
    keywords = _parse_keyword_payload(payload)
    limit = max(1, min(int(payload.get('limit') or len(keywords)), 100))
    keywords = keywords[:limit]
    from .scraper import scrape_keywords_v3
    result = scrape_keywords_v3(
        keywords,
        max_scrolls=max(1, min(int(payload.get('max_scrolls') or 5), 20)),
        headless=_bool_payload(payload, 'headless', True),
        country=(payload.get('country') or 'US').strip().upper(),
        fast=_bool_payload(payload, 'fast', True),
        delay_sec=max(0.0, min(float(payload.get('delay_sec') or 0.15), 10.0)),
        refresh_every=max(0, min(int(payload.get('refresh_every') or 10), 50)),
        max_pages=int(payload.get('max_pages') or 0) or None,
    )
    if _bool_payload(payload, 'process', True) and result.get('saved'):
        try:
            from .raw_processor import process_raw_results
            from .recommendations import rebuild_recommendations
            result['processed_files'] = process_raw_results(force=False, workers=2)
            rebuild_recommendations()
        except Exception as e:
            log.exception("v3 processing failed")
            result['process_error'] = str(e)
    return scrub_output(result)


@app.get("/v3/run")
def v3_scrape_keywords_get(q: str = Query(..., min_length=2), limit: int = Query(20, ge=1, le=100), process: bool = Query(True)):
    return v3_scrape_keywords({'text': q, 'limit': limit, 'process': process})


@app.post("/v3/auto")
def v3_auto_run(payload: dict):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .keyword_evolution import fetch_external_suggestions, ingest_suggestions
    from .scraper import scrape_keywords_v3

    seeds = _parse_seed_payload(payload)
    alphabet = _bool_payload(payload, 'alphabet', True)
    top_n = max(1, min(int(payload.get('top_n') or payload.get('limit') or 25), 100))
    suggestion_runs = []
    errors = []
    with ThreadPoolExecutor(max_workers=min(8, len(seeds))) as ex:
        future_to_seed = {ex.submit(fetch_external_suggestions, seed, alphabet): seed for seed in seeds}
        for fut in as_completed(future_to_seed):
            seed = future_to_seed[fut]
            try:
                data = fut.result()
                if data.get('error'):
                    errors.append({'seed': seed, 'error': data.get('error')})
                else:
                    suggestion_runs.append(data)
            except Exception as e:
                log.exception(f"v3 auto suggestions failed: {seed}")
                errors.append({'seed': seed, 'error': str(e)})

    merged = {}
    for run in suggestion_runs:
        for row in run.get('scored') or []:
            kw = row.get('keyword', '').strip().lower()
            if not kw:
                continue
            cur = merged.get(kw)
            sources = sorted(set((cur or {}).get('sources', []) + row.get('sources', [])))
            score = max((cur or {}).get('trend_score', 0), row.get('trend_score', 0))
            merged[kw] = {'keyword': kw, 'trend_score': score, 'sources': sources, 'kw_type': row.get('kw_type', 'product'), 'subtype': row.get('subtype', 'generic')}

    ranked = sorted(merged.values(), key=lambda r: (-int(r.get('trend_score') or 0), -len(r.get('sources') or []), len(r.get('keyword') or ''), r.get('keyword') or ''))
    selected_rows = ranked[:top_n]
    selected_keywords = [r['keyword'] for r in selected_rows]

    ingested = ingest_suggestions(selected_keywords, source='v3-auto-2026') if _bool_payload(payload, 'ingest', True) and selected_keywords else 0
    scrape_result = {'ok': True, 'requested': 0, 'parsed': 0, 'saved': 0, 'results': []}
    if selected_keywords:
        scrape_result = scrape_keywords_v3(
            selected_keywords,
            max_scrolls=max(1, min(int(payload.get('max_scrolls') or 5), 20)),
            headless=_bool_payload(payload, 'headless', True),
            country=(payload.get('country') or 'US').strip().upper(),
            fast=_bool_payload(payload, 'fast', True),
            delay_sec=max(0.0, min(float(payload.get('delay_sec') or 0.15), 10.0)),
            refresh_every=max(0, min(int(payload.get('refresh_every') or 10), 50)),
            max_pages=int(payload.get('max_pages') or 0) or None,
        )
    if _bool_payload(payload, 'process', True) and scrape_result.get('saved'):
        try:
            from .raw_processor import process_raw_results
            from .recommendations import rebuild_recommendations
            scrape_result['processed_files'] = process_raw_results(force=False, workers=2)
            rebuild_recommendations()
        except Exception as e:
            log.exception("v3 auto processing failed")
            scrape_result['process_error'] = str(e)

    return scrub_output({'ok': True, 'mode': 'v3-auto-2026', 'seeds': seeds, 'suggestion_errors': errors, 'generated_count': len(merged), 'selected_count': len(selected_keywords), 'top_keywords': selected_rows, 'ingested': ingested, 'scrape': scrape_result})


@app.get("/v3/auto")
def v3_auto_run_get(q: str = Query('', description="种子关键词；为空则使用内置 2026 趋势种子"), top_n: int = Query(25, ge=1, le=100), process: bool = Query(True)):
    return v3_auto_run({'text': q, 'top_n': top_n, 'process': process})


@app.post("/admin/import-keywords")
def admin_import_keywords(payload: dict):
    """
    📥 用户手动粘贴关键词批量入队 + 可选入库.
    Body: {
        "text": "原始粘贴文本 (支持换行/逗号/制表符)",
        "to_queue": true,
        "to_learned": true,
        "category": "可选, 批注来源"
    }
    """
    raw = (payload.get('text') or '').strip()
    if not raw:
        return JSONResponse(status_code=400, content={"error": "text 为空"})
    to_queue = bool(payload.get('to_queue', True))
    to_learned = bool(payload.get('to_learned', True))
    category = (payload.get('category') or 'manual-import').strip()

    import re
    tokens = re.split(r'[\n\r,;\t|]+', raw)
    kws = []
    seen = set()
    for t in tokens:
        t = t.strip().lower().strip('- ')
        if 3 <= len(t) <= 80 and t not in seen:
            seen.add(t)
            kws.append(t)
    if not kws:
        return JSONResponse(status_code=400, content={"error": "解析后无有效关键词"})

    added_queue = 0
    queue_file = None
    if to_queue:
        queue_file = RESULTS_DIR / f"scan_queue_manual_{datetime.now().strftime('%Y-%m-%d_%H%M')}.txt"
        with open(queue_file, 'w', encoding='utf-8') as f:
            f.write(f"# === 手动导入 {datetime.now().isoformat(timespec='seconds')} ({category}) ===\n")
            f.write(f"# 关键词: {len(kws)}\n\n")
            for kw in kws:
                f.write(f"{kw}|待翻译|manual|{category}\n")
                added_queue += 1

    added_learned = 0
    if to_learned:
        from .keyword_evolution import ingest_suggestions
        added_learned = ingest_suggestions(kws, source=f'manual:{category}')

    return {
        "parsed": len(kws),
        "queued": added_queue,
        "learned_added": added_learned,
        "queue_file": str(queue_file) if queue_file else None,
        "sample": kws[:10],
    }


@app.post("/admin/evolve-keywords")
def admin_evolve_keywords(dry_run: bool = Query(False)):
    """🧬 关键词进化: harvest + 5 层兔子洞扩展 + retire + cluster. 返回结构化报告."""
    from .keyword_evolution import evolve
    try:
        report = evolve(dry_run=dry_run, write_queue=not dry_run)
        return report
    except Exception as e:
        log.exception("evolve-keywords 异常")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/admin/expand-seed")
def admin_expand_seed(seed: str = Query(..., min_length=2, max_length=80)):
    """🕳 兔子洞单词扩展预览: 给一个种子词返回 L2-L5 四层变体."""
    from .keyword_evolution import rabbit_hole_expand
    try:
        return {
            'seed': seed,
            'layers': rabbit_hole_expand(seed),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/admin/fetch-suggestions")
def admin_fetch_suggestions(
    seed: str = Query(..., min_length=2, max_length=80),
    alphabet: bool = Query(False, description="A-Z 长尾爆破, 52 次请求约 15 秒"),
):
    """🔍 从 Amazon 下拉 + Google autocomplete 抓取真实搜索长尾词."""
    from .keyword_evolution import fetch_external_suggestions
    try:
        return fetch_external_suggestions(seed, alphabet=alphabet)
    except Exception as e:
        log.exception("fetch-suggestions 异常")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/admin/ingest-suggestions")
def admin_ingest_suggestions(payload: dict):
    """接收前端提交的关键词列表, 入库 learned_keywords."""
    from .keyword_evolution import ingest_suggestions
    kws = payload.get('keywords') or []
    source = payload.get('source') or 'amazon+google'
    try:
        added = ingest_suggestions(kws, source=source)
        return {"added": added, "requested": len(kws)}
    except Exception as e:
        log.exception("ingest-suggestions 异常")
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/admin/rebuild-intel")
def admin_rebuild_intel():
    from threading import Thread
    from .intelligence import update_brand_intelligence
    from .rabbit_hole import generate_rabbit_hole_queue
    def _run():
        try:
            update_brand_intelligence()
            generate_rabbit_hole_queue()
            rebuild_recommendations()
        except Exception:
            log.exception("admin rebuild failed")
    Thread(target=_run, daemon=True).start()
    return {"started": True}


@app.get("/admin/daemon/status")
def daemon_status():
    import subprocess
    try:
        out = subprocess.check_output(['launchctl', 'list'], text=True, timeout=2)
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) >= 3 and parts[2] == 'com.doi.fbpipeline.daemon':
                pid_raw, status_raw, label = parts
                running = pid_raw.isdigit()
                return {
                    "running": running,
                    "loaded": True,
                    "pid": int(pid_raw) if running else None,
                    "status": status_raw,
                    "label": label,
                }
    except subprocess.TimeoutExpired:
        return {"running": False, "error": "launchctl timeout"}
    except Exception as e:
        return {"running": False, "error": str(e)}
    return {"running": False, "loaded": False}


@app.post("/admin/daemon/start")
def daemon_start():
    import subprocess
    from pathlib import Path
    plist = Path.home() / "Library/LaunchAgents/com.doi.fbpipeline.daemon.plist"
    try:
        subprocess.run(['launchctl', 'load', str(plist)], timeout=10, check=False)
        return {"ok": True, "action": "started"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/admin/daemon/stop")
def daemon_stop():
    import subprocess
    from pathlib import Path
    plist = Path.home() / "Library/LaunchAgents/com.doi.fbpipeline.daemon.plist"
    try:
        subprocess.run(['launchctl', 'unload', str(plist)], timeout=10, check=False)
        return {"ok": True, "action": "stopped"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/brands/{domain}/countries")
def brand_countries(domain: str):
    """品牌的国家覆盖 + 未覆盖机会区.

    核心改进: 不只看"我在哪国扫的", 而是看每条广告的 target_locations 字段
    (FB Ad Library 官方数据: 广告实际投放的国家), 这才是品牌真实投放版图.
    """
    import json as _json
    from .paths import RESULTS_DIR
    from .categories import get_all_regions, get_region_metadata

    # ISO 国家名/代码规范化
    COUNTRY_MAP = {
        'united states': 'US', 'usa': 'US', 'us': 'US',
        'united kingdom': 'GB', 'uk': 'GB', 'gb': 'GB', 'great britain': 'GB',
        'canada': 'CA', 'ca': 'CA',
        'australia': 'AU', 'au': 'AU',
        'new zealand': 'NZ', 'nz': 'NZ',
        'germany': 'DE', 'de': 'DE', 'deutschland': 'DE',
        'france': 'FR', 'fr': 'FR',
        'netherlands': 'NL', 'nl': 'NL', 'holland': 'NL',
        'switzerland': 'CH', 'ch': 'CH',
        'sweden': 'SE', 'se': 'SE',
        'norway': 'NO', 'no': 'NO',
        'denmark': 'DK', 'dk': 'DK',
        'ireland': 'IE', 'ie': 'IE',
        'singapore': 'SG', 'sg': 'SG',
        'japan': 'JP', 'jp': 'JP',
        'israel': 'IL', 'il': 'IL',
        'united arab emirates': 'AE', 'uae': 'AE', 'ae': 'AE',
    }

    def _normalize(c: str) -> str:
        if not c:
            return ''
        c = c.strip().strip('"').lower()
        if c in COUNTRY_MAP:
            return COUNTRY_MAP[c]
        if len(c) == 2:
            return c.upper()
        return ''

    covered: set[str] = set()
    evidence_count: dict[str, int] = {}
    sample_ads: dict[str, list[str]] = {}

    for jf in RESULTS_DIR.glob('*_corrected.json'):
        if '汇总' in jf.name:
            continue
        try:
            data = _json.loads(jf.read_text(encoding='utf-8'))
        except Exception:
            continue

        # scan-level country (fallback)
        scan_country = _normalize(data.get('country') or 'US')

        for c in data.get('top_competitors', []):
            if not isinstance(c, dict):
                continue
            brand = c.get('brand', '') or ''
            landing = c.get('landing', '') or ''
            matched = (landing == domain) or (domain.lower() in brand.lower()) or (brand.lower() in domain.lower())
            if not matched:
                continue

            # 优先读 target_locations (FB 真实投放国)
            locs = c.get('target_profile', {}).get('locations', []) or []
            if locs:
                for loc in locs:
                    code = _normalize(str(loc))
                    if code:
                        covered.add(code)
                        evidence_count[code] = evidence_count.get(code, 0) + 1
            elif scan_country:
                covered.add(scan_country)
                evidence_count[scan_country] = evidence_count.get(scan_country, 0) + 1

            # 记录一个广告样本 URL 作为证据
            snapshots = c.get('ad_snapshot_urls', [])
            if snapshots:
                for cc in covered:
                    sample_ads.setdefault(cc, [])
                    if len(sample_ads[cc]) < 2:
                        sample_ads[cc].append(snapshots[0])

    all_regions = set(get_all_regions())
    missing = all_regions - covered
    meta = get_region_metadata()

    def _gdp(code):
        return (meta.get(code, {}) or {}).get('gdp_per_capita', 0)

    missing_sorted = sorted(missing, key=_gdp, reverse=True)

    def _enrich(codes):
        out = []
        for c in codes:
            m = meta.get(c, {}) or {}
            out.append({
                'code': c,
                'flag': m.get('flag', '🏳'),
                'name': m.get('name', c),
                'gdp_per_capita': m.get('gdp_per_capita', 0),
                'fb_penetration': m.get('fb_penetration', 0),
                'ecom_maturity': m.get('ecom_maturity', '未知'),
                'notes': m.get('notes', ''),
                'evidence_count': evidence_count.get(c, 0),
                'sample_ads': sample_ads.get(c, []),
            })
        return out

    return {
        'domain': domain,
        'covered': _enrich(sorted(covered, key=_gdp, reverse=True)),
        'missing': _enrich(missing_sorted),
        'total_evidence': sum(evidence_count.values()),
        'opportunity': bool(missing and covered),
    }


@app.get("/recommendations/top")
@ttl_cache(ttl_seconds=120)
def recommendations_top(
    n: int = Query(5, ge=1, le=500),
    min_score: int = Query(70, ge=0, le=120),
    only_shopify: bool = Query(True),
    exclude_platforms: bool = Query(True),
):
    """TOP N 精选: 综合分 + 专家分 + 7天动能, 带 sparkline."""
    from .trends import top_recommendations
    rows = top_recommendations(n=n * 3 if exclude_platforms else n, min_score=min_score, only_shopify=only_shopify)
    return _sanitize_rows(rows, exclude_platforms=exclude_platforms)[:n]


@app.get("/recommendations/expert")
def recommendations_with_insights(
    response: Response,
    limit: int = Query(200, ge=1, le=20000),
    offset: int = Query(0, ge=0),
    min_score: int = Query(45, ge=0, le=120),
    only_shopify: bool = Query(False),
    tier: str = Query("all", pattern="^(all|testing|validated|proven)$"),
    signals_only: bool = Query(False),
    days: int = Query(0, ge=0, le=365),
    strategy_tag: Optional[str] = Query(None, pattern="^(lookalike|paingap|seasonal)$"),
    date: Optional[str] = Query(None, pattern=r'^\d{4}-\d{2}-\d{2}$'),
    exclude_platforms: bool = Query(True),
):
    """带专家洞见 + 战术打法的推荐列表. 分页 + days/date/平台 过滤."""
    from .insights import compute_insights
    from .playbook import analyze as playbook_analyze
    from .recommendations import count_recommendations

    tier_val = None if tier == "all" else tier
    excl = tuple(_EXCLUDE_SET) if exclude_platforms else None
    needs_post_filter = bool(signals_only or date)
    fetch_limit = max(limit + offset, 50000) if needs_post_filter else limit
    base_rows = list_recommendations(limit=fetch_limit, min_score=min_score,
                                     only_shopify=only_shopify, days=days,
                                     strategy_tag=strategy_tag, tier=tier_val,
                                     offset=0 if needs_post_filter else offset,
                                     exclude_domains=excl)
    if signals_only:
        base_rows = [r for r in base_rows if (r.get('signal_score') or 0) > 0]
    base_rows = _apply_date_filter(base_rows, date)
    base_rows = _sanitize_rows(base_rows, exclude_platforms=exclude_platforms)
    if needs_post_filter:
        total = len(base_rows)
        rows = base_rows[offset:offset+limit]
    else:
        total = count_recommendations(min_score=min_score, only_shopify=only_shopify,
                                     days=days, strategy_tag=strategy_tag, tier=tier_val)
        rows = base_rows

    for r in rows:
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
            from .classifier import classify
            r.update(classify(r))
        except Exception:
            pass
        # FB fitness 展示
        r['fb_fitness'] = r.get('fb_fitness', 0) or 0
        try:
            r['fb_breakdown'] = json.loads(r.get('fb_breakdown') or '{}')
        except Exception:
            r['fb_breakdown'] = {}
        try:
            r['fb_tags'] = json.loads(r.get('fb_tags') or '[]')
        except Exception:
            r['fb_tags'] = []

    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return rows


@app.get("/recommendations/fb-ready")
def recommendations_fb_ready(
    response: Response,
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    min_fb_fitness: int = Query(60, ge=0, le=100),
    exclude_platforms: bool = Query(True),
):
    """FB Ads 可投筛选 — fb_fitness >= threshold 的候选."""
    db = get_persistence()
    with db._conn() as conn:
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM recommendations WHERE fb_fitness >= ?",
                (min_fb_fitness,),
            ).fetchone()[0]
        except Exception:
            total = 0
        try:
            rows = [dict(r) for r in conn.execute(
                """SELECT * FROM recommendations
                   WHERE fb_fitness >= ?
                   ORDER BY fb_fitness DESC, score DESC
                   LIMIT ? OFFSET ?""",
                (min_fb_fitness, limit, offset),
            )]
        except Exception:
            rows = []

    from .fb_fitness import fb_fitness_label
    for r in rows:
        fb = r.get('fb_fitness', 0) or 0
        r['fb_label'] = fb_fitness_label(fb)
        try:
            r['fb_breakdown'] = json.loads(r.get('fb_breakdown') or '{}')
        except Exception:
            r['fb_breakdown'] = {}
        try:
            r['fb_tags'] = json.loads(r.get('fb_tags') or '[]')
        except Exception:
            r['fb_tags'] = []

    rows = _sanitize_rows(rows, exclude_platforms=exclude_platforms)
    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return rows


@app.get("/recommendations/rabbit2026")
def recommendations_rabbit2026(
    response: Response,
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    min_score: int = Query(45, ge=0, le=120),
    days: int = Query(0, ge=0, le=365),
    date: Optional[str] = Query(None, pattern=r'^\d{4}-\d{2}-\d{2}$'),
    exclude_platforms: bool = Query(True),
):
    """🐰 兔子洞升级 2026: 纯算法 rabbit-hole 评分 (sibling 聚集 + 新鲜度 + 价格带 + 活跃度)."""
    # 1. 候选池 (放宽过滤, 以便兔子洞推演; 取更大池子保证多样性)
    rows = list_recommendations(limit=2000, min_score=min_score, days=days, only_shopify=False)
    rows = _apply_date_filter(rows, date)
    rows = _sanitize_rows(rows, exclude_platforms=exclude_platforms)

    # 2. 域名维度聚合: 统计每个 TLD base 下的 sibling 数 (同 pain / scene)
    from collections import defaultdict
    scene_to_brands = defaultdict(set)
    pain_to_brands = defaultdict(set)
    for r in rows:
        if r.get('top_scene'): scene_to_brands[r['top_scene']].add(r.get('domain'))
        if r.get('top_pain'):  pain_to_brands[r['top_pain']].add(r.get('domain'))

    # 3. 2026 兔子洞分数 = 基础分 × sibling 聚集度 × 新鲜度 × 价格档 × 活跃度
    def rh_score(r):
        base = r.get('score', 0) or 0
        sib_scene = len(scene_to_brands.get(r.get('top_scene') or '', set()))
        sib_pain  = len(pain_to_brands.get(r.get('top_pain') or '', set()))
        cluster_boost = min(30, (sib_scene + sib_pain) * 3)    # 同场景/痛点有多个品牌 = 赛道正在成形
        age = r.get('ad_age_days') or 0
        freshness = 20 if age < 30 else (10 if age < 90 else 0) # 偏好新品
        price = r.get('price_main') or 0
        price_fit = 15 if 35 <= price <= 120 else (5 if price else 0)
        active_bonus = 8 if r.get('is_active') else 0
        shopify_bonus = 5 if r.get('is_shopify_shape') else 0
        rh = base + cluster_boost + freshness + price_fit + active_bonus + shopify_bonus
        return int(min(200, rh))

    for r in rows:
        r['rh_score'] = rh_score(r)
        r['rh_siblings'] = len(scene_to_brands.get(r.get('top_scene') or '', set())) + \
                           len(pain_to_brands.get(r.get('top_pain') or '', set()))

    rows.sort(key=lambda r: r['rh_score'], reverse=True)
    total = len(rows)
    page = rows[offset:offset+limit]

    response.headers["X-Total-Count"] = str(total)
    response.headers["Access-Control-Expose-Headers"] = "X-Total-Count"
    return page


@app.get("/playbook/{domain}")
def playbook_deepdive(domain: str):
    """单品牌完整战术拆解 (用于 Deep-Dive 弹窗)."""
    from .playbook import analyze as playbook_analyze
    from .persistence import get_persistence
    from .categories import get_region_metadata
    db = get_persistence()
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM recommendations WHERE domain = ?", (domain,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "not found")
    rec = dict(row)
    rec = scrub_output(rec)
    pb = playbook_analyze(rec)
    rec.update(pb)

    # 同关键词其他品牌 (竞争情报)
    with db._conn() as conn:
        siblings = [dict(r) for r in conn.execute(
            "SELECT domain, brand_name, score, ad_age_days, is_shopify_shape "
            "FROM recommendations WHERE keyword = ? AND domain != ? "
            "ORDER BY score DESC LIMIT 10",
            (rec.get('keyword', ''), domain)
        )]
    rec['competitors_same_keyword'] = [scrub_output(s) for s in siblings]
    rec['market_heat'] = len(siblings) + 1
    return rec


@app.get("/categories")
def list_categories():
    from .categories import load_categories, category_stats
    cfg = load_categories()
    return {
        "config": cfg,
        "stats": category_stats(),
    }


@app.post("/categories/seed")
def seed_categories():
    from .categories import ingest_category_seeds
    n = ingest_category_seeds()
    return {"ingested": n}


@app.get("/ops/log-tail")
def log_tail(n: int = Query(80, ge=1, le=500)):
    """返回最近 n 行 pipeline.log."""
    from .paths import LOG_DIR
    log_file = LOG_DIR / "pipeline.log"
    if not log_file.exists():
        return {"lines": []}
    with open(log_file, encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    return {"lines": [l.rstrip() for l in lines[-n:]]}


@app.get("/ops/queue")
def ops_queue(force: bool = Query(False, description="绕过缓存强制重算")):
    """队列内容分析 (按类型统计). 5 分钟 TTL 缓存, scan_queue_loader 生成慢."""
    import time
    ttl = 300
    cache = ops_queue._cache  # type: ignore[attr-defined]
    now = time.time()
    if not force and cache.get('data') and (now - cache.get('at', 0)) < ttl:
        d = dict(cache['data'])
        d['cached'] = True
        d['cache_age_sec'] = int(now - cache['at'])
        return d
    from .scan_instructions import scan_queue_loader
    items = scan_queue_loader()
    by_type: dict[str, int] = {}
    for it in items:
        t = it.get('type', 'other') or 'other'
        by_type[t] = by_type.get(t, 0) + 1
    data = {
        "total": len(items),
        "by_type": sorted([{"type": k, "count": v} for k, v in by_type.items()],
                          key=lambda x: -x['count']),
        "preview": items[:20],
        "cached": False,
        "cache_age_sec": 0,
    }
    cache['data'] = data
    cache['at'] = now
    return data

ops_queue._cache = {'data': None, 'at': 0}  # type: ignore[attr-defined]


# ═══════════════════════════════════════════════════════════════════════════
@app.post("/admin/harvest-keywords")
def harvest_keywords(
    target: int = Query(200, ge=20, le=1000),
    force: bool = Query(False, description="即使队列充足也强制收割"),
):
    """从外部平台 (TikTok/Reddit/Google Trends/Amazon) 自动收割趋势关键词,
    过滤后追加到 facebook-ad-keywords-scan.txt 队列.
    queue < 50 自动触发, force=true 跳过阈值."""
    from .keyword_harvester import harvest_all, harvest_if_empty
    if force:
        return harvest_all(target=target)
    return harvest_if_empty(min_queue=50, target=target)


@app.post("/admin/v3/trend-lists")
def admin_v3_trend_lists(
    force: bool = Query(False, description="忽略每日一次限制, 强制刷新"),
    append_scan: bool = Query(False, description="把 FB+Shopify 适配词追加到 scan queue"),
):
    from .trend_product_lists import update_daily_lists
    return update_daily_lists(force=force, append_scan=append_scan)


@app.get("/admin/v3/trend-lists")
def get_v3_trend_lists():
    from .trend_product_lists import summary
    return summary()


@app.get("/admin/insights")
def get_insights_2026():
    """2026 Q2 FB Ads + TikTok Ads 顶级洞见 + 隐藏信号库 (rising niches / dead keywords / hidden signals)."""
    from .insights_2026 import export_insights
    return export_insights()


@app.post("/admin/lp-probe")
def admin_lp_probe(
    limit: int = Query(50, ge=1, le=500),
    workers: int = Query(8, ge=1, le=32),
    force: bool = Query(False, description="刷新已有指纹"),
):
    """后台探针 landing pages 技术栈 (Shopify/Klaviyo/Judge.me/Recharge/...)"""
    from .lp_prober import probe_domains
    return probe_domains(limit=limit, max_workers=workers, force_refresh=force)


@app.get("/admin/lp-fingerprints/stats")
def admin_lp_stats():
    """LP 指纹总览: 覆盖率 / tech 分布 / dropship 检出."""
    from collections import Counter
    from .persistence import get_persistence
    db = get_persistence()
    with db._conn() as conn:
        total_brands = conn.execute("SELECT COUNT(*) FROM recommendations WHERE landing_url IS NOT NULL AND landing_url != ''").fetchone()[0]
        try:
            probed = conn.execute("SELECT COUNT(*), AVG(bonus), SUM(has_dropship) FROM lp_fingerprints").fetchone()
        except Exception:
            probed = (0, 0, 0)
        tag_hist = Counter()
        try:
            for (tj,) in conn.execute("SELECT tags_json FROM lp_fingerprints WHERE tags_json IS NOT NULL"):
                import json as _json
                for t in _json.loads(tj or '[]'):
                    tag_hist[t] += 1
        except Exception:
            pass
    return {
        'total_brands_with_url': total_brands,
        'probed': probed[0] or 0,
        'coverage_pct': round(100 * (probed[0] or 0) / max(total_brands, 1), 1),
        'avg_bonus': round(probed[1] or 0, 1),
        'dropship_count': probed[2] or 0,
        'tech_distribution': dict(tag_hist.most_common(20)),
    }


@app.post("/admin/snapshot-ad-count")
def admin_snapshot_ad_count():
    """手动/cron: 写入今日 ads_count 快照到 rec_ad_count_history.

    velocity 信号需要 14 天前快照对比, 建议每日跑一次 (launchd/cron 可调).
    """
    from .persistence import get_persistence
    db = get_persistence()
    with db._conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS rec_ad_count_history (
            domain TEXT NOT NULL,
            snapshot_at TEXT NOT NULL,
            ads_count INTEGER,
            PRIMARY KEY (domain, snapshot_at)
        )""")
        today = datetime.now().strftime('%Y-%m-%d')
        cur = conn.execute(
            "INSERT OR IGNORE INTO rec_ad_count_history(domain, snapshot_at, ads_count) "
            "SELECT domain, ?, ads_count FROM recommendations WHERE ads_count IS NOT NULL",
            (today,),
        )
        inserted = cur.rowcount
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM rec_ad_count_history").fetchone()[0]
        oldest = conn.execute("SELECT MIN(snapshot_at) FROM rec_ad_count_history").fetchone()[0]
    return {'ok': True, 'date': today, 'inserted': inserted, 'total_history_rows': total,
            'oldest_snapshot': oldest,
            'velocity_ready': oldest and oldest <= (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')}


@app.get("/admin/insights-v2")
def get_insights_2026_v2():
    """2026 Q2 顶级信号库 v2: velocity · diversity · age-curve · cluster · LP-fingerprint · geo · fatigue · cross-platform · engagement · reddit."""
    from .insights_2026_v2 import export_v2
    return export_v2()


# ── v9.1 insights v3 — ad-archive 派生 (creative · geo · demographics · platforms · fatigue · copy)

@app.get("/admin/insights/v3/summary")
@ttl_cache(ttl_seconds=180)
def insights_v3_summary(days: int = Query(14, ge=1, le=180)):
    from .insights_v3 import summary
    return summary(days=days)


@app.get("/admin/insights/v3/creative-mix")
@ttl_cache(ttl_seconds=180)
def insights_v3_creative_mix(
    days: int = Query(14, ge=1, le=180),
    domain: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
):
    from .insights_v3 import creative_mix
    return creative_mix(days=days, domain=domain, keyword=keyword)


@app.get("/admin/insights/v3/geo-distribution")
@ttl_cache(ttl_seconds=180)
def insights_v3_geo(
    days: int = Query(14, ge=1, le=180),
    domain: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
):
    from .insights_v3 import geo_distribution
    return geo_distribution(days=days, domain=domain, keyword=keyword)


@app.get("/admin/insights/v3/demographics")
@ttl_cache(ttl_seconds=180)
def insights_v3_demographics(
    days: int = Query(14, ge=1, le=180),
    domain: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
):
    from .insights_v3 import demographics
    return demographics(days=days, domain=domain, keyword=keyword)


@app.get("/admin/insights/v3/platforms")
@ttl_cache(ttl_seconds=180)
def insights_v3_platforms(
    days: int = Query(14, ge=1, le=180),
    domain: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
):
    from .insights_v3 import platform_mix
    return platform_mix(days=days, domain=domain, keyword=keyword)


@app.get("/admin/insights/v3/creative-fatigue")
@ttl_cache(ttl_seconds=180)
def insights_v3_fatigue(
    days: int = Query(30, ge=1, le=365),
    domain: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
):
    from .insights_v3 import creative_fatigue
    return creative_fatigue(days=days, domain=domain, keyword=keyword)


@app.get("/admin/insights/v3/copy-patterns")
@ttl_cache(ttl_seconds=180)
def insights_v3_copy(
    days: int = Query(14, ge=1, le=180),
    domain: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
):
    from .insights_v3 import copy_patterns
    return copy_patterns(days=days, domain=domain, keyword=keyword)


@app.get("/admin/insights/v3/latest-ads")
@ttl_cache(ttl_seconds=120)
def insights_v3_latest_ads(
    limit: int = Query(50, ge=1, le=500),
    hours: int = Query(0, ge=0, le=720),
    keyword: Optional[str] = Query(None),
    domain: Optional[str] = Query(None),
    only_with_graphql: bool = Query(False),
):
    """v9.1: 最新数据按钮 — 按 inserted_at DESC 拿最近 N 条 ad-level 富数据."""
    from .insights_v3 import latest_ads
    return latest_ads(
        limit=limit, hours=hours, keyword=keyword,
        domain=domain, only_with_graphql=only_with_graphql,
    )


# ── v9.2 老兵直觉 / 渗透率 / 跟买卡 (3 条新端点) ──────────────────────────────

@app.get("/admin/insights/v3/veteran-flags")
@ttl_cache(ttl_seconds=180)
def insights_v3_veteran_flags(
    days: int = Query(30, ge=1, le=180),
    limit: int = Query(50, ge=1, le=300),
    only_active: bool = Query(True),
    min_ads: int = Query(2, ge=1, le=50),
    verdict: Optional[str] = Query(None),
):
    """13 条老兵直觉信号 + 综合评分, 返回 top-N 品牌."""
    from .veteran_signals import veteran_flags
    return veteran_flags(days=days, limit=limit, only_active=only_active,
                          min_ads=min_ads, verdict=verdict)


@app.get("/admin/insights/v3/saturation")
@ttl_cache(ttl_seconds=60)
def insights_v3_saturation(
    keyword: str = Query(..., min_length=2),
    days: int = Query(30, ge=1, le=180),
):
    """单关键词的渗透率 / HHI / 新进入者 / verdict (LOCKED/TIGHT/OPEN/...)."""
    from .veteran_signals import saturation
    return saturation(keyword=keyword, days=days)


@app.get("/admin/insights/v3/follow-card")
@ttl_cache(ttl_seconds=60)
def insights_v3_follow_card(
    domain: str = Query(..., min_length=4),
    days: int = Query(60, ge=1, le=180),
):
    """单品牌跟买决策卡 — 给徒弟看的那张纸."""
    from .veteran_signals import follow_card
    return follow_card(domain=domain, days=days)


@app.get("/admin/scraper/throughput")
@ttl_cache(ttl_seconds=120)
def admin_scraper_throughput():
    import sqlite3 as _sqlite3
    from .persistence import get_persistence
    _pdb = get_persistence()
    out = {}
    with _pdb._conn() as conn:
        try:
            # ad_archive inserted_at 是带时间戳的, 比 scans.scan_date (日期) 更精确
            for label, hours in (('last_1h', 1), ('last_6h', 6),
                                  ('last_24h', 24), ('last_3d', 72)):
                row = conn.execute(
                    "SELECT COUNT(DISTINCT keyword) AS kw, COUNT(*) AS ads "
                    "FROM ad_archive "
                    "WHERE inserted_at >= datetime('now', '-' || ? || ' hours')",
                    (hours,)
                ).fetchone()
                kw = row[0] or 0
                ads = row[1] or 0
                out[label] = {
                    'keywords': kw,
                    'ads': ads,
                    'kw_per_hour': round(kw / hours, 1),
                    'ads_per_hour': round(ads / hours, 1),
                }
        except Exception as e:
            out['error'] = str(e)

    # 当前抓取参数
    from .scraper import (DEFAULT_KEYWORD_DELAY, LONG_BREAK_PROB,
                          DEFAULT_CYCLE_MINUTES, FB_BLOCK_RESOURCES)
    out['speed_pack'] = {
        'version': 'v9.4',
        'optimizations': [
            f'resource blocker (image/font/css/media): {"ON" if FB_BLOCK_RESOURCES else "OFF"}',
            f'inter-keyword delay: {DEFAULT_KEYWORD_DELAY[0]}-{DEFAULT_KEYWORD_DELAY[1]}s',
            f'long break probability: {LONG_BREAK_PROB}',
            f'cycle interval: {DEFAULT_CYCLE_MINUTES}min',
            'human waits v9.4 (initial 0.1-0.3, scroll 0.2-0.6)',
            'max_scrolls 12 + adaptive early-break',
        ],
    }
    return out


# ── v9.5 详情页 enrich pipeline ──────────────────────────────────────────────

@app.get("/admin/enrich/stats")
def admin_enrich_stats():
    """详情页 enrich 进度统计."""
    from .persistence import get_persistence
    _pdb = get_persistence()
    with _pdb._conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ad_archive)").fetchall()}
        if 'enriched_at' not in cols:
            return {'enriched_total': 0, 'pending_eligible': 0,
                    'note': 'enriched_at column not yet created — call /admin/enrich/run-now to bootstrap'}
        total = conn.execute("SELECT COUNT(*) FROM ad_archive WHERE is_active=1").fetchone()[0]
        enriched = conn.execute(
            "SELECT COUNT(*) FROM ad_archive WHERE is_active=1 AND enriched_at IS NOT NULL"
        ).fetchone()[0]
        recent = conn.execute(
            "SELECT COUNT(*) FROM ad_archive "
            "WHERE enriched_at >= datetime('now', '-24 hours')"
        ).fetchone()[0]
        by_src = {(r[0] or '?'): r[1] for r in conn.execute(
            "SELECT enrich_source, COUNT(*) FROM ad_archive "
            "WHERE enriched_at IS NOT NULL GROUP BY enrich_source"
        )}
        e_video = conn.execute(
            "SELECT COUNT(*) FROM ad_archive "
            "WHERE enriched_at IS NOT NULL AND video_duration_sec IS NOT NULL"
        ).fetchone()[0]
        e_cards = conn.execute(
            "SELECT COUNT(*) FROM ad_archive "
            "WHERE enriched_at IS NOT NULL AND cards_count > 1"
        ).fetchone()[0]
        e_regional = conn.execute(
            "SELECT COUNT(*) FROM ad_archive "
            "WHERE enriched_at IS NOT NULL AND regional_distribution IS NOT NULL "
            "AND regional_distribution != '[]' AND regional_distribution != ''"
        ).fetchone()[0]
        return {
            'active_ads_total': total,
            'enriched_total': enriched,
            'pending_eligible': max(0, total - enriched),
            'enriched_24h': recent,
            'enriched_pct': round(enriched / max(total, 1) * 100, 1),
            'by_source': by_src,
            'enriched_field_fill': {
                'video_duration_sec': e_video,
                'cards_real_carousel': e_cards,
                'regional_distribution': e_regional,
            },
        }


@app.post("/admin/enrich/run-now")
def admin_enrich_run_now(
    min_score: int = Query(70, ge=30, le=100),
    max_brands: int = Query(10, ge=1, le=50),
    max_ads_per_brand: int = Query(5, ge=1, le=20),
    days: int = Query(30, ge=7, le=180),
):
    """对 score>=min_score 的 GO_FOLLOW brand 跑详情页 enrich (后台线程)."""
    from threading import Thread
    from .snapshot_enricher import enrich_top_brands

    def _run():
        try:
            log.info(f"enrich_run_now: starting score>={min_score} "
                     f"brands={max_brands} ads={max_ads_per_brand}")
            r = enrich_top_brands(min_score=min_score, max_brands=max_brands,
                                    max_ads_per_brand=max_ads_per_brand, days=days)
            log.info(f"enrich_run_now: done {r.get('ads_ok')}/"
                     f"{r.get('ads_ok',0)+r.get('ads_fail',0)} "
                     f"fields={r.get('fields_filled')}")
        except Exception:
            log.exception("enrich_run_now failed")
    Thread(target=_run, daemon=True).start()
    return {
        'started': True,
        'params': {'min_score': min_score, 'max_brands': max_brands,
                    'max_ads_per_brand': max_ads_per_brand, 'days': days},
        'eta_min': max_brands * max_ads_per_brand * 5 // 60 + 1,
        'note': '5-10min 后看 /admin/enrich/stats 查进度',
    }


@app.post("/admin/enrich/single-ad")
def admin_enrich_single_ad(ad_archive_id: str = Query(..., min_length=8)):
    """对单条广告立即跑详情页 enrich (同步, 5-10s 内返回)."""
    from .snapshot_enricher import (enrich_one_ad, upsert_enriched,
                                     _ensure_enrich_columns)
    from .persistence import get_persistence
    aid = ad_archive_id.strip()
    if not aid.isdigit():
        raise HTTPException(status_code=400, detail="ad_archive_id must be digits")
    enriched = enrich_one_ad(aid)
    if not enriched:
        return {'ok': False, 'reason': 'no detail data (ad inactive / 404 / FB blocked)'}
    _pdb = get_persistence()
    with _pdb._conn() as conn:
        _ensure_enrich_columns(conn)
        res = upsert_enriched(conn, aid, enriched)
        conn.commit()
    return {
        'ok': True,
        'ad_archive_id': aid,
        'source': enriched.get('_enrich_source'),
        'graphql_meta': enriched.get('_graphql_meta'),
        'fields_filled': res.get('fields_filled', []),
        'video_duration_sec': enriched.get('video_duration_sec'),
        'cards_count': len(enriched.get('cards') or []),
        'body_len': len(enriched.get('body') or ''),
    }


@app.get("/recommendations/rerank-v2")
def recommendations_rerank_v2(
    limit: int = Query(50, ge=1, le=500),
    min_base_score: int = Query(40, ge=0, le=120),
    only_shopify: bool = Query(True),
):
    """用 2026-v2 信号对已有推荐进行顶级再排序.

    v2 维度: velocity · diversity · age-curve · cluster · geo · fatigue · engagement · cross-platform
    velocity 需要 14d 前快照, 首次请求会写快照, 之后 14d 起开始给真分.
    """
    import json as _json
    from .insights_2026_v2 import score_brand_v2
    from .persistence import get_persistence
    from .lp_prober import get_fingerprints_bulk

    _pdb = get_persistence()
    with _pdb._conn() as conn:

        # 可选: 建一张极简 ad_count 历史表 (首次自动建, 无破坏)
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS rec_ad_count_history (
                domain TEXT NOT NULL,
                snapshot_at TEXT NOT NULL,
                ads_count INTEGER,
                PRIMARY KEY (domain, snapshot_at)
            )""")
            conn.commit()
        except Exception:
            pass

        cand_limit = min(limit * 6, 300)
        shopify_clause = "AND is_shopify_shape = 1" if only_shopify else ""
        cur = conn.execute(f"""
            SELECT r.domain, r.brand_name, r.score, r.ads_count, r.ad_age_days,
                   r.is_shopify_shape, r.keyword, r.scanned_at, r.first_seen_at,
                   r.landing_url, r.signal_score, r.orbit_score,
                   b.highest_likes, b.appearance_count, b.raw_json, b.last_seen
            FROM recommendations r
            LEFT JOIN brands b ON b.domain = r.domain
            WHERE r.score >= ?
              {shopify_clause}
            ORDER BY r.score DESC
            LIMIT ?
        """, (min_base_score, cand_limit))

        now = datetime.now()
        today_iso = now.strftime('%Y-%m-%d')
        rows_in = [dict(r) for r in cur.fetchall()]

        # 写今日快照 (幂等, 同一天同一 domain 不重插)
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO rec_ad_count_history(domain, snapshot_at, ads_count) VALUES (?, ?, ?)",
                [(r['domain'], today_iso, r.get('ads_count') or 0) for r in rows_in],
            )
            conn.commit()
        except Exception:
            pass

        # 同 keyword 的品牌数 (cluster signal)
        cluster_map: dict[str, int] = {}
        try:
            kws = list({r.get('keyword') for r in rows_in if r.get('keyword')})
            if kws:
                qmarks = ','.join(['?'] * len(kws))
                c2 = conn.execute(
                    f"SELECT keyword, COUNT(DISTINCT domain) c FROM recommendations "
                    f"WHERE keyword IN ({qmarks}) GROUP BY keyword", kws,
                )
                cluster_map = {row[0]: row[1] for row in c2.fetchall()}
        except Exception:
            pass

        # 批量取 LP 指纹 + 计算 angle_diversity (同 keyword 下 brand 名称唯一性)
        doms = [r['domain'] for r in rows_in]
        lp_map = get_fingerprints_bulk(doms) if doms else {}
        angle_map: dict[str, float] = {}
        try:
            # 同 keyword 下不同品牌名的基础字符差异 → 用名称首字母集合近似
            from collections import defaultdict
            kw_brands = defaultdict(set)
            for r in rows_in:
                kw = r.get('keyword') or ''
                bn = (r.get('brand_name') or '').lower().strip()
                if kw and bn:
                    kw_brands[kw].add(bn[:8])  # 前8字符指纹
            for kw, s in kw_brands.items():
                # diversity = 唯一名前缀数 / 品牌总数, clamp [0,1]
                cnt = cluster_map.get(kw, 1) if False else len(s)  # noqa
                # 我们只能近似: 不同前缀越多 → 角度越分化
                angle_map[kw] = min(1.0, len(s) / 3.0)
        except Exception:
            pass

        out = []
        for r in rows_in:
            # velocity: 查 14d 前快照
            ad_count_14d_ago = None
            try:
                h = conn.execute(
                    "SELECT ads_count FROM rec_ad_count_history "
                    "WHERE domain = ? AND snapshot_at <= date('now', '-14 days') "
                    "ORDER BY snapshot_at DESC LIMIT 1",
                    (r['domain'],),
                ).fetchone()
                if h:
                    ad_count_14d_ago = h[0]
            except Exception:
                pass

            # 解析 brand raw_json → country_codes / variant_total
            country_codes: list[str] = []
            variant_total = 0
            try:
                raw = r.get('raw_json') or '{}'
                bj = _json.loads(raw) if isinstance(raw, str) else (raw or {})
                cs = bj.get('countries') or bj.get('country_codes') or []
                if isinstance(cs, dict):
                    country_codes = list(cs.keys())
                elif isinstance(cs, list):
                    country_codes = [str(c) for c in cs]
                variant_total = int(bj.get('variant_total') or bj.get('variants') or 0)
            except Exception:
                pass

            # fatigue: last_seen 距今天数
            days_since_latest = 0
            try:
                last_s = r.get('last_seen') or r.get('scanned_at')
                if last_s:
                    last = datetime.fromisoformat(str(last_s).replace('Z', '+00:00').split('.')[0])
                    days_since_latest = max(0, (now - last).days)
            except Exception:
                pass

            v2_input = {
                'ad_count_now': r.get('ads_count') or 0,
                'ad_count_14d_ago': ad_count_14d_ago,
                'variant_total': variant_total,
                'ad_age_days': r.get('ad_age_days') or 0,
                'brand_count_in_niche': cluster_map.get(r.get('keyword') or '', 1),
                'angle_diversity': angle_map.get(r.get('keyword') or '', 0.5),
                'lp_cache': lp_map.get(r['domain']),   # ← 真 LP 指纹
                'country_codes': country_codes,
                'days_since_latest': days_since_latest,
                'ad_text': f"{r.get('brand_name') or ''} {r.get('keyword') or ''}",
                'has_vertical_video': False,
                'likes': r.get('highest_likes') or 0,
                'reddit_hits': 0,
            }
            v2 = score_brand_v2(v2_input)
            out.append({
                'domain': r['domain'],
                'brand_name': r['brand_name'],
                'keyword': r.get('keyword'),
                'base_score': r['score'],
                'ads_count': r.get('ads_count'),
                'ad_age_days': r.get('ad_age_days'),
                'highest_likes': r.get('highest_likes'),
                'variant_total': variant_total,
                'country_codes': country_codes,
                'is_shopify': bool(r.get('is_shopify_shape')),
                'landing_url': r.get('landing_url'),
                'brand_count_in_niche': cluster_map.get(r.get('keyword') or '', 1),
                'v2_total': v2['v2_total'],
                'v2_tier': v2['v2_tier'],
                'v2_breakdown': v2['v2_breakdown'],
                'v2_tags': v2['v2_tags'],
                'combined_score': round(v2['v2_total'] * 0.6 + (r['score'] or 0) * 0.4, 1),
            })

        out.sort(key=lambda x: x['combined_score'], reverse=True)
        return {
            'count': len(out[:limit]),
            'doctrine': '60% v2 (velocity/diversity/age/cluster/geo/fatigue/engagement) + 40% 原 base_score',
            'note': 'velocity 需 14d 历史快照, 首次调用后快照开始累积, 14d 后显现真值',
            'items': out[:limit],
        }


@app.get("/export/excel")
def export_excel():
    """下载当前 Excel 汇总文件 (facebook-ad-keywords-master.xlsx)."""
    from fastapi.responses import FileResponse
    from .paths import EXCEL_FILE
    if not EXCEL_FILE.exists():
        raise HTTPException(404, f"Excel 文件尚未生成: {EXCEL_FILE.name}")
    return FileResponse(
        str(EXCEL_FILE),
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        filename=EXCEL_FILE.name,
    )


@app.post("/export/excel/rebuild")
def export_excel_rebuild():
    """强制重建 Excel (从最新 _corrected.json 全量), 返回统计."""
    from .excel_writer import update_excel
    try:
        kw_count, brand_count = update_excel()
        return {"ok": True, "keywords": kw_count, "brands": brand_count}
    except Exception as e:
        log.exception("excel rebuild failed")
        raise HTTPException(500, f"rebuild 失败: {e}")


@app.get("/export/html", response_class=HTMLResponse)
def export_html(
    limit: int = Query(50, ge=1, le=500),
    download: bool = Query(False, description="true=作为附件下载; false=在线预览"),
):
    """生成自包含 HTML 报告.
    - download=false: 浏览器直接渲染 (预览)
    - download=true: 作为 .html 文件下载
    """
    from .exporter import export_html_report
    try:
        out = export_html_report(limit=limit)
    except Exception as e:
        log.exception("html export failed")
        raise HTTPException(500, f"HTML 生成失败: {e}")
    content = out.read_text(encoding='utf-8')
    if download:
        return Response(
            content=content,
            media_type='text/html; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="{out.name}"'},
        )
    return HTMLResponse(content=content)


# ─── 自定义关键词 ───────────────────────────────────────────────────────────
@app.post("/keywords/custom")
def add_custom_keywords(payload: dict):
    """追加自定义关键词到扫描队列.
    Body: {"keywords": [{"keyword": "...", "translation": "...", "type": "product|pain|scene|brand", "subtype": "..."}, ...]}
        或 {"keywords": ["word1", "word2"]} (简写)
    """
    from .paths import SCAN_FILE, LEARNED_FILE
    items = payload.get('keywords') if isinstance(payload, dict) else None
    if not items or not isinstance(items, list):
        raise HTTPException(400, "body 必须是 {keywords: [...]}")

    # 收集已有关键词 (小写规范化)
    known: set[str] = set()
    for f in (SCAN_FILE, LEARNED_FILE):
        if f.exists():
            for line in f.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                k = line.lstrip('#').strip().split('|')[0].strip().lower()
                if k:
                    known.add(k)

    new_lines, skipped = [], []
    allowed_types = {'product', 'pain', 'scene', 'brand', 'custom'}
    for raw in items:
        if isinstance(raw, str):
            it = {'keyword': raw}
        elif isinstance(raw, dict):
            it = raw
        else:
            skipped.append({'input': str(raw), 'reason': '格式不支持'})
            continue
        kw = (it.get('keyword') or '').strip()
        if not kw:
            skipped.append({'input': str(raw), 'reason': 'keyword 为空'})
            continue
        if len(kw) > 120:
            skipped.append({'input': kw, 'reason': '长度超过 120'})
            continue
        if kw.lower() in known:
            skipped.append({'input': kw, 'reason': '已存在'})
            continue
        tr = (it.get('translation') or '待翻译').strip()
        tp = (it.get('type') or 'custom').strip().lower()
        if tp not in allowed_types:
            tp = 'custom'
        sub = (it.get('subtype') or '自定义').strip()
        new_lines.append(f"{kw}|{tr}|{tp}|{sub}")
        known.add(kw.lower())

    if new_lines:
        today = datetime.now().strftime('%Y-%m-%d')
        with open(SCAN_FILE, 'a', encoding='utf-8') as f:
            f.write(f"\n# === 自定义入队 {today} ({len(new_lines)} 个) ===\n")
            for ln in new_lines:
                f.write(f"{ln}\n")
        log.info(f"自定义关键词入队: {len(new_lines)} 个, 跳过 {len(skipped)}")

    return {"ok": True, "added": len(new_lines), "skipped": skipped,
            "preview": new_lines[:10]}


# ─── 自定义类目 ─────────────────────────────────────────────────────────────
@app.post("/categories/custom")
def add_custom_category(payload: dict):
    """新增/更新一个类目. 若 id 已存在则合并 seeds (去重).
    Body: {
        "id": "..",
        "name": "..",
        "priority": 1-9,
        "tier": "high_ticket|mid_ticket|low_ticket",
        "avg_price_usd": 89,
        "seeds": ["kw1", "kw2", ...]
    }
    """
    from .categories import CATEGORIES_FILE, load_categories, ingest_category_seeds
    cid = (payload.get('id') or '').strip()
    if not cid:
        raise HTTPException(400, "id 必填")
    name = (payload.get('name') or cid).strip()
    priority = int(payload.get('priority', 5))
    priority = max(1, min(9, priority))
    tier = (payload.get('tier') or 'mid_ticket').strip()
    avg_price = float(payload.get('avg_price_usd', 0) or 0)
    seeds_raw = payload.get('seeds') or []
    if not isinstance(seeds_raw, list):
        raise HTTPException(400, "seeds 必须为数组")
    seeds = [s.strip() for s in seeds_raw if isinstance(s, str) and s.strip()]
    seeds = list(dict.fromkeys(seeds))  # 保序去重
    if not seeds:
        raise HTTPException(400, "seeds 不能为空")

    cfg = load_categories()
    cats = cfg.setdefault('categories', [])
    existing = next((c for c in cats if c.get('id') == cid), None)
    if existing:
        merged = list(dict.fromkeys((existing.get('seeds') or []) + seeds))
        existing.update({
            'name': name, 'priority': priority, 'tier': tier,
            'avg_price_usd': avg_price, 'seeds': merged,
            'custom': True, 'updated_at': datetime.now().isoformat(timespec='seconds'),
        })
        action = 'updated'
    else:
        cats.append({
            'id': cid, 'name': name, 'priority': priority, 'tier': tier,
            'avg_price_usd': avg_price, 'seeds': seeds,
            'custom': True,
            'created_at': datetime.now().isoformat(timespec='seconds'),
        })
        action = 'created'

    CATEGORIES_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    ingested = ingest_category_seeds()
    return {"ok": True, "action": action, "id": cid,
            "seeds_count": len(seeds), "ingested_into_queue": ingested}


@app.delete("/categories/custom/{cid}")
def delete_custom_category(cid: str):
    """删除一个自定义类目 (custom=True 才允许删, 防止误删内置)."""
    from .categories import CATEGORIES_FILE, load_categories
    cfg = load_categories()
    cats = cfg.get('categories', [])
    target = next((c for c in cats if c.get('id') == cid), None)
    if not target:
        raise HTTPException(404, f"类目不存在: {cid}")
    if not target.get('custom'):
        raise HTTPException(403, f"内置类目不可删除: {cid}")
    cfg['categories'] = [c for c in cats if c.get('id') != cid]
    CATEGORIES_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    return {"ok": True, "deleted": cid}


DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FB Pipeline · 商用级仪表盘</title>
<style>
  :root{
    --hdr-h:58px; --tabs-h:48px; --tool-h:62px;
    --total-top: calc(var(--hdr-h) + var(--tabs-h) + var(--tool-h));
    /* ── 顶级设计令牌 · 深色主题 ── */
    --bg-0:#07090f;        /* 最底层背景 */
    --bg-1:#0d1119;        /* 面板背景 */
    --bg-2:#141926;        /* 提升面板 */
    --bg-3:#1b2232;        /* 交互悬停 */
    --bg-4:#242d40;        /* 激活态 */
    --border-1:#1f2736;
    --border-2:#2b344a;
    --text-1:#e8ecf3;      /* 主文本 */
    --text-2:#a8b1c4;      /* 次文本 */
    --text-3:#6b7489;      /* 弱文本 */
    --brand:#5b9dff;
    --brand-hi:#7fb4ff;
    --brand-lo:#3a7ce0;
    --brand-glow:rgba(91,157,255,.35);
    --ok:#34d399;
    --warn:#fbbf24;
    --err:#f87171;
    --gold:#fbbf24;
    --radius-s:6px;
    --radius-m:8px;
    --radius-l:12px;
    --radius-xl:16px;
    --shadow-1:0 1px 2px rgba(0,0,0,.3);
    --shadow-2:0 4px 12px rgba(0,0,0,.28);
    --shadow-3:0 10px 28px rgba(0,0,0,.35);
    --ring: 0 0 0 3px rgba(91,157,255,.35);
    --ease: cubic-bezier(.22,.61,.36,1);
  }
  *{box-sizing:border-box;}
  *::selection{background:var(--brand-glow);color:var(--text-1);}
  body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Inter",sans-serif;margin:0;background:radial-gradient(1200px 600px at 20% -10%,rgba(91,157,255,.08),transparent 60%),radial-gradient(900px 500px at 110% 10%,rgba(167,120,255,.06),transparent 60%),var(--bg-0);color:var(--text-1);font-size:15.5px;-webkit-font-smoothing:antialiased;letter-spacing:.1px;}
  /* 自定义滚动条 */
  ::-webkit-scrollbar{width:10px;height:10px;}
  ::-webkit-scrollbar-track{background:transparent;}
  ::-webkit-scrollbar-thumb{background:var(--bg-3);border-radius:10px;border:2px solid transparent;background-clip:content-box;}
  ::-webkit-scrollbar-thumb:hover{background:var(--bg-4);background-clip:content-box;}
  header{background:linear-gradient(180deg,rgba(20,25,38,.85),rgba(13,17,25,.72));padding:14px 26px;border-bottom:1px solid var(--border-1);position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:14px;height:var(--hdr-h);backdrop-filter:blur(14px) saturate(1.1);-webkit-backdrop-filter:blur(14px) saturate(1.1);}
  h1{margin:0;font-size:20px;font-weight:700;letter-spacing:.3px;color:var(--text-1);display:inline-flex;align-items:center;gap:6px;}
  h1 .h1-emoji{filter:saturate(1.2);}
  h1 .h1-text{background:linear-gradient(135deg,#fff,#a8b1c4);-webkit-background-clip:text;background-clip:text;color:transparent;}
  .ver{font-size:12px;font-weight:600;color:var(--text-3);background:rgba(255,255,255,.04);padding:3px 10px;border-radius:999px;border:1px solid var(--border-1);letter-spacing:.3px;}
  .badge-mode{font-size:11.5px;font-weight:600;color:#c4b5fd;background:linear-gradient(135deg,rgba(139,92,246,.18),rgba(91,157,255,.12));padding:3px 10px 3px 8px;border-radius:999px;border:1px solid rgba(139,92,246,.28);letter-spacing:.3px;cursor:help;display:inline-flex;align-items:center;gap:2px;}
  body.light .badge-mode{color:#6b5a8a;background:linear-gradient(135deg,#eee4f4,#e3e9f0);border:1px solid #dcd2e3;}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:middle;transition:box-shadow .3s var(--ease);}
  .dot.ok{background:var(--ok);box-shadow:0 0 0 3px rgba(52,211,153,.18),0 0 10px rgba(52,211,153,.55);}
  .dot.warn{background:var(--warn);box-shadow:0 0 0 3px rgba(251,191,36,.2),0 0 10px rgba(251,191,36,.55);}
  .dot.err{background:var(--err);box-shadow:0 0 0 3px rgba(248,113,113,.22),0 0 10px rgba(248,113,113,.6);animation:pulseErr 1.6s ease-in-out infinite;}
  @keyframes pulseErr{0%,100%{opacity:1;}50%{opacity:.55;}}
  .status-line{font-size:13.5px;color:var(--text-2);margin-left:auto;font-variant-numeric:tabular-nums;}
  /* ── 置顶状态横幅: 在 header 之前, 最醒目位置 ── */
  .status-center{position:relative;display:flex;justify-content:center;align-items:center;gap:12px;margin:14px auto 14px;padding:12px 24px;background:linear-gradient(135deg,rgba(20,25,38,.88),rgba(27,34,50,.78));border:1px solid var(--border-2);border-radius:999px;max-width:fit-content;font-size:15px;font-weight:500;color:var(--text-1);font-variant-numeric:tabular-nums;box-shadow:var(--shadow-2),inset 0 1px 0 rgba(255,255,255,.04);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);overflow:hidden;}
  .status-center::before{content:"";position:absolute;inset:-1px;border-radius:inherit;padding:1px;background:linear-gradient(135deg,rgba(91,157,255,.4),transparent 40%,transparent 60%,rgba(167,120,255,.3));-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;mask-composite:exclude;pointer-events:none;opacity:.6;}
  .status-center .dot{width:10px;height:10px;margin-right:0;}
  body.light .status-center{background:linear-gradient(135deg,#fdfcf8,#f4f1e8);border:1px solid #e4e1d8;color:#2d4a6b;box-shadow:0 8px 24px rgba(100,80,50,.1),inset 0 1px 0 #fff;}
  body.light .status-center::before{opacity:.35;background:linear-gradient(135deg,rgba(91,138,168,.4),transparent 50%,rgba(192,138,91,.3));}
  nav.tabs{display:flex;background:rgba(13,17,25,.65);border-bottom:1px solid var(--border-1);padding:0 26px;position:sticky;top:var(--hdr-h);z-index:40;height:var(--tabs-h);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);}
  nav.tabs button{position:relative;background:transparent;color:var(--text-2);border:0;padding:13px 20px;cursor:pointer;font-size:15px;font-weight:500;transition:color .2s var(--ease);letter-spacing:.2px;}
  nav.tabs button:hover{color:var(--text-1);}
  nav.tabs button::after{content:"";position:absolute;left:50%;right:50%;bottom:-1px;height:2px;background:linear-gradient(90deg,var(--brand),#a778ff);border-radius:2px 2px 0 0;transition:left .25s var(--ease),right .25s var(--ease);}
  nav.tabs button.active{color:var(--brand-hi);}
  nav.tabs button.active::after{left:16px;right:16px;box-shadow:0 0 12px var(--brand-glow);}
  nav.tabs button.hot-tab{position:relative;color:#fff;background:linear-gradient(135deg,rgba(139,92,246,.25),rgba(91,157,255,.2));border-radius:10px 10px 0 0;margin:4px 6px 0;padding:9px 16px;font-weight:600;letter-spacing:.3px;}
  nav.tabs button.hot-tab:hover{background:linear-gradient(135deg,rgba(139,92,246,.4),rgba(91,157,255,.3));color:#fff;}
  nav.tabs button.hot-tab.active{background:linear-gradient(135deg,#8b5cf6,#5b9dff);color:#fff;box-shadow:0 4px 16px rgba(139,92,246,.45);}
  nav.tabs button.hot-tab.active::after{display:none;}
  nav.tabs button.hot-tab .hot-badge{display:inline-block;background:#f87171;color:#fff;font-size:10px;font-weight:700;padding:1px 6px;border-radius:6px;margin-left:6px;letter-spacing:.3px;vertical-align:middle;animation:pulseBadge 2s ease-in-out infinite;}
  @keyframes pulseBadge{0%,100%{opacity:1;transform:scale(1);}50%{opacity:.75;transform:scale(1.08);}}
  body.light nav.tabs button.hot-tab{background:linear-gradient(135deg,#eee4f4,#e3e9f0) !important;color:#2d4a6b !important;}
  body.light nav.tabs button.hot-tab.active{background:linear-gradient(135deg,#a78bfa,#5b8aa8) !important;color:#fff !important;box-shadow:0 4px 16px rgba(91,138,168,.45) !important;}
  .toolbar{padding:13px 26px;background:rgba(11,14,20,.7);border-bottom:1px solid var(--border-1);display:flex;gap:10px;flex-wrap:wrap;align-items:center;position:sticky;top:calc(var(--hdr-h) + var(--tabs-h));z-index:30;min-height:var(--tool-h);height:auto;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);}
  .toolbar input[type=text]{background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);padding:9px 14px;border-radius:var(--radius-m);font-size:14.5px;min-width:260px;transition:border-color .18s var(--ease),box-shadow .18s var(--ease);}
  .toolbar input[type=text]:focus{outline:none;border-color:var(--brand);box-shadow:var(--ring);}
  .toolbar input[type=text]::placeholder{color:var(--text-3);}
  /* ── 药丸筛选器: 轻量点击反馈 ── */
  .toolbar .pill{background:var(--bg-2);color:var(--text-2);padding:7px 14px;border-radius:999px;font-size:13.5px;font-weight:500;cursor:pointer;border:1px solid var(--border-1);user-select:none;transition:background .18s var(--ease),color .18s var(--ease),border-color .18s var(--ease),transform .08s var(--ease);}
  .toolbar .pill:hover:not(.active){background:var(--bg-3);color:var(--text-1);border-color:var(--border-2);}
  .toolbar .pill:active{transform:scale(.97);}
  .toolbar .pill.active{background:linear-gradient(135deg,var(--brand),var(--brand-lo));color:#fff;border-color:transparent;box-shadow:0 2px 8px var(--brand-glow);}
  .toolbar label{display:flex;align-items:center;gap:6px;font-size:13.5px;color:var(--text-2);cursor:pointer;}
  .toolbar label:hover{color:var(--text-1);}
  /* ── 通用按钮: 顶级触感 ── */
  .toolbar button.btn, .btn{background:var(--bg-3);color:var(--text-1);border:1px solid var(--border-2);padding:8px 16px;border-radius:var(--radius-m);cursor:pointer;font-size:14px;font-weight:500;letter-spacing:.2px;transition:background .18s var(--ease),border-color .18s var(--ease),transform .08s var(--ease),box-shadow .18s var(--ease);}
  .toolbar button.btn:hover, .btn:hover{background:var(--bg-4);border-color:var(--border-2);}
  .toolbar button.btn:active, .btn:active{transform:translateY(1px);}
  .toolbar button.btn:focus-visible, .btn:focus-visible{outline:none;box-shadow:var(--ring);}
  .toolbar button.btn.primary, .btn.primary{background:linear-gradient(135deg,var(--brand),var(--brand-lo));color:#fff;border-color:transparent;box-shadow:0 2px 10px var(--brand-glow);}
  .toolbar button.btn.primary:hover, .btn.primary:hover{background:linear-gradient(135deg,var(--brand-hi),var(--brand));box-shadow:0 4px 16px var(--brand-glow);}
  .toolbar button.btn.success, .btn.success{background:linear-gradient(135deg,#34d399,#10b981);color:#031a10;border-color:transparent;font-weight:600;box-shadow:0 2px 10px rgba(52,211,153,.28);}
  .toolbar button.btn.success:hover, .btn.success:hover{background:linear-gradient(135deg,#6ee7b7,#34d399);box-shadow:0 4px 16px rgba(52,211,153,.36);}
  .toolbar .recs-only{display:inline-flex;}
  body.non-recs .recs-only{display:none;}
  .topright-actions .recs-only{display:inline-flex;}
  body.non-recs .topright-actions .recs-only{display:none;}
  .toolbar{flex-wrap:wrap;row-gap:8px;}
  #search{flex-basis:100%;width:100%;order:-1;margin-bottom:4px;}
  .strat-tbl thead th{position:sticky;top:0;z-index:5;background:var(--bg-3);box-shadow:0 1px 0 var(--border-1);}

  /* ── 右上角操作栏: 升级玻璃浮层 (双层: 按钮 + 数据状态) ── */
  .topright-actions{
    position:fixed;top:11px;right:14px;z-index:100;
    display:flex;flex-direction:column;gap:4px;padding:5px;border-radius:12px;
    background:rgba(13,17,25,.6);backdrop-filter:blur(16px) saturate(1.4);
    -webkit-backdrop-filter:blur(16px) saturate(1.4);
    box-shadow:0 6px 20px rgba(0,0,0,.35),inset 0 1px 0 rgba(255,255,255,.05);
    border:1px solid rgba(255,255,255,.07);
  }
  .topright-actions .btn-row{display:flex;gap:3px;}
  .topright-actions .status-row{display:flex;align-items:center;gap:6px;padding:4px 10px 2px;font-size:12px;color:var(--text-2);border-top:1px solid rgba(255,255,255,.06);font-variant-numeric:tabular-nums;white-space:nowrap;}
  body.light .topright-actions .status-row{color:#5a6070;border-top-color:#e4e1d8;}
  .topright-actions .btn{
    padding:6px 11px;font-size:12.5px;line-height:1;font-weight:500;
    background:transparent;color:var(--text-1);border:0;border-radius:8px;
    box-shadow:none;min-height:28px;transition:background .15s var(--ease),color .15s var(--ease),transform .08s var(--ease);
  }
  .topright-actions .btn:hover{background:rgba(255,255,255,.08);color:#fff;}
  .topright-actions .btn:active{transform:scale(.96);}
  .topright-actions .btn.primary{background:rgba(91,157,255,.16);color:var(--brand-hi);box-shadow:none;}
  .topright-actions .btn.primary:hover{background:rgba(91,157,255,.3);color:#fff;}
  .topright-actions .btn.success{background:rgba(52,211,153,.14);color:#6ee7b7;box-shadow:none;}
  .topright-actions .btn.success:hover{background:rgba(52,211,153,.28);color:#fff;}
  .topright-actions .sep{width:1px;align-self:stretch;margin:4px 2px;background:rgba(255,255,255,.08);}

  /* ── 护眼白天模式 (暖白米灰, 非刺眼纯白) ── */
  body.light{
    background:
      radial-gradient(1200px 600px at 20% -10%,rgba(91,138,168,.08),transparent 60%),
      radial-gradient(900px 500px at 110% 10%,rgba(192,138,91,.06),transparent 60%),
      #f6f5f0 !important;
    color:#3a3f4a;
  }
  body.light h1{background:linear-gradient(135deg,#2d4a6b,#5a6070) !important;-webkit-background-clip:text;background-clip:text;color:transparent !important;}
  body.light header{
    background:linear-gradient(180deg,rgba(251,250,246,.85),rgba(240,238,232,.7)) !important;
    border-bottom:1px solid #e4e1d8;
    color:#3a3f4a;
  }
  body.light header h1{color:#2d4a6b;}
  body.light .ver{background:#f0eee8;color:#7a8090;border:1px solid #d8d4c7;}
  body.light .status-line{color:#5a6070;}
  body.light nav.tabs{background:rgba(251,250,246,.8);border-bottom:1px solid #e4e1d8;}
  body.light nav.tabs button{color:#5a6070;background:transparent;}
  body.light nav.tabs button:hover{color:#2d4a6b;}
  body.light nav.tabs button::after{background:linear-gradient(90deg,#5b8aa8,#c08a5b) !important;}
  body.light nav.tabs button.active{color:#2d4a6b;background:transparent;border-bottom:0;}
  body.light nav.tabs button.active::after{box-shadow:0 0 12px rgba(91,138,168,.35) !important;}
  body.light .toolbar{background:#fbfaf6;border-bottom:1px solid #e4e1d8;}
  body.light .toolbar input, body.light .toolbar select, body.light input[type=text], body.light input[type=number]{
    background:#fdfcf8;color:#3a3f4a;border:1px solid #d8d4c7;
  }
  body.light .toolbar input[type=text]:focus{border-color:#5b8aa8 !important;box-shadow:0 0 0 3px rgba(91,138,168,.2) !important;}
  body.light .toolbar input::placeholder{color:#9097a3;}
  body.light .pill{background:#ede9dd;color:#4a5063;border:1px solid #d8d4c7;}
  body.light .pill:hover{background:#e2ddcc;}
  body.light .pill.active{background:linear-gradient(135deg,#6fa0c0,#4a7793) !important;color:#fdfcf8 !important;border-color:transparent !important;box-shadow:0 2px 8px rgba(91,138,168,.28) !important;}
  body.light .btn{background:#ede9dd;color:#3a3f4a;border:1px solid #d8d4c7;box-shadow:none;}
  body.light .btn:hover{background:#e2ddcc;border-color:#c8c3b4;}
  body.light .btn.primary{background:linear-gradient(135deg,#7ea476,#587849) !important;color:#fdfcf8 !important;border-color:transparent !important;box-shadow:0 2px 10px rgba(107,142,94,.28) !important;}
  body.light .btn.primary:hover{background:linear-gradient(135deg,#8ab180,#6b8e5e) !important;box-shadow:0 4px 16px rgba(107,142,94,.36) !important;}
  body.light .btn.success{background:linear-gradient(135deg,#d4a077,#a5734a) !important;color:#fdfcf8 !important;border-color:transparent !important;box-shadow:0 2px 10px rgba(192,138,91,.28) !important;}
  body.light .btn.success:hover{background:linear-gradient(135deg,#dcac85,#b47e54) !important;box-shadow:0 4px 16px rgba(192,138,91,.36) !important;}
  body.light .topright-actions{background:rgba(253,252,248,.75);border:1px solid #e4e1d8;box-shadow:0 6px 20px rgba(100,80,50,.1),inset 0 1px 0 #fff;}
  body.light .topright-actions .btn{color:#4a5063;background:transparent;box-shadow:none;}
  body.light .topright-actions .btn:hover{background:#ede9dd;color:#2d4a6b;}
  body.light .topright-actions .btn.primary{background:rgba(107,142,94,.15) !important;color:#587849 !important;box-shadow:none !important;}
  body.light .topright-actions .btn.primary:hover{background:rgba(107,142,94,.28) !important;color:#3e5432 !important;}
  body.light .topright-actions .btn.success{background:rgba(192,138,91,.15) !important;color:#a5734a !important;box-shadow:none !important;}
  body.light .topright-actions .btn.success:hover{background:rgba(192,138,91,.28) !important;color:#7d5635 !important;}
  body.light .topright-actions .sep{background:#d8d4c7;}
  body.light .card, body.light .top5-banner, body.light .modal{
    background:#fdfcf8;border:1px solid #e4e1d8;color:#3a3f4a;
    box-shadow:0 2px 12px rgba(100,80,50,.06);
  }
  body.light .top5-banner{
    background:linear-gradient(90deg,#fbfaf6,#f0eee8) !important;
    border-bottom:1px solid #e4e1d8;
  }
  body.light .top5-title{color:#2d4a6b !important;}
  body.light .top5-card{
    background:#fdfcf8 !important;
    border:1px solid #e4e1d8 !important;
    box-shadow:0 1px 4px rgba(100,80,50,.06);
  }
  body.light .top5-card .brand{color:#2d4a6b !important;}
  body.light .top5-card .meta{color:#5a6070 !important;}
  body.light .top5-card .rank{background:#5b8aa8 !important;color:#fdfcf8 !important;}
  body.light .top5-card .rank.gold{background:linear-gradient(135deg,#c08a5b,#a5734a) !important;color:#fdfcf8 !important;}
  body.light .top5-card .trend{border-top:1px dashed #e4e1d8 !important;}
  body.light .top5-card .trend-label{color:#5a6070 !important;}
  body.light .top5-card .tag{background:#ede9dd !important;color:#4a5063 !important;}
  body.light .top5-card .tag.gold{background:#e7d6b5 !important;color:#8a6a3a !important;}
  body.light .top5-card .tag.up{background:#d8e8d0 !important;color:#3e6b32 !important;}
  body.light .top5-card .tag.down{background:#e8d8cc !important;color:#8a4e32 !important;}
  body.light .top5-card .spark .bar{background:linear-gradient(180deg,#5b8aa8,#4a7793) !important;}
  body.light table{background:#fdfcf8;color:#3a3f4a;}
  body.light thead th{background:#ede9dd !important;color:#2d4a6b;border-bottom:1px solid #d8d4c7;box-shadow:0 1px 0 #e4e1d8;}
  body.light tbody td{color:#3a3f4a;border-top:1px solid #ede9dd;}
  body.light tbody tr:hover{background:#f3f0e7;}
  body.light .stat{background:linear-gradient(135deg,#fdfcf8,#f0eee8);border:1px solid #e4e1d8;color:#3a3f4a;}
  body.light .stat .label, body.light .stat .l{color:#7a8090;}
  body.light .stat .v{color:#2d4a6b;}
  body.light .truncate{color:#3a3f4a;}
  body.light .meta span{color:#5a6070;}
  body.light a{color:#4a7793;text-decoration:none;}
  body.light a:hover{color:#2d4a6b;text-decoration:underline;}
  body.light .strat-tbl thead th{background:#ede9dd;color:#2d4a6b;}
  body.light .pager{color:#5a6070;}
  body.light .pager button{background:#ede9dd !important;color:#3a3f4a !important;border:1px solid #d8d4c7 !important;}
  body.light .pager button:hover:not(:disabled){background:#e2ddcc !important;}
  body.light .pager button.active{background:#5b8aa8 !important;color:#fdfcf8 !important;border-color:#4a7793 !important;}
  body.light .pager button:disabled{opacity:.4 !important;}
  body.light .pager input{background:#fdfcf8 !important;color:#3a3f4a !important;border:1px solid #d8d4c7 !important;}
  body.light .pager select{background:#fdfcf8 !important;color:#3a3f4a !important;border:1px solid #d8d4c7 !important;}
  body.light .pager .info{color:#7a8090 !important;}
  body.light #col-picker{background:#fdfcf8 !important;color:#3a3f4a;border:1px solid #e4e1d8;}
  body.light .modal-bg{background:rgba(60,55,45,.45);}
  body.light .modal-bg .modal{background:#fdfcf8 !important;color:#3a3f4a;border:1px solid #e4e1d8;}
  body.light .modal h3{color:#2d4a6b;}
  body.light .score-pill{color:#fdfcf8 !important;}            /* 保留深色背景看清分数 */
  body.light .itag{background:#ede9dd !important;color:#4a5063 !important;}
  body.light .sig-badge{background:#c08a5b !important;color:#fdfcf8 !important;}
  body.light .shopify{color:#6b8e5e !important;}
  body.light .active-dot{color:#6b8e5e !important;}
  body.light code{background:#ede9dd;color:#2d4a6b;padding:2px 6px;border-radius:4px;}
  body.light .linkbtn{background:transparent;color:#4a7793;}
  body.light .linkbtn:hover{background:#ede9dd;color:#2d4a6b;}
  body.light .dot{border:1px solid #d8d4c7;}

  /* ── 通配补丁: 拦截所有内联硬编码深色, 映射到米白色系 ── */
  /* 用 *= 宽松匹配, 覆盖 background / linear-gradient / border-color 等各种位置 */
  body.light [style*="#0a0d13"],
  body.light [style*="#0b0e14"],
  body.light [style*="#0f131c"],
  body.light [style*="#0f1419"]{background:#f6f5f0 !important;color:#3a3f4a !important;}
  body.light [style*="#141824"],
  body.light [style*="#181e2a"],
  body.light [style*="#1a1f2d"]{background:#fdfcf8 !important;color:#3a3f4a !important;border-color:#e4e1d8 !important;}
  body.light [style*="#1a2030"],
  body.light [style*="#1e2433"],
  body.light [style*="#1f2633"],
  body.light [style*="#222938"]{background:#ede9dd !important;color:#3a3f4a !important;border-color:#d8d4c7 !important;}
  body.light [style*="#2a3142"]{background:#e4e1d8 !important;color:#3a3f4a !important;border-color:#d0ccbd !important;}
  body.light [style*="#0f172a"]{background:#fbfaf6 !important;color:#3a3f4a !important;}
  body.light [style*="#1e293b"]{background:#f0eee8 !important;color:#3a3f4a !important;}
  body.light [style*="#1e3a5f"]{background:#e3e9f0 !important;color:#2d4a6b !important;border-color:#d0dae5 !important;}
  body.light [style*="#15803d"]{background:#cfe1c5 !important;color:#3e6b32 !important;}
  body.light [style*="#14532d"]{background:#d8e8d0 !important;color:#3e6b32 !important;}
  body.light [style*="#854d0e"]{background:#e7d6b5 !important;color:#8a6a3a !important;}
  body.light [style*="#7c2d12"]{background:#e8d8cc !important;color:#8a4e32 !important;}
  body.light [style*="#3f3f46"]{background:#e4e1d8 !important;color:#4a5063 !important;}
  body.light [style*="#047857"]{background:#cfe1c5 !important;color:#3e6b32 !important;}
  /* gradient 渐变混合色 (#XXX → XXX 两端都要接得住) */
  body.light [style*="linear-gradient"][style*="#1e3a5f"]{background:linear-gradient(135deg,#e3e9f0,#f0eee8) !important;color:#2d4a6b !important;}
  body.light [style*="linear-gradient"][style*="#1a2030"]{background:linear-gradient(135deg,#fdfcf8,#ede9dd) !important;color:#3a3f4a !important;}
  /* 浅色文本但深色父容器里的, 回退成 柔墨色 */
  body.light [style*="color:#9aa3b6"],
  body.light [style*="color:#d5d8de"],
  body.light [style*="color:#e6e6e6"],
  body.light [style*="color:#93c5fd"],
  body.light [style*="color:#86efac"],
  body.light [style*="color:#fed7aa"],
  body.light [style*="color:#fef3c7"]{color:#3a3f4a !important;}

  /* ── 关键结构强制覆盖 ── */
  body.light{background:#f6f5f0 !important;}
  body.light header{background:linear-gradient(90deg,#fbfaf6,#f0eee8) !important;border-bottom:1px solid #e4e1d8 !important;}
  body.light nav.tabs{background:#fbfaf6 !important;border-bottom:1px solid #e4e1d8 !important;}
  body.light .toolbar{background:#fbfaf6 !important;border-bottom:1px solid #e4e1d8 !important;}
  body.light .toolbar input[type=text], body.light .toolbar input[type=number]{background:#fdfcf8 !important;color:#3a3f4a !important;border:1px solid #d8d4c7 !important;}
  body.light .toolbar input::placeholder{color:#9097a3 !important;}
  body.light table{background:#fdfcf8 !important;color:#3a3f4a !important;}
  body.light thead th{background:#ede9dd !important;color:#2d4a6b !important;border-bottom:1px solid #d8d4c7 !important;}
  body.light tbody td{color:#3a3f4a !important;border-top:1px solid #ede9dd !important;}
  body.light tbody tr:hover{background:#f3f0e7 !important;}
  body.light .modal-bg{background:rgba(60,55,45,.45) !important;}
  body.light .modal{background:#fdfcf8 !important;color:#3a3f4a !important;border:1px solid #e4e1d8 !important;}
  body.light #col-picker{background:#fdfcf8 !important;color:#3a3f4a !important;border:1px solid #e4e1d8 !important;}
  body.light .linkbtn{background:transparent !important;color:#4a7793 !important;}
  body.light .linkbtn:hover{background:#ede9dd !important;color:#2d4a6b !important;}

  /* ── 白天模式: 补齐所有 CSS class 级深色块 (inline 通配追不到的) ── */
  body.light .stats{background:#fbfaf6 !important;}
  body.light .pager{background:#fbfaf6 !important;border-top:1px solid #e4e1d8 !important;}
  body.light .pager button{background:#ede9dd !important;color:#3a3f4a !important;border:1px solid #d8d4c7 !important;}
  body.light .pager button:hover:not(:disabled){background:#e2ddcc !important;}
  body.light .pager button.active{background:#5b8aa8 !important;color:#fdfcf8 !important;}
  body.light .pager input, body.light .pager select{background:#fdfcf8 !important;color:#3a3f4a !important;border:1px solid #d8d4c7 !important;}
  /* tier 三色徽标 → 柔和米白系 */
  body.light .tier-testing{background:#e3e9f0 !important;color:#2d4a6b !important;}
  body.light .tier-validated{background:#e7d6b5 !important;color:#8a6a3a !important;}
  body.light .tier-proven{background:#d8e8d0 !important;color:#3e6b32 !important;}
  /* flag 覆盖徽标 */
  body.light .flag-covered{background:#d8e8d0 !important;color:#3e6b32 !important;}
  body.light .flag-missing{background:#e8d8cc !important;color:#8a4e32 !important;}
  /* 分数徽标 → 白天用柔和色,仍能区分层级 */
  body.light .score-pill{box-shadow:none !important;}
  body.light .s-100{background:#6b8e5e !important;color:#fdfcf8 !important;}
  body.light .s-90{background:#7ba06d !important;color:#fdfcf8 !important;}
  body.light .s-80{background:#8fb082 !important;color:#fdfcf8 !important;}
  body.light .s-70{background:#c08a5b !important;color:#fdfcf8 !important;}
  body.light .s-60{background:#b8a894 !important;color:#fdfcf8 !important;}
  /* 其它零散深块 */
  body.light .sig-badge{background:#e4e1d8 !important;color:#5a6070 !important;}
  body.light .toast{background:#6b8e5e !important;color:#fdfcf8 !important;box-shadow:0 4px 16px rgba(100,80,50,.18) !important;}
  body.light .strat-tbl thead th{background:#ede9dd !important;color:#2d4a6b !important;box-shadow:0 1px 0 #e4e1d8 !important;}
  body.light .top5-card .spark .bar{background:linear-gradient(180deg,#5b8aa8,#4a7793) !important;opacity:1 !important;}
  body.light .top5-card .rank{background:#5b8aa8 !important;color:#fdfcf8 !important;}
  body.light .top5-card .rank.gold{background:linear-gradient(135deg,#c08a5b,#a5734a) !important;}
  body.light .modal{box-shadow:0 8px 32px rgba(100,80,50,.2) !important;}
  body.light .modal button{background:#5b8aa8 !important;color:#fdfcf8 !important;}
  body.light .modal button:hover{background:#4a7793 !important;}
  /* 兜底: main 任何残留 #0b0e14 / #141824 inline */
  body.light main, body.light main > div{background:transparent !important;}

  /* 补全之前遗漏的暗色块覆盖 (黑色残留修复) */
  body.light .s-80{background:#7ba06d !important;color:#fdfcf8 !important;}
  body.light .sig-badge{background:#e4e1d8 !important;color:#5a6070 !important;border:1px solid #d8d4c7;}
  body.light [style*="#166534"]{background:#d8e8d0 !important;color:#3e6b32 !important;}
  body.light [style*="#334155"]{background:#e4e1d8 !important;color:#4a5063 !important;}
  body.light [style*="#3a4256"]{border-color:#d8d4c7 !important;}
  body.light [style*="#0a0d13"]{background:#fbfaf6 !important;color:#3a3f4a !important;border-color:#e4e1d8 !important;}
  body.light [style*="#6a7080"]{color:#7a8090 !important;}
  body.light [style*="color:#8a93a6"]{color:#6a7080 !important;}
  body.light [style*="color:#a3acc0"]{color:#5a6070 !important;}
  body.light [style*="color:#cbd5e1"]{color:#4a5063 !important;}
  body.light [style*="color:#4ade80"]{color:#587849 !important;}
  body.light [style*="color:#fbbf24"]{color:#a5734a !important;}
  body.light [style*="color:#f87171"]{color:#b54a3a !important;}
  body.light [style*="color:#60a5fa"]{color:#4a7793 !important;}
  body.light [style*="color:#4a90ff"]{color:#4a7793 !important;}
  body.light [style*="color:#fde68a"]{color:#8a6a3a !important;}
  /* color:#fff 需要保留在"强色背景"按钮上, 所以只替换纯文字 div/span */
  body.light span[style*="color:#fff"]:not([style*="background"]){color:#3a3f4a !important;}
  body.light div[style*="color:#fff"]:not([style*="background"]){color:#3a3f4a !important;}
  body.light b[style*="color:#fff"]:not([style*="background"]){color:#3a3f4a !important;}
  /* 卡片/模态框内的 <b>, <pre> 柔化 */
  body.light pre{background:#f0eee8 !important;color:#3a3f4a !important;border:1px solid #e4e1d8 !important;}
  body.light #col-picker b, body.light #col-picker span, body.light #col-picker label{color:#3a3f4a !important;}
  body.light #col-picker button{background:#ede9dd !important;color:#3a3f4a !important;}
  /* .modal 内的段落 / 标题 */
  body.light .modal b, body.light .modal h4, body.light .modal span{color:inherit;}
  body.light .modal [style*="color:#e6e6e6"]{color:#2d4a6b !important;}
  body.light .modal [style*="color:#d5d8de"]{color:#3a3f4a !important;}

  main{padding:0;}
  .stats{display:flex;gap:14px;padding:22px 26px 14px;flex-wrap:wrap;background:transparent;}
  .stat{position:relative;background:linear-gradient(135deg,rgba(20,25,38,.9),rgba(13,17,25,.75));padding:18px 22px;border-radius:var(--radius-l);min-width:170px;border:1px solid var(--border-1);box-shadow:var(--shadow-2),inset 0 1px 0 rgba(255,255,255,.03);transition:border-color .2s var(--ease),transform .2s var(--ease),box-shadow .2s var(--ease);overflow:hidden;}
  .stat::before{content:"";position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,var(--brand),transparent);opacity:0;transition:opacity .25s var(--ease);}
  .stat:hover{border-color:var(--border-2);transform:translateY(-2px);box-shadow:var(--shadow-3);}
  .stat:hover::before{opacity:.75;}
  .stat .v{font-size:28px;font-weight:700;color:var(--brand-hi);line-height:1.15;letter-spacing:.2px;font-variant-numeric:tabular-nums;}
  .stat .l{font-size:13px;color:var(--text-2);letter-spacing:.3px;margin-top:6px;font-weight:500;}
  .stat.ok .v{color:var(--ok);}
  .stat.warn .v{color:var(--warn);}
  /* 表格: 清晰层次 + 柔和行高 */
  .tblwrap{padding:0 26px 60px;}
  table{border-collapse:separate;border-spacing:0;font-size:14.5px;background:linear-gradient(180deg,rgba(20,25,38,.6),rgba(13,17,25,.6));min-width:1680px;width:100%;box-shadow:var(--shadow-2);}
  thead th{background:rgba(27,34,50,.95);padding:13px 15px;text-align:left;cursor:pointer;user-select:none;border-bottom:1px solid var(--border-2);font-weight:600;color:var(--text-2);position:sticky;position:-webkit-sticky;top:var(--total-top);z-index:20;white-space:nowrap;box-shadow:0 2px 6px rgba(0,0,0,.28);border-right:1px solid var(--border-1);font-size:13px;letter-spacing:.5px;text-transform:uppercase;transition:background .15s var(--ease),color .15s var(--ease);}
  thead th:hover{background:var(--bg-4);color:var(--text-1);}
  thead th.sort-asc::after{content:" ↑";color:var(--brand-hi);}
  thead th.sort-desc::after{content:" ↓";color:var(--brand-hi);}
  thead th .resizer{position:absolute;right:-3px;top:0;width:7px;height:100%;cursor:col-resize;user-select:none;z-index:21;border-right:1px dashed transparent;transition:border-color .15s var(--ease);}
  thead th .resizer:hover,thead th .resizer:active{border-right:2px solid var(--brand);background:rgba(91,157,255,.15);}
  tbody td{padding:13px 15px;border-bottom:1px solid var(--border-1);vertical-align:middle;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:360px;font-size:14.5px;line-height:1.55;color:var(--text-1);}
  tbody tr{transition:background .15s var(--ease);}
  tbody tr:hover{background:rgba(91,157,255,.05);}
  tbody tr:last-child td{border-bottom:0;}
  /* ── 分数胶囊: 色阶化深浅, 金奖发光 ── */
  .score-pill{display:inline-block;padding:4px 12px;border-radius:999px;font-weight:700;font-size:13px;min-width:38px;text-align:center;letter-spacing:.3px;font-variant-numeric:tabular-nums;}
  .s-100{background:linear-gradient(135deg,#10b981,#059669);color:#fff;box-shadow:0 0 0 1px rgba(16,185,129,.4),0 0 12px rgba(16,185,129,.35);}
  .s-90{background:linear-gradient(135deg,#22c55e,#15803d);color:#fff;box-shadow:0 1px 3px rgba(0,0,0,.2);}
  .s-80{background:linear-gradient(135deg,#4ade80,#166534);color:#052e16;}
  .s-70{background:linear-gradient(135deg,#f59e0b,#854d0e);color:#fff;box-shadow:0 1px 3px rgba(0,0,0,.2);}
  .s-60{background:linear-gradient(135deg,#52525b,#3f3f46);color:#e4e4e7;}
  .shopify{color:var(--ok);font-weight:700;}
  .row-shopify td{background:rgba(34,197,94,.08) !important;}
  .row-shopify td:first-child{box-shadow:inset 3px 0 0 #22c55e;}
  body.light .row-shopify td{background:#f0fdf4 !important;}
  body.light .row-shopify td:first-child{box-shadow:inset 3px 0 0 #16a34a;}
  .tag.shopify-tag{font-weight:800;background:#16a34a;color:#fff;padding:2px 8px;border-radius:6px;letter-spacing:.2px;}
  .date-input{padding:4px 10px;border-radius:12px;background:rgba(255,255,255,.05);color:var(--text-1);border:1px solid var(--border-2);font-size:12.5px;font-variant-numeric:tabular-nums;cursor:pointer;}
  .date-input:hover{border-color:var(--brand);}
  body.light .date-input{background:#fff;color:#2d4a6b;border-color:#d8d4c7;}
  .tier-b{display:inline-block;padding:3px 9px;border-radius:6px;font-size:12.5px;font-weight:600;white-space:nowrap;letter-spacing:.2px;}
  .tier-testing{background:rgba(91,157,255,.15);color:var(--brand-hi);border:1px solid rgba(91,157,255,.25);}
  .tier-validated{background:rgba(251,191,36,.12);color:#fcd34d;border:1px solid rgba(251,191,36,.25);}
  .tier-proven{background:rgba(52,211,153,.12);color:#6ee7b7;border:1px solid rgba(52,211,153,.25);}
  .sig-badge{display:inline-block;padding:2px 8px;background:rgba(168,177,196,.1);color:var(--text-2);border-radius:4px;font-size:12px;margin-right:3px;white-space:nowrap;border:1px solid var(--border-1);}
  .active-dot{color:var(--ok);font-size:16px;line-height:0;}
  a{color:var(--brand-hi);text-decoration:none;transition:color .15s var(--ease);}
  a:hover{color:#fff;text-decoration:underline;}
  .truncate{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .loader{text-align:center;padding:48px;color:var(--text-3);font-size:15px;position:relative;}
  .loader::before{content:"";display:inline-block;width:18px;height:18px;margin-right:10px;border-radius:50%;border:2px solid var(--border-2);border-top-color:var(--brand);vertical-align:middle;animation:spin 0.9s linear infinite;}
  @keyframes spin{to{transform:rotate(360deg);}}
  .empty{text-align:center;padding:48px;color:var(--text-3);font-style:italic;font-size:15px;}
  .top5-banner{padding:20px 26px;background:linear-gradient(135deg,rgba(15,23,42,.85),rgba(30,41,59,.6));border-bottom:1px solid var(--border-1);}
  .top5-title{font-size:16px;font-weight:700;background:linear-gradient(135deg,var(--ok),#a7f3d0);-webkit-background-clip:text;background-clip:text;color:transparent;margin-bottom:14px;display:flex;align-items:center;gap:8px;letter-spacing:.3px;}
  .panel-close{margin-left:auto;background:rgba(255,255,255,.06);color:var(--text-2);border:1px solid var(--border-2);width:28px;height:28px;border-radius:8px;cursor:pointer;font-size:14px;line-height:1;padding:0;transition:background .15s var(--ease),color .15s var(--ease),transform .08s var(--ease);-webkit-text-fill-color:var(--text-2);background-clip:border-box;}
  .panel-close:hover{background:rgba(248,113,113,.2);color:#fca5a5;-webkit-text-fill-color:#fca5a5;}
  .panel-close:active{transform:scale(.94);}
  body.light .panel-close{background:#ede9dd;color:#5a6070;border:1px solid #d8d4c7;-webkit-text-fill-color:#5a6070;}
  body.light .panel-close:hover{background:#e8d8cc !important;color:#8a4e32 !important;-webkit-text-fill-color:#8a4e32 !important;}
  .top5-grid{display:grid;grid-template-columns:repeat(auto-fit, minmax(300px, 1fr));gap:14px;}
  .top5-card{background:linear-gradient(135deg,rgba(20,25,38,.85),rgba(13,17,25,.85));padding:18px;border-radius:var(--radius-l);border:1px solid var(--border-1);position:relative;transition:border-color .2s var(--ease),transform .2s var(--ease),box-shadow .2s var(--ease);box-shadow:var(--shadow-1);}
  .top5-card:hover{border-color:var(--border-2);transform:translateY(-3px);box-shadow:var(--shadow-3);}
  .top5-card .rank{position:absolute;top:-8px;left:-8px;background:linear-gradient(135deg,var(--brand),var(--brand-lo));color:#fff;width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;box-shadow:0 3px 10px var(--brand-glow);}
  .top5-card .rank.gold{background:linear-gradient(135deg,#fbbf24,#d97706);box-shadow:0 3px 14px rgba(251,191,36,.5);}
  .top5-card .brand{font-weight:700;font-size:16px;color:var(--text-1);margin-bottom:6px;padding-left:26px;}
  .top5-card .meta{font-size:12.5px;color:var(--text-2);margin-bottom:8px;padding-left:26px;display:flex;gap:10px;flex-wrap:wrap;}
  .top5-card .trend{display:flex;align-items:center;gap:8px;margin-top:8px;padding-top:8px;border-top:1px dashed var(--border-1);}
  .top5-card .trend-label{font-size:12.5px;color:var(--text-2);}
  .top5-card .spark{display:flex;gap:1px;align-items:flex-end;height:24px;flex:1;}
  .top5-card .spark .bar{flex:1;background:linear-gradient(180deg,var(--brand-hi),var(--brand-lo));min-height:1px;border-radius:2px 2px 0 0;}
  .top5-card .score-row{display:flex;gap:5px;margin-top:6px;padding-left:26px;flex-wrap:wrap;}
  .top5-card .tag{font-size:11.5px;padding:3px 9px;border-radius:999px;background:rgba(91,157,255,.14);color:var(--brand-hi);font-weight:500;border:1px solid rgba(91,157,255,.2);}
  .top5-card .tag.gold{background:rgba(251,191,36,.12);color:#fcd34d;border-color:rgba(251,191,36,.25);}
  .top5-card .tag.up{background:rgba(52,211,153,.12);color:#6ee7b7;border-color:rgba(52,211,153,.25);}
  .top5-card .tag.down{background:rgba(248,113,113,.12);color:#fca5a5;border-color:rgba(248,113,113,.25);}
  .itag{display:inline-block;padding:3px 10px;background:rgba(91,157,255,.14);color:var(--brand-hi);border-radius:999px;font-size:11.5px;margin:1px 2px;white-space:nowrap;border:1px solid rgba(91,157,255,.2);font-weight:500;}
  .pager{display:flex;align-items:center;justify-content:center;gap:7px;padding:14px 26px;background:rgba(11,14,20,.7);border-top:1px solid var(--border-1);position:sticky;bottom:0;flex-wrap:wrap;backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);}
  .pager button{background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);padding:8px 14px;border-radius:var(--radius-s);cursor:pointer;font-size:13.5px;min-width:40px;font-weight:500;transition:background .15s var(--ease),border-color .15s var(--ease),transform .08s var(--ease);}
  .pager button:hover:not(:disabled){background:var(--bg-3);border-color:var(--border-2);}
  .pager button:active:not(:disabled){transform:scale(.96);}
  .pager button.active{background:linear-gradient(135deg,var(--brand),var(--brand-lo));color:#fff;border-color:transparent;box-shadow:0 2px 8px var(--brand-glow);}
  .pager button:disabled{opacity:.3;cursor:not-allowed;}
  .pager input{width:70px;background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);padding:7px 10px;border-radius:var(--radius-s);font-size:13.5px;text-align:center;transition:border-color .15s var(--ease),box-shadow .15s var(--ease);}
  .pager input:focus{outline:none;border-color:var(--brand);box-shadow:var(--ring);}
  .pager .info{color:var(--text-2);font-size:13.5px;padding:0 10px;font-variant-numeric:tabular-nums;}
  .pager select{background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);padding:7px 10px;border-radius:var(--radius-s);font-size:13.5px;cursor:pointer;}
  .linkbtn{background:transparent;border:0;cursor:pointer;font-size:18px;padding:4px 6px;border-radius:6px;transition:background .15s var(--ease);}
  .linkbtn:hover{background:var(--bg-3);}
  .modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:200;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);animation:fadeIn .2s var(--ease);}
  @keyframes fadeIn{from{opacity:0;}to{opacity:1;}}
  .modal{background:linear-gradient(180deg,var(--bg-2),var(--bg-1));padding:30px;border-radius:var(--radius-xl);min-width:420px;max-width:640px;border:1px solid var(--border-2);font-size:15px;box-shadow:0 24px 64px rgba(0,0,0,.5),inset 0 1px 0 rgba(255,255,255,.04);animation:popIn .25s var(--ease);}
  @keyframes popIn{from{opacity:0;transform:scale(.95) translateY(8px);}to{opacity:1;transform:scale(1) translateY(0);}}
  .modal h3{margin:0 0 16px;color:var(--text-1);font-size:18px;font-weight:700;letter-spacing:.2px;}
  .modal .flag{display:inline-block;padding:6px 13px;margin:3px;border-radius:999px;font-weight:600;font-size:13.5px;}
  .flag-covered{background:rgba(52,211,153,.14);color:#6ee7b7;border:1px solid rgba(52,211,153,.25);}
  .flag-missing{background:rgba(248,113,113,.12);color:#fca5a5;border:1px solid rgba(248,113,113,.25);}
  .modal button{background:linear-gradient(135deg,var(--brand),var(--brand-lo));color:#fff;border:0;padding:10px 22px;border-radius:var(--radius-m);margin-top:18px;cursor:pointer;font-size:14.5px;font-weight:600;letter-spacing:.2px;box-shadow:0 2px 10px var(--brand-glow);transition:background .18s var(--ease),box-shadow .18s var(--ease),transform .08s var(--ease);}
  .modal button:hover{background:linear-gradient(135deg,var(--brand-hi),var(--brand));box-shadow:0 4px 18px var(--brand-glow);}
  .modal button:active{transform:translateY(1px);}
  .toast{position:fixed;bottom:22px;right:22px;background:linear-gradient(135deg,#10b981,#059669);color:#fff;padding:13px 22px;border-radius:var(--radius-m);font-size:14.5px;font-weight:500;z-index:100;box-shadow:0 8px 24px rgba(16,185,129,.32),inset 0 1px 0 rgba(255,255,255,.15);animation:slideIn .28s var(--ease);}
  @keyframes slideIn{from{transform:translateX(120%);opacity:0;}to{transform:translateX(0);opacity:1;}}
</style>
</head>
<body>
<div class="topright-actions">
  <div class="btn-row">
    <button class="btn" onclick="toggleTheme()" title="切换白天/夜间" id="themebtn">☀️</button>
    <button class="btn" onclick="load()" title="刷新">🔄</button>
    <button class="btn" onclick="openColPicker()" title="选择显示的列">⚙️</button>
    <div class="sep"></div>
    <button class="btn recs-only" onclick="togglePanel('latest')" title="展示最近 48 小时新入库">📥 最新入库</button>
    <button class="btn recs-only" onclick="togglePanel('top')" title="展示 TOP 50 精选推荐">🏆 TOP 50</button>
    <div class="sep"></div>
    <button class="btn success recs-only" onclick="adminScrape()" title="立即抓取">⚡ 抓取</button>
    <button class="btn recs-only" onclick="adminRebuild()" title="重建推荐">🔨 重建</button>
    <button class="btn recs-only" onclick="harvestKw()" title="从 TikTok / Reddit / Google Trends / Amazon 收割最新趋势词">📈 增榜</button>
    <button class="btn recs-only" onclick="adminEvolve()" title="关键词进化: 学习新词+扩展变体+淘汰陈旧+品类发现">🧬 进化</button>
    <button class="btn recs-only" onclick="openSuggestDialog()" title="从 Amazon 下拉 + Google 自动补全抓取真实搜索长尾词">🔍 增词</button>
    <button class="btn recs-only" onclick="adminStrategies()" title="运行三种策略并打标签">🎯 策略</button>
    <div class="sep"></div>
    <button class="btn primary" onclick="exportCSV()" title="导出 Excel">⬇️ 导出</button>
  </div>
  <div class="status-row"><span class="dot" id="status-dot2"></span><span id="status-text2">加载中...</span></div>
</div>
<header>
  <h1><span class="h1-emoji">📊</span><span class="h1-text">FB Pipeline</span></h1>
  <span class="ver" id="ver">v—</span>
  <span class="badge-mode" title="通过无头 Chromium 渲染页面抓取, 指纹混淆 + 拟人行为">🕶️ 无头浏览器</span>
  <a href="/v2" style="display:inline-block;margin-left:8px;padding:3px 12px;background:linear-gradient(135deg,#8b5cf6,#5b9dff);color:#fff;font-size:12px;font-weight:600;border-radius:6px;text-decoration:none;letter-spacing:.3px" title="2026 v2 顶级信号面板 · velocity · LP 指纹 · Reddit 洞见">🚀 V2 面板</a>
  <div class="status-line"><span class="dot" id="status-dot"></span><span id="status-text">加载中...</span></div>
</header>

<nav class="tabs" id="tabs">
  <button data-tab="recs" class="active">🎯 产品推荐</button>
  <button data-tab="ops">⚙️ 运维状态</button>
</nav>

<div class="toolbar" id="toolbar">
  <input type="text" id="search" placeholder="🔍 搜索...">
  <span class="pill" data-days="1" id="rangeToday">📅 今天</span>
  <span class="pill" data-days="2" id="rangeYest">📆 昨天</span>
  <span class="pill" data-days="7" id="range7">🕐 最近7天</span>
  <span class="pill active" data-days="0" id="rangeAll">📚 全部</span>
  <input type="date" id="date-picker" class="date-input" title="指定日期 (单天)" />
  <label title="排除 Amazon / Temu / eBay / AliExpress / TikTok Shop 等平台,只保留独立站"><input type="checkbox" id="exclude-platforms" checked> 只看独立站</label>
  <button class="btn primary" onclick="openV3AutoDialog()" title="自动抓大平台下拉词,筛选 2026 强趋势关键词,并立即跑 FB 数据">⚡ V3 Auto</button>
  <span class="pill active recs-only" data-tier="all">阶段:全部</span>
  <span class="pill recs-only" data-tier="testing">测试&lt;30d</span>
  <span class="pill recs-only" data-tier="validated">验证30-90d</span>
  <span class="pill recs-only" data-tier="proven">爆品90+d</span>
  <span class="pill recs-only" data-minscore="45">全部分</span>
  <span class="pill active recs-only" data-minscore="60">≥60分</span>
  <span class="pill recs-only" data-minscore="80">≥80分</span>
  <span class="pill recs-only" data-minscore="90">≥90分</span>
  <span class="pill active recs-only" data-strategy="">全部策略</span>
  <span class="pill recs-only" data-strategy="lookalike" style="border-left:3px solid #4ade80;">🔁 爆款反查</span>
  <span class="pill recs-only" data-strategy="paingap" style="border-left:3px solid #fbbf24;">🕳 痛点空白</span>
  <span class="pill recs-only" data-strategy="seasonal" style="border-left:3px solid #93c5fd;">📅 季节提前</span>
  <label class="recs-only"><input type="checkbox" id="shopify-only"> Shopify</label>
  <label class="recs-only"><input type="checkbox" id="active-only"> 活跃</label>
  <label class="recs-only"><input type="checkbox" id="signal-only"> 信号</label>
</div>

<div id="col-picker" style="display:none;position:fixed;top:60px;right:20px;z-index:1000;background:#141824;border:1px solid #2a3142;border-radius:8px;padding:12px;max-height:70vh;overflow-y:auto;min-width:240px;box-shadow:0 8px 32px rgba(0,0,0,.5);">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid #2a3142;">
    <b style="color:#fff;">列显示设置</b>
    <button onclick="document.getElementById('col-picker').style.display='none'" style="background:none;border:none;color:#8a93a6;cursor:pointer;font-size:18px;">✕</button>
  </div>
  <div style="display:flex;gap:8px;margin-bottom:8px;">
    <button class="btn" style="padding:4px 8px;font-size:11px;" onclick="setAllCols(true)">全选</button>
    <button class="btn" style="padding:4px 8px;font-size:11px;" onclick="setAllCols(false)">全隐</button>
    <button class="btn" style="padding:4px 8px;font-size:11px;" onclick="resetCols()">重置</button>
  </div>
  <div id="col-picker-body"></div>
</div>

<main>
  <div id="latest-container"></div>
  <div id="strategies-container"></div>
  <div id="top5-container"></div>
  <div class="stats" id="stats"></div>
  <div id="content"><div class="loader">加载中...</div></div>
</main>

<script>
const state = {tab:'recs', tier:'all', days:0, date:null, excludePlatforms:true, data:[], total:0, page:1, pageSize:200, sortKey:'score', sortDir:-1, health:null, minScore:60, minOrbit:0, kwType:'', strategyTag:''};
// 主题: localStorage 持久化
try{
  const t = localStorage.getItem('theme') || 'dark';
  if(t==='light') document.body.classList.add('light');
  const btn = document.getElementById('themebtn');
  if(btn) btn.textContent = (t==='light'?'🌙':'☀️');
}catch(e){}
function toggleTheme(){
  const b = document.body;
  b.classList.toggle('light');
  const light = b.classList.contains('light');
  try{ localStorage.setItem('theme', light?'light':'dark'); }catch(e){}
  const btn = document.getElementById('themebtn');
  if(btn) btn.textContent = (light?'🌙':'☀️');
}
// 从 localStorage 恢复偏好
try{ const d=parseInt(localStorage.getItem('days')); if(!isNaN(d)) state.days=d; }catch(e){}
try{ const dt=localStorage.getItem('date'); if(dt && /^\d{4}-\d{2}-\d{2}$/.test(dt)) state.date=dt; }catch(e){}
try{ const ep=localStorage.getItem('exclude_platforms'); if(ep!==null) state.excludePlatforms=(ep==='true'); }catch(e){}
try{ const ps=parseInt(localStorage.getItem('pageSize')); if(!isNaN(ps) && ps>=50 && ps<=20000) state.pageSize=ps; }catch(e){}

document.querySelectorAll('nav.tabs button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav.tabs button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  state.tab=b.dataset.tab;
  state.page = 1;
  document.body.classList.remove('non-recs');
  if(state.tab==='recs') { /* 默认状态即为 recs */ }
  else document.body.classList.add('non-recs');
  state.sortKey = {recs:'score',rabbit2026:'rh_score',ops:null}[state.tab];
  state.sortDir = -1;
  load();
});
// 时间范围 pill (今天/昨天/7天/全部)
document.querySelectorAll('.toolbar .pill[data-days]').forEach(p=>{
  p.classList.toggle('active', !state.date && parseInt(p.dataset.days)===state.days);
  p.onclick=()=>{
    document.querySelectorAll('.toolbar .pill[data-days]').forEach(x=>x.classList.remove('active'));
    p.classList.add('active');
    state.days = parseInt(p.dataset.days);
    state.date = null;
    state.page = 1;
    try{localStorage.setItem('days', String(state.days)); localStorage.removeItem('date');}catch(e){}
    const dp = document.getElementById('date-picker'); if(dp) dp.value = '';
    load();
  };
});
// 单日日期选择器 (与 pill 互斥)
(function(){
  const dp = document.getElementById('date-picker');
  if(!dp) return;
  if(state.date) dp.value = state.date;
  dp.onchange = () => {
    const v = dp.value;
    state.date = v || null;
    state.page = 1;
    try{ if(v) localStorage.setItem('date', v); else localStorage.removeItem('date'); }catch(e){}
    if(v) document.querySelectorAll('.toolbar .pill[data-days]').forEach(x=>x.classList.remove('active'));
    load();
  };
})();
// 只看独立站开关 (默认 ON, 过滤 Amazon/Temu/eBay/AliExpress/TikTok Shop 等平台)
(function(){
  const ep = document.getElementById('exclude-platforms');
  if(!ep) return;
  ep.checked = state.excludePlatforms;
  ep.onchange = () => {
    state.excludePlatforms = ep.checked;
    state.page = 1;
    try{ localStorage.setItem('exclude_platforms', String(ep.checked)); }catch(e){}
    load();
  };
})();
// 阶段 pill (all/testing/validated/proven)
document.querySelectorAll('.toolbar .pill[data-tier]').forEach(p=>p.onclick=()=>{
  document.querySelectorAll('.toolbar .pill[data-tier]').forEach(x=>x.classList.remove('active'));
  p.classList.add('active');
  state.tier=p.dataset.tier;
  state.page = 1;
  load();
});
document.querySelectorAll('.toolbar .pill[data-minscore]').forEach(p=>p.onclick=()=>{
  document.querySelectorAll('.toolbar .pill[data-minscore]').forEach(x=>x.classList.remove('active'));
  p.classList.add('active');
  state.minScore = parseInt(p.dataset.minscore);
  state.page = 1;
  load();
});
document.querySelectorAll('.toolbar .pill[data-strategy]').forEach(p=>p.onclick=()=>{
  document.querySelectorAll('.toolbar .pill[data-strategy]').forEach(x=>x.classList.remove('active'));
  p.classList.add('active');
  const tag = p.dataset.strategy || '';
  state.strategyTag = tag;
  state.page = 1;
  // 策略标签自带强过滤, 同步放宽"最低分"和"时间"以避免默认 60分+7天 导致空集
  if(tag){
    state.minScore = 45;
    state.days = 0;
    document.querySelectorAll('.toolbar .pill[data-minscore]').forEach(x=>x.classList.toggle('active', parseInt(x.dataset.minscore)===45));
    document.querySelectorAll('.toolbar .pill[data-days]').forEach(x=>x.classList.toggle('active', parseInt(x.dataset.days)===0));
    toast('已应用策略 "'+p.textContent.trim()+'" · 分数放宽到 ≥45 · 时间 全部');
  }
  load();
});
document.getElementById('search').oninput=debounce(render, 180);
document.getElementById('shopify-only').onchange=()=>{state.page=1;load();};
document.getElementById('active-only').onchange=debounce(render, 100);
document.getElementById('signal-only').onchange=()=>{state.page=1;load();};

async function fetchJSON(url, opts){
  const tries = (opts && opts.tries) || 2;
  const tmo = (opts && opts.timeout) || 8000;
  let lastErr;
  for(let i=0; i<tries; i++){
    const ctl = (window.AbortController ? new AbortController() : null);
    const tid = ctl ? setTimeout(()=>ctl.abort(), tmo) : 0;
    try{
      const r = await fetch(url, {cache:'no-store', signal: ctl?ctl.signal:undefined});
      if(tid) clearTimeout(tid);
      if(!r.ok){
        if(r.status >= 400 && r.status < 500) throw new Error('HTTP '+r.status);
        throw new Error('HTTP '+r.status);
      }
      const total = r.headers.get('X-Total-Count');
      if(total!=null) state.total = parseInt(total);
      return await r.json();
    }catch(e){
      if(tid) clearTimeout(tid);
      lastErr = e;
      if(e.message && /^HTTP 4\d\d/.test(e.message)) break;
      if(i < tries-1){
        const base = 400 * Math.pow(1.5, i);
        const jitter = Math.random() * 200;
        await new Promise(res=>setTimeout(res, base + jitter));
      }
    }
  }
  console.error('fetch failed ('+tries+' tries):', url, lastErr);
  throw lastErr || new Error('unknown');
}

// 防抖: 减少搜索框/过滤器连敲时的并发请求
function debounce(fn, wait){
  let t;
  return function(){
    const a = arguments, c = this;
    clearTimeout(t);
    t = setTimeout(()=>fn.apply(c, a), wait);
  };
}

// 页面级兜底: 不让单次异常炸掉整个脚本
window.addEventListener('error', e=>{ console.warn('global error:', e.message); });
window.addEventListener('unhandledrejection', e=>{ console.warn('unhandled rejection:', e.reason); });
// 断网/回网自动恢复
window.addEventListener('online', ()=>{ toast('🌐 网络已恢复, 自动刷新'); setTimeout(load, 400); });
window.addEventListener('offline', ()=>{ toast('⚠️ 网络已断开', false); });

async function load(silent){
  // SWR: silent=true 时保留旧数据后台刷新, 不闪 loader
  const q = state.tab;
  if(q==='ops'){
    // ops tab 内部已拉 health, 不重复请求
    fetchJSON('/health').then(h=>{state.health=h;updateHealth(h);}).catch(()=>updateHealth(null));
    await renderOps(); state.lastFetched = Date.now(); return;
  }
  try{
    state.health = await fetchJSON('/health');
    updateHealth(state.health);
  }catch(e){updateHealth(null);}

  const only = document.getElementById('shopify-only').checked;
  const dayParam = state.date ? '' : (state.days>0 ? '&days='+state.days : '');
  const dateParam = state.date ? '&date='+encodeURIComponent(state.date) : '';
  const excludeParam = '&exclude_platforms=' + (state.excludePlatforms ? 'true' : 'false');
  const pageParam = '&limit='+state.pageSize+'&offset='+((state.page-1)*state.pageSize);
  let url;
  if(q==='recs'){
    const stratParam = state.strategyTag ? '&strategy_tag='+state.strategyTag : '';
    url = '/recommendations/expert?min_score='+state.minScore+'&tier='+state.tier + (only?'&only_shopify=true':'') + (document.getElementById('signal-only').checked?'&signals_only=true':'') + dayParam + dateParam + excludeParam + pageParam + stratParam;
  }else if(q==='rabbit2026'){
    url = '/recommendations/rabbit2026?min_score='+Math.max(45,state.minScore-15)+pageParam+dayParam+dateParam+excludeParam;
  }
  // SWR: 切 tab / 首次进入清空; 自动刷新保留旧数据
  if(!silent) state.total = 0;
  try{
    const fresh = await fetchJSON(url);
    state.data = Array.isArray(fresh) ? fresh : [];
    state.lastFetched = Date.now();
  }catch(e){
    if(!silent){
      state.data = [];
      toast('数据加载失败: '+e.message, false);
    }else{
      console.warn('silent refresh failed:', e.message);
    }
  }
  renderStats();
  render();
  if(!silent){
    document.getElementById('top5-container').innerHTML='';
    document.getElementById('latest-container').innerHTML='';
  }
}

function togglePanel(kind){
  // 始终重新加载最新数据, 不做 toggle-close 避免"没刷新"困惑
  // 关闭由面板内 ✕ 按钮负责
  if(kind==='latest') renderLatest();
  else renderTop(50);
}
function closePanel(kind){
  const id = kind==='latest' ? 'latest-container' : 'top5-container';
  const el = document.getElementById(id);
  if(el) el.innerHTML = '';
}

async function renderLatest(){
  const el = document.getElementById('latest-container');
  const closeBtn = '<button class="panel-close" onclick="closePanel(\'latest\')" title="关闭">✕</button>';
  el.innerHTML = '<div class="top5-banner"><div class="top5-title">📥 最新入库 · 最近 48 小时新品'+closeBtn+'</div><div style="color:#8a93a6;font-size:13px;">加载中...</div></div>';
  try{
    const rows = await fetchJSON('/recommendations/latest?hours=48&limit=100');
    if(!rows || !rows.length){
      el.innerHTML = '<div class="top5-banner"><div class="top5-title">📥 最新入库'+closeBtn+'</div><div style="color:#8a93a6;font-size:13px;">最近 48 小时暂无新入库</div></div>';
      return;
    }
    const now = Date.now();
    const rel = (t)=>{
      if(!t) return '';
      const d = new Date(t.replace(' ','T'));
      const mins = Math.max(1, Math.floor((now - d.getTime())/60000));
      if(mins < 60) return mins+' 分钟前';
      if(mins < 1440) return Math.floor(mins/60)+' 小时前';
      return Math.floor(mins/1440)+' 天前';
    };
    let h = '<div class="top5-banner"><div class="top5-title">📥 最新入库 · 最近 48 小时新品 ('+rows.length+')'+closeBtn+'</div><div class="top5-grid">';
    rows.forEach((r,i)=>{
      const url = r.landing_url || '#';
      const pain = r.top_pain ? `<span>💢 ${r.top_pain.slice(0,30)}</span>` : '';
      const scene = r.top_scene ? `<span>📍 ${r.top_scene.slice(0,30)}</span>` : '';
      h += `<div class="top5-card">
        <div class="rank">NEW</div>
        <div class="brand"><a href="${url}" target="_blank" rel="noopener">${r.brand_name||r.domain}</a></div>
        <div class="meta">
          <span>${r.domain}</span>
          <span class="score-pill s-${r.score>=90?90:r.score>=80?80:r.score>=70?70:60}">${r.score||0}</span>
          ${r.ad_age_days?`<span>📅 ${r.ad_age_days}d</span>`:''}
          <span style="color:#8a93a6;font-size:11px;">⏱ ${rel(r.first_seen_at)}</span>
          ${pain}${scene}
        </div>
      </div>`;
    });
    h += '</div></div>';
    el.innerHTML = h;
  }catch(e){
    el.innerHTML = '<div class="top5-banner"><div class="top5-title">📥 最新入库'+closeBtn+'</div><div style="color:#f87171;font-size:13px;">加载失败: '+e.message+'</div></div>';
  }
}

async function renderTop(n){
  n = n || 50;
  const el = document.getElementById('top5-container');
  const closeBtn = '<button class="panel-close" onclick="closePanel(\'top\')" title="关闭">✕</button>';
  el.innerHTML = `<div class="top5-banner"><div class="top5-title">🏆 TOP ${n} 精选推荐 · 带 7 天投放动能${closeBtn}</div><div style="color:#8a93a6;font-size:13px;">加载中...</div></div>`;
  try{
    const top = await fetchJSON('/recommendations/top?n='+n);
    if(!Array.isArray(top) || !top.length){
      el.innerHTML = `<div class="top5-banner"><div class="top5-title">🏆 TOP ${n} 精选${closeBtn}</div><div style="color:#8a93a6;font-size:13px;">暂无推荐</div></div>`;
      return;
    }
    const maxSpark = Math.max(...top.flatMap(r=>r.sparkline||[1]), 1);
    let h = `<div class="top5-banner"><div class="top5-title">🏆 TOP ${top.length} 精选推荐 · 综合分+专家分+7天投放动能${closeBtn}</div><div class="top5-grid">`;
    top.forEach((r,i)=>{
      const spark = (r.sparkline||[]).map(v=>`<div class="bar" style="height:${Math.max(1, v/maxSpark*100)}%;opacity:${v>0?1:0.15};"></div>`).join('');
      const mp = r.momentum_pct||0;
      const mpTag = mp>=30?'up':mp<=-30?'down':'';
      const url = r.landing_url || '#';
      const rankCls = i===0?'rank gold':'rank';
      h += `<div class="top5-card">
        <div class="${rankCls}">${i+1}</div>
        <div class="brand"><a href="${url}" target="_blank" rel="noopener">${r.brand_name||r.domain}</a></div>
        <div class="meta">
          <span>${r.domain}</span>
          ${r.price_main?`<span>💰 $${r.price_main}</span>`:''}
          ${r.ad_age_days?`<span>📅 ${r.ad_age_days}d</span>`:''}
          ${r.ads_count?`<span>📊 ${r.ads_count}广告</span>`:''}
        </div>
        <div class="score-row">
          <span class="tag gold">综合 ${r.combined_score}</span>
          <span class="tag">分 ${r.score}</span>
          <span class="tag">专家 ${r.expert_score||0}/10</span>
          ${r.signal_score?`<span class="tag">信号 ${r.signal_score}</span>`:''}
          ${r.is_shopify_shape?'<span class="tag shopify-tag">🛒 Shopify</span>':''}
        </div>
        <div class="trend">
          <span class="trend-label">${r.trend_direction||''} ${mp>0?'+':''}${mp}%</span>
          <div class="spark" title="最近 14 天广告量">${spark}</div>
          <span class="trend-label">7d:${r.ads_7d||0}</span>
        </div>
        <div style="display:flex;gap:4px;margin-top:6px;padding-left:20px;">
          <button class="linkbtn" onclick="showPlaybook('${r.domain}')" title="战术深度">🔍</button>
          <button class="linkbtn" onclick="showCoverage('${r.domain}')" title="国家覆盖">🌍</button>
        </div>
      </div>`;
    });
    h += '</div></div>';
    el.innerHTML = h;
  }catch(e){
    el.innerHTML = '<div class="top5-banner"><div class="top5-title">🏆 TOP 精选'+closeBtn+'</div><div style="color:#f87171;font-size:13px;">加载失败: '+e.message+'</div></div>';
  }
}

let _opsInflight = false;
async function renderOps(){
  if(_opsInflight) return;
  _opsInflight = true;
  document.getElementById('stats').innerHTML = '';
  const ct = document.getElementById('content');
  if(!ct.querySelector('.ops-wrap')) ct.innerHTML = '<div class="loader">运维数据加载中...</div>';
  let html = '<div class="ops-wrap" style="padding:0 24px 40px;">';
  try{
    const [h, ds, cats, qr, lg] = await Promise.all([
      state.health ? Promise.resolve(state.health) : fetchJSON('/health'),
      fetchJSON('/admin/daemon/status'),
      fetchJSON('/categories'),
      fetchJSON('/ops/queue'),
      fetchJSON('/ops/log-tail?n=80'),
    ]);
    const s = h.scraper||{};

    const daemonLabel = ds.running
      ? `🟢 守护进程运行中 (PID ${ds.pid})`
      : (ds.loaded
        ? `🟡 守护进程已加载但未运行${ds.status!=null ? ' (status '+ds.status+')' : ''}`
        : '🔴 守护进程已停止');

    // 一键启停
    html += `<h3 style="color:#e6e6e6;margin:10px 0 8px;">🎛 系统控制</h3>`;
    html += `<div style="display:flex;gap:10px;align-items:center;margin-bottom:18px;">
      <span class="flag ${ds.running?'flag-covered':'flag-missing'}" style="padding:6px 14px;">${daemonLabel}</span>
      <button class="btn success" style="padding:8px 18px;" onclick="daemonStart()">▶ 启动</button>
      <button class="btn" style="padding:8px 18px;background:#7c2d12;color:#fed7aa;" onclick="daemonStop()">⏹ 停止</button>
    </div>`;

    html += `<h3 style="color:#e6e6e6;margin:10px 0 8px;">🛰 扫描器状态</h3>`;
    html += `<div class="stats">
      ${stat('今日',s.daily_quota>0?(s.today_count+'/'+s.daily_quota):(s.today_count+' ∞'))}
      ${stat('累计',s.total_scraped||0,'ok')}
      ${stat('队列',h.queue_pending,(s.daily_quota>0 && s.today_count>=s.daily_quota)?'warn':'ok')}
      ${stat('风控',s.consecutive_challenges||0,(s.consecutive_challenges||0)>0?'warn':'ok')}
      ${stat('熔断',s.in_cooldown?'是':'否',s.in_cooldown?'warn':'ok')}
      ${stat('静默',s.in_quiet_hours?'是':'否')}
      ${stat('最后抓取',s.last_scrape_at?(s.last_scrape_at.slice(11,19)):'—')}
    </div>`;

    html += `<h3 style="color:#e6e6e6;margin:20px 0 8px;">📂 类目覆盖</h3>`;
    html += `<table style="min-width:720px;"><thead><tr><th>优先</th><th>类目</th><th>种子</th><th>已扫</th><th>覆盖</th><th>进度</th></tr></thead><tbody>`;
    for(const c of cats.stats){
      const pct = c.coverage_pct;
      const bar = `<div style="background:#1a2030;border-radius:3px;height:10px;width:160px;overflow:hidden;"><div style="width:${pct}%;height:100%;background:${pct>=80?'#15803d':pct>=40?'#854d0e':'#4a90ff'};"></div></div>`;
      html += `<tr><td>P${c.priority}</td><td>${c.name}</td><td>${c.seeds_total}</td><td>${c.scanned}</td><td>${pct}%</td><td>${bar}</td></tr>`;
    }
    html += '</tbody></table>';

    const cacheHint = qr.cached ? ` <span style="color:#8a93a6;font-size:11px;">(缓存 ${qr.cache_age_sec}s · <a href="#" onclick="refreshOpsQueue();return false;" style="color:#4a90ff;">刷新</a>)</span>` : '';
    html += `<h3 style="color:#e6e6e6;margin:20px 0 8px;">📋 待扫描队列 (${qr.total} 个)${cacheHint}</h3>`;
    html += `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">`;
    for(const t of qr.by_type){
      html += `<span style="background:#1a2030;padding:4px 10px;border-radius:12px;font-size:12px;">${t.type}: <b style="color:#4a90ff;">${t.count}</b></span>`;
    }
    html += '</div>';

    html += `<h3 style="color:#e6e6e6;margin:20px 0 8px;">📜 日志尾部 (${lg.lines.length} 行)</h3>`;
    html += `<pre style="background:#0a0d13;color:#9aa3b6;padding:12px;border-radius:6px;font-size:11px;max-height:360px;overflow:auto;border:1px solid #1e2433;margin:0 0 20px;">`;
    html += lg.lines.map(l=>{
      if(l.includes('|ERROR|'))return `<span style="color:#f87171;">${escapeHtml(l)}</span>`;
      if(l.includes('|WARNING|'))return `<span style="color:#fbbf24;">${escapeHtml(l)}</span>`;
      return escapeHtml(l);
    }).join('\n');
    html += '</pre>';
  }catch(e){
    html += `<div class="empty">加载失败: ${e}</div>`;
  }
  html += '</div>';
  const prev = ct.querySelector('.ops-wrap');
  if(prev){
    const scrollTop = prev.closest('[style*=overflow]')?.scrollTop || window.scrollY;
    ct.innerHTML = html;
    window.scrollTo(0, scrollTop);
  }else{
    ct.innerHTML = html;
  }
  _opsInflight = false;
}
async function refreshOpsQueue(){
  _opsInflight = false;
  try{ await fetchJSON('/ops/queue?force=true'); }catch(e){}
  if(state.tab==='ops') renderOps();
}


function escapeHtml(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

function updateHealth(h){
  const set = (msg, cls) => {
    for(const id of ['status-text','status-text2']){ const el=document.getElementById(id); if(el) el.textContent=msg; }
    for(const id of ['status-dot','status-dot2']){ const el=document.getElementById(id); if(el) el.className='dot '+cls; }
  };
  if(!h){ set('API 不可达','err'); return; }
  document.getElementById('ver').textContent='v'+h.pipeline_version;
  const s = h.scraper||{};
  let cls='ok', msg=`今日 ${s.today_count||0}${s.daily_quota>0?('/'+s.daily_quota):' (无配额 24h)'} · 队列 ${h.queue_pending||0} · 扫描总 ${h.scans_total||0} · 推荐 ${h.recommendations_total||0}`;
  if(s.in_cooldown){cls='err'; msg='🔒 熔断冷却 · 剩余 '+Math.floor((s.cooldown_remaining_sec||0)/60)+'min · '+msg;}
  else if(s.in_quiet_hours){cls='warn'; msg='🌙 静默时段 · '+msg;}
  else if(s.consecutive_challenges>0){cls='warn'; msg='⚠️ 连续 '+s.consecutive_challenges+' 次风控 · '+msg;}
  set(msg, cls);
}



function renderStats(){
  const d = state.data;
  const T = (state.total||d.length).toLocaleString();
  let html='';
  if(state.tab==='recs'){
    const t={testing:0,validated:0,proven:0};
    d.forEach(r=>{if(r.ad_age_days<30)t.testing++; else if(r.ad_age_days<90)t.validated++; else t.proven++;});
    html = stat('推荐总数',T)+stat('当前页',d.length)+stat('爆品(90+d)',t.proven,'ok')+stat('验证(30-90d)',t.validated)+stat('测试(<30d)',t.testing,'warn')+stat('Shopify',d.filter(r=>r.is_shopify_shape).length,'ok')+stat('活跃中',d.filter(r=>r.is_active).length,'ok');
  }else if(state.tab==='rabbit2026'){
    html = stat('2026 候选',T)+stat('当前页',d.length)+stat('同窝≥5',d.filter(r=>(r.rh_siblings||0)>=5).length,'ok')+stat('爆品(90+d)',d.filter(r=>(r.ad_age_days||0)>=90).length,'ok');
  }
  document.getElementById('stats').innerHTML=html;
}
function stat(l,v,cls){return `<div class="stat ${cls||''}"><div class="v">${v}</div><div class="l">${l}</div></div>`;}

function scorePill(s){
  const cls=s>=100?'s-100':s>=90?'s-90':s>=80?'s-80':s>=70?'s-70':'s-60';
  return `<span class="score-pill ${cls}">${s}</span>`;
}
function tierBadge(age){
  if(age<30)return `<span class="tier-b tier-testing">测试 ${age}d</span>`;
  if(age<90)return `<span class="tier-b tier-validated">验证 ${age}d</span>`;
  return `<span class="tier-b tier-proven">爆品 ${age}d</span>`;
}
function toast(msg,ok=true){
  const t=document.createElement('div');
  t.className='toast'; t.style.background=ok?'#15803d':'#b91c1c';
  t.textContent=msg; document.body.appendChild(t);
  setTimeout(()=>t.remove(),2500);
}
async function adminScrape(){
  try{const r=await fetch('/admin/scrape?limit=3',{method:'POST'});const j=await r.json();toast('已启动后台采集 ('+j.limit+' 个)');setTimeout(load,3000);}
  catch(e){toast('启动失败: '+e,false);}
}
async function adminRebuild(){
  try{await fetch('/admin/rebuild-intel',{method:'POST'});toast('重建已启动, 稍后自动刷新');setTimeout(load,8000);}
  catch(e){toast('重建失败: '+e,false);}
}
async function harvestKw(){
  toast('📈 收割趋势词中... (TikTok/Reddit/Google/Amazon)');
  try{
    const r = await fetch('/admin/harvest-keywords?target=200&force=true',{method:'POST'});
    const j = await r.json();
    if(j.error){ toast('收割失败: '+j.error, false); return; }
    if(j.skipped){ toast('队列充足('+j.queue_size+'词), 未触发', true); return; }
    const added = j.added||0;
    const src = j.sources ? Object.entries(j.sources).map(([k,v])=>k+':'+v).join(' · ') : '';
    toast('✅ 入库 '+added+' 词'+(src?' · '+src:''));
    setTimeout(load, 2000);
  }catch(e){ toast('收割失败: '+e, false); }
}
async function adminEvolve(){
  toast('🧬 5层兔子洞进化中...');
  try{
    const r = await fetch('/admin/evolve-keywords',{method:'POST'});
    const j = await r.json();
    if(j.error){ toast('进化失败: '+j.error, false); return; }
    const h = j.harvested?.count||0, e = j.expanded?.count||0;
    const ih = j.ingested?.harvest_added||0, ie = j.ingested?.expand_added||0;
    const ret = j.retired?.count||0, cl = j.clusters?.count||0;
    if(h===0 && e===0){
      toast('✓ 系统已学饱和 · 等待新扫描数据产生种子 · 新品类建议 '+cl+' 个');
    }else{
      const bl = j.expanded?.by_layer || {};
      const layerStr = Object.entries(bl).map(([k,v])=>`${k}:${v}`).join(' · ') || '—';
      toast(`🧬 进化完成 · 学习 ${h} · 10层扩展 ${e} (${layerStr}) · 入库 ${ih+ie} · 淘汰 ${ret} · 新品类 ${cl}`);
    }
    setTimeout(load, 2500);
  }catch(e){ toast('进化失败: '+e,false); }
}

function openV3AutoDialog(){
  const modal = document.createElement('div');
  modal.className = 'modal-bg';
  modal.innerHTML = `<div class="modal" style="min-width:720px;max-width:980px;max-height:90vh;overflow-y:auto;">
    <h3>⚡ V3 Auto · 2026 趋势关键词自动跑数据</h3>
    <p style="color:#8a93a6;font-size:13px;margin:0 0 12px;">并发抓 Amazon / Google / Bing / DuckDuckGo / YouTube / Wikipedia 下拉词，筛选强趋势关键词后立即跑 FB Ad Library。</p>
    <textarea id="v3-seeds" style="width:100%;height:96px;background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:8px;padding:10px;font-family:inherit;" placeholder="vagus nerve, red light therapy, glp-1 alternative"></textarea>
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0;">
      <label style="font-size:12px;color:#8a93a6;">Top N<input id="v3-topn" type="number" value="30" min="1" max="100" style="width:100%;margin-top:4px;background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:6px;padding:7px;"></label>
      <label style="font-size:12px;color:#8a93a6;">国家<input id="v3-country" type="text" value="US" maxlength="2" style="width:100%;margin-top:4px;background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:6px;padding:7px;"></label>
      <label style="font-size:12px;color:#8a93a6;">滚动次数<input id="v3-scrolls" type="number" value="5" min="1" max="20" style="width:100%;margin-top:4px;background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:6px;padding:7px;"></label>
      <label style="font-size:12px;color:#8a93a6;">刷新间隔<input id="v3-refresh" type="number" value="10" min="0" max="50" style="width:100%;margin-top:4px;background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:6px;padding:7px;"></label>
    </div>
    <div style="display:flex;gap:14px;align-items:center;margin:10px 0 14px;font-size:13px;color:var(--text-1);flex-wrap:wrap;">
      <label><input type="checkbox" id="v3-alphabet" checked> A-Z 下拉爆破</label>
      <label><input type="checkbox" id="v3-fast" checked> 快速模式</label>
      <label><input type="checkbox" id="v3-ingest" checked> 入关键词库</label>
      <label><input type="checkbox" id="v3-process" checked> 抓完自动处理</label>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:14px;"><button class="btn primary" onclick="runV3Auto()">⚡ 开始自动跑</button><button class="btn" onclick="this.closest('.modal-bg').remove()">关闭</button></div>
    <div id="v3-auto-result" style="display:none;"></div>
  </div>`;
  document.body.appendChild(modal);
}
async function runV3Auto(){
  const result = document.getElementById('v3-auto-result');
  const payload = {
    text: document.getElementById('v3-seeds').value.trim(),
    alphabet: document.getElementById('v3-alphabet').checked,
    top_n: parseInt(document.getElementById('v3-topn').value || '30'),
    country: document.getElementById('v3-country').value.trim().toUpperCase() || 'US',
    max_scrolls: parseInt(document.getElementById('v3-scrolls').value || '5'),
    refresh_every: parseInt(document.getElementById('v3-refresh').value || '10'),
    fast: document.getElementById('v3-fast').checked,
    ingest: document.getElementById('v3-ingest').checked,
    process: document.getElementById('v3-process').checked,
    delay_sec: 0.15,
  };
  result.style.display='block';
  result.innerHTML = `<div class="loader">⚡ V3 Auto 运行中：抓下拉词 → 2026 排序 → FB 抓取 → 处理入库...</div>`;
  toast('V3 Auto 已启动，等待结果...');
  try{
    const r = await fetch('/v3/auto', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    const j = await r.json();
    if(j.error || j.ok===false){ result.innerHTML = `<div class="empty" style="color:#f87171;">运行失败: ${escapeHtml(j.detail||j.error||JSON.stringify(j))}</div>`; toast('V3 Auto 失败', false); return; }
    renderV3AutoResult(j);
    toast(`V3 Auto 完成 · 生成 ${j.generated_count||0} · 选中 ${j.selected_count||0} · 保存 ${j.scrape?.saved||0}`);
    setTimeout(load, 1500);
  }catch(e){
    result.innerHTML = `<div class="empty" style="color:#f87171;">运行失败: ${escapeHtml(e.message||e)}</div>`;
    toast('V3 Auto 失败: '+e, false);
  }
}
function renderV3AutoResult(j){
  const result = document.getElementById('v3-auto-result');
  const kws = j.top_keywords || [];
  const rows = j.scrape?.results || [];
  let html = `<div style="border-top:1px solid var(--border-1);padding-top:12px;">`;
  html += `<div class="stats" style="padding:0;margin:0 0 12px;">${stat('生成词',j.generated_count||0,'ok')}${stat('选中',j.selected_count||0,'ok')}${stat('入库',j.ingested||0)}${stat('保存',j.scrape?.saved||0,(j.scrape?.saved||0)>0?'ok':'warn')}${stat('耗时(s)',j.scrape?.runtime_s||'—')}</div>`;
  html += `<h4 style="color:#e6e6e6;margin:10px 0 6px;">🔥 2026 Top Keywords</h4><div style="max-height:260px;overflow:auto;border:1px solid var(--border-1);border-radius:8px;">`;
  kws.forEach(k=>{ html += `<div style="display:flex;gap:8px;align-items:center;padding:7px 10px;border-bottom:1px dashed var(--border-1);font-size:13px;"><span class="score-pill ${k.trend_score>=80?'s-90':k.trend_score>=60?'s-80':'s-70'}">${k.trend_score||0}</span><b style="flex:1;">${escapeHtml(k.keyword)}</b><span class="itag">${escapeHtml(k.kw_type||'product')}</span><span style="color:#8a93a6;font-size:11px;">${escapeHtml((k.sources||[]).join(', '))}</span></div>`; });
  html += `</div><h4 style="color:#e6e6e6;margin:14px 0 6px;">📡 抓取结果</h4><table style="min-width:720px;"><thead><tr><th>关键词</th><th>状态</th><th>广告数</th><th>耗时</th><th>文件</th></tr></thead><tbody>`;
  rows.forEach(r=>{ html += `<tr><td>${escapeHtml(r.keyword||'')}</td><td>${r.ok?'✅':'⚠️'} ${escapeHtml(r.status||'')}</td><td>${r.count||0}</td><td>${r.duration_s||0}s</td><td style="font-size:11px;color:#8a93a6;">${escapeHtml(r.file||'—')}</td></tr>`; });
  html += `</tbody></table></div>`;
  result.innerHTML = html;
}

// 🔍 增词: 从 Amazon + Google 下拉抓取真实搜索长尾
function openSuggestDialog(){
  const seed = (prompt('输入种子词 (英文, 如 "knee pain"):') || '').trim();
  if(!seed) return;
  const alphabet = confirm('是否启用 A-Z 长尾爆破?\n\n确定 = 开启 (约 15 秒, 52 次请求, 产出 100-300 长尾词)\n取消 = 快速模式 (2 次请求, 即时返回 10-20 词)');
  fetchAndShowSuggestions(seed, alphabet);
}
async function fetchAndShowSuggestions(seed, alphabet){
  const modal = document.createElement('div');
  modal.className = 'modal-bg';
  modal.innerHTML = `<div class="modal" style="min-width:620px;max-width:820px;max-height:88vh;overflow-y:auto;"><h3>🔍 6 平台抓取中...</h3><div class="loader">Amazon · Google · Bing · DuckDuckGo · YouTube · Wikipedia ${alphabet?'(A-Z 爆破, ~90s)':'(快速)'}</div></div>`;
  document.body.appendChild(modal);
  const PLAT_BADGE = {
    amazon:   {label:'🛒 AMZ', bg:'#ff9900', fg:'#000'},
    google:   {label:'🔍 GG',  bg:'#4285f4', fg:'#fff'},
    bing:     {label:'Ⓑ BING',bg:'#008373', fg:'#fff'},
    ddg:      {label:'🦆 DDG', bg:'#de5833', fg:'#fff'},
    youtube:  {label:'▶ YT',   bg:'#ff0000', fg:'#fff'},
    wikipedia:{label:'W WIKI', bg:'#000',    fg:'#fff'},
  };
  const TYPE_CLR = {pain:'#f87171', scene:'#60a5fa', product:'#4ade80', brand:'#fbbf24'};
  try{
    const r = await fetch('/admin/fetch-suggestions?seed='+encodeURIComponent(seed)+'&alphabet='+alphabet);
    const j = await r.json();
    if(j.error){ modal.querySelector('.modal').innerHTML = `<h3>❌ 抓取失败</h3><p>${j.error}</p><button class="btn" onclick="this.closest('.modal-bg').remove()">关闭</button>`; return; }
    const combined = j.combined || [];
    const already = new Set(j.already || []);
    const newOnes = combined.filter(k=>!already.has(k));
    const pc = j.platform_counts || {};
    let body = `<h3>🔍 「${j.seed}」 6 平台下拉结果</h3>`;
    body += `<div style="color:#8a93a6;font-size:12.5px;margin-bottom:12px;display:flex;gap:10px;flex-wrap:wrap;">`;
    Object.entries(PLAT_BADGE).forEach(([k,v])=>{
      body += `<span class="itag" style="background:${v.bg};color:${v.fg};">${v.label}: ${pc[k]||0}</span>`;
    });
    body += `<span style="margin-left:auto;">合并去重: <b style="color:#4ade80;">${newOnes.length} 新</b> + ${already.size} 已入库</span></div>`;
    body += `<div style="display:flex;gap:8px;margin-bottom:10px;">
      <button class="btn primary" onclick="bulkSelectSuggests(true)">全选新词</button>
      <button class="btn" onclick="bulkSelectSuggests(false)">全不选</button>
      <button class="btn success" onclick="ingestSelectedSuggests()">入库选中 (自动分类)</button>
    </div>`;
    body += `<div id="suggest-list" style="max-height:52vh;overflow-y:auto;border:1px solid var(--border-1);border-radius:8px;padding:10px;">`;
    newOnes.forEach(kw=>{
      const srcs = (j.kw_sources||{})[kw] || [];
      const cls = (j.classified||{})[kw] || {kw_type:'product', subtype:''};
      const srcBadges = srcs.map(s=>{
        const b = PLAT_BADGE[s] || {label:s,bg:'#666',fg:'#fff'};
        return `<span class="itag" style="background:${b.bg};color:${b.fg};font-size:10px;padding:1px 5px;">${b.label}</span>`;
      }).join('');
      const typeClr = TYPE_CLR[cls.kw_type] || '#8a93a6';
      body += `<label style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px dashed var(--border-1);font-size:14px;cursor:pointer;">
        <input type="checkbox" class="sg-cb" data-kw="${kw.replace(/"/g,'&quot;')}" checked>
        <span style="flex:1;">${kw}</span>
        <span class="itag" style="background:${typeClr};color:#000;font-weight:600;" title="${cls.subtype}">${cls.kw_type}</span>
        <span style="display:flex;gap:3px;">${srcBadges}</span>
      </label>`;
    });
    if(already.size){
      body += `<details style="margin-top:12px;"><summary style="cursor:pointer;color:#8a93a6;">已入库 ${already.size} 个</summary>`;
      (j.already||[]).forEach(kw=>{ body += `<div style="padding:3px 0;color:#6a7080;font-size:13px;">· ${kw}</div>`; });
      body += `</details>`;
    }
    body += `</div><button class="btn" style="margin-top:12px;" onclick="this.closest('.modal-bg').remove()">关闭</button>`;
    modal.querySelector('.modal').innerHTML = body;
  }catch(e){
    modal.querySelector('.modal').innerHTML = `<h3>❌ 抓取失败</h3><p>${e.message||e}</p><button class="btn" onclick="this.closest('.modal-bg').remove()">关闭</button>`;
  }
}
function bulkSelectSuggests(on){
  document.querySelectorAll('.sg-cb').forEach(cb=>cb.checked=on);
}
async function ingestSelectedSuggests(){
  const kws = [...document.querySelectorAll('.sg-cb')].filter(c=>c.checked).map(c=>c.dataset.kw);
  if(!kws.length){ toast('未选中任何关键词',false); return; }
  try{
    const r = await fetch('/admin/ingest-suggestions',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({keywords: kws, source:'amazon+google'}),
    });
    const j = await r.json();
    if(j.error){ toast('入库失败: '+j.error, false); return; }
    toast(`✓ 入库 ${j.added}/${j.requested} 个 · 下轮 daemon 会扫描`);
    const modal = document.querySelector('.modal-bg'); if(modal) modal.remove();
    setTimeout(load, 1500);
  }catch(e){ toast('入库失败: '+e,false); }
}

// 📥 导入新词: 自动读剪贴板 + 实时计数 + 一键入队
async function openImportKeywords(){
  let clipText = '';
  try{
    if(navigator.clipboard && navigator.clipboard.readText){
      clipText = await navigator.clipboard.readText();
      clipText = (clipText || '').slice(0, 50000);
    }
  }catch(e){ /* 无权限时静默, 用户可手动粘贴 */ }

  const modal = document.createElement('div');
  modal.className = 'modal-bg';
  modal.innerHTML = `<div class="modal" style="min-width:620px;max-width:820px;">
    <h3 style="display:flex;align-items:center;gap:10px;">📥 批量导入关键词 <span id="imp-count" style="margin-left:auto;font-size:13px;color:#4ade80;font-weight:500;">0 个</span></h3>
    <div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;">
      <button class="btn" onclick="impPaste()" title="从剪贴板读入">📋 粘贴剪贴板</button>
      <button class="btn" onclick="impLoadFile()" title="从 .txt/.csv 文件加载">📁 加载文件</button>
      <button class="btn" onclick="impClear()">🗑 清空</button>
      <input type="file" id="imp-file" accept=".txt,.csv,.md" style="display:none;" onchange="impFileChanged(event)">
    </div>
    <p style="color:#8a93a6;font-size:12.5px;margin:0 0 8px;">支持换行/逗号/分号/竖线/制表符分隔 · 已自动读取剪贴板</p>
    <textarea id="imp-text" style="width:100%;min-height:240px;background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:8px;padding:10px;font-size:13.5px;font-family:monospace;line-height:1.5;" placeholder="knee massager&#10;red light therapy&#10;vagus nerve stimulator, cold plunge ..." oninput="impUpdateCount()"></textarea>
    <div style="display:flex;gap:14px;align-items:center;margin:12px 0;font-size:13.5px;color:var(--text-1);flex-wrap:wrap;">
      <label><input type="checkbox" id="imp-queue" checked> 写入扫描队列 (优先级 P1)</label>
      <label><input type="checkbox" id="imp-learned" checked> 入 learned_keywords</label>
      <input type="text" id="imp-cat" placeholder="分类标签 (可选)" style="flex:1;min-width:160px;background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:6px;padding:6px 10px;font-size:13px;">
    </div>
    <div style="display:flex;gap:8px;">
      <button class="btn primary" onclick="submitImportKeywords()">📥 导入并入队</button>
      <button class="btn" onclick="this.closest('.modal-bg').remove()">取消</button>
    </div>
  </div>`;
  document.body.appendChild(modal);
  if(clipText){
    document.getElementById('imp-text').value = clipText;
    impUpdateCount();
    toast('✓ 剪贴板已自动填入, 点击导入即可');
  }
  setTimeout(()=>document.getElementById('imp-text')?.focus(), 50);
}
function impUpdateCount(){
  const text = (document.getElementById('imp-text')||{}).value || '';
  const tokens = text.split(/[\n\r,;\t|]+/).map(t=>t.trim().toLowerCase().replace(/^[-\s]+|[-\s]+$/g,''));
  const uniq = new Set(tokens.filter(t=>t.length>=3 && t.length<=80));
  const el = document.getElementById('imp-count');
  if(el) el.textContent = `${uniq.size} 个新词`;
}
async function impPaste(){
  try{
    const txt = await navigator.clipboard.readText();
    const ta = document.getElementById('imp-text');
    ta.value = (ta.value ? ta.value + '\n' : '') + (txt || '');
    impUpdateCount();
    toast('✓ 剪贴板已追加');
  }catch(e){ toast('无法读剪贴板, 请手动粘贴', false); }
}
function impLoadFile(){ document.getElementById('imp-file').click(); }
function impFileChanged(ev){
  const f = ev.target.files[0]; if(!f) return;
  const r = new FileReader();
  r.onload = e => {
    const ta = document.getElementById('imp-text');
    ta.value = (ta.value ? ta.value + '\n' : '') + e.target.result;
    impUpdateCount();
    toast('✓ 文件已加载: '+f.name);
  };
  r.readAsText(f);
}
function impClear(){
  document.getElementById('imp-text').value = '';
  impUpdateCount();
}
async function submitImportKeywords(){
  const text = document.getElementById('imp-text').value.trim();
  if(!text){ toast('请输入或粘贴关键词',false); return; }
  const to_queue = document.getElementById('imp-queue').checked;
  const to_learned = document.getElementById('imp-learned').checked;
  const category = document.getElementById('imp-cat').value.trim() || 'manual-import';
  try{
    const r = await fetch('/admin/import-keywords',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text, to_queue, to_learned, category}),
    });
    const j = await r.json();
    if(j.error){ toast('导入失败: '+j.error, false); return; }
    toast(`✓ 解析 ${j.parsed} · 入队 ${j.queued} (P1 优先) · 入库 ${j.learned_added} · 下轮立即扫描`);
    document.querySelector('.modal-bg')?.remove();
    setTimeout(load, 1000);
  }catch(e){ toast('导入失败: '+e,false); }
}

// 🏷 新品类: 自定义 或 从聚类建议选
async function openNewCategory(){
  const modal = document.createElement('div');
  modal.className = 'modal-bg';
  modal.innerHTML = `<div class="modal" style="min-width:620px;max-width:780px;max-height:88vh;overflow-y:auto;"><h3>🏷 新品类</h3><div class="loader">加载聚类建议中...</div></div>`;
  document.body.appendChild(modal);
  try{
    // 拿聚类建议 (evolve dry_run)
    const r = await fetch('/admin/evolve-keywords?dry_run=true',{method:'POST'});
    const j = await r.json();
    const clusters = j.clusters?.suggestions || [];
    let body = `<h3>🏷 新增品类</h3>
      <p style="color:#8a93a6;font-size:13px;margin:0 0 10px;">选一个聚类建议自动填, 或手动输入</p>
      <details style="margin-bottom:12px;"><summary style="cursor:pointer;color:var(--brand-hi);">💡 ${clusters.length} 个聚类建议 (点击展开)</summary>
      <div style="max-height:200px;overflow-y:auto;margin-top:8px;">`;
    clusters.forEach(c=>{
      const seedsJson = JSON.stringify(c.seed_keywords||[]).replace(/"/g,'&quot;');
      body += `<div style="padding:6px 0;border-bottom:1px dashed var(--border-1);">
        <button class="btn" style="padding:4px 10px;font-size:12px;" onclick="fillCategoryForm('${c.cluster_token}', '${c.proposed_category}', ${seedsJson.replace(/'/g,"\\'")})">选中</button>
        <b>${c.cluster_token}</b> × ${c.size} 个 · 建议 <code>${c.proposed_category}</code>
        <div style="font-size:12px;color:#8a93a6;">种子: ${(c.seed_keywords||[]).slice(0,5).join(', ')}</div>
      </div>`;
    });
    body += `</div></details>`;
    body += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px;">
      <input type="text" id="cat-id" placeholder="id (如: vagus-health)" style="background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:6px;padding:8px 10px;">
      <input type="text" id="cat-name" placeholder="名称 (如: 迷走神经系列)" style="background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:6px;padding:8px 10px;">
      <select id="cat-priority" style="background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:6px;padding:8px 10px;">
        <option value="1">P1 最高优先</option><option value="2">P2 高</option>
        <option value="3" selected>P3 中</option><option value="5">P5 低</option>
      </select>
      <select id="cat-tier" style="background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:6px;padding:8px 10px;">
        <option value="high_ticket">高客单 $80+</option>
        <option value="mid_ticket" selected>中客单 $30-80</option>
        <option value="low_ticket">低客单 &lt;$30</option>
      </select>
    </div>
    <textarea id="cat-seeds" placeholder="种子关键词, 每行一个" style="width:100%;min-height:120px;background:var(--bg-2);color:var(--text-1);border:1px solid var(--border-1);border-radius:8px;padding:10px;font-size:13.5px;font-family:monospace;"></textarea>
    <div style="display:flex;gap:8px;margin-top:12px;">
      <button class="btn primary" onclick="submitNewCategory()">创建品类</button>
      <button class="btn" onclick="this.closest('.modal-bg').remove()">取消</button>
    </div>`;
    modal.querySelector('.modal').innerHTML = body;
  }catch(e){
    modal.querySelector('.modal').innerHTML = `<h3>❌ 加载失败</h3><p>${e.message||e}</p><button class="btn" onclick="this.closest('.modal-bg').remove()">关闭</button>`;
  }
}
function fillCategoryForm(token, propId, seeds){
  document.getElementById('cat-id').value = propId;
  document.getElementById('cat-name').value = token.replace(/\b\w/g,c=>c.toUpperCase())+' 系列';
  document.getElementById('cat-seeds').value = seeds.join('\n');
}
async function submitNewCategory(){
  const id = document.getElementById('cat-id').value.trim();
  const name = document.getElementById('cat-name').value.trim() || id;
  const priority = parseInt(document.getElementById('cat-priority').value);
  const tier = document.getElementById('cat-tier').value;
  const seeds = document.getElementById('cat-seeds').value.split('\n').map(s=>s.trim()).filter(Boolean);
  if(!id){ toast('id 必填', false); return; }
  if(!seeds.length){ toast('种子关键词必填', false); return; }
  try{
    const r = await fetch('/categories/custom',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id, name, priority, tier, seeds}),
    });
    const j = await r.json();
    if(!j.ok){ toast('创建失败: '+(j.detail||j.error||'未知'),false); return; }
    toast(`✓ 品类 "${name}" ${j.action} · 入库种子 ${j.ingested||0} 个`);
    document.querySelector('.modal-bg')?.remove();
    setTimeout(load, 1200);
  }catch(e){ toast('创建失败: '+e,false); }
}

// 🕳 扩展方案: 输入种子词, 看 10 层兔子洞预览 + 入库
function openExpansionPlan(){
  const seed = (prompt('输入种子词看 10 层扩展预览:') || '').trim();
  if(!seed) return;
  showExpansionPreview(seed);
}
async function showExpansionPreview(seed){
  const modal = document.createElement('div');
  modal.className = 'modal-bg';
  modal.innerHTML = `<div class="modal" style="min-width:700px;max-width:900px;max-height:90vh;overflow-y:auto;"><h3>🕳 「${seed}」 10 层扩展方案</h3><div class="loader">分析中...</div></div>`;
  document.body.appendChild(modal);
  const LAYER_META = {
    L2:{n:'属性', clr:'#7dd3fc', desc:'程度/频次/病程修饰'},
    L3:{n:'场景', clr:'#93c5fd', desc:'动作/时间/语境'},
    L4:{n:'人群', clr:'#c4b5fd', desc:'年龄/职业/画像'},
    L5:{n:'根因', clr:'#fda4af', desc:'并发症/条件'},
    L6:{n:'问句',   clr:'#fcd34d', desc:'Voice/GenZ 搜索'},
    L7:{n:'TikTok', clr:'#ff69b4', desc:'短视频钩子'},
    L8:{n:'身份',   clr:'#f0abfc', desc:'情绪/社群'},
    L9:{n:'2026',   clr:'#86efac', desc:'健康新概念'},
    L10:{n:'清洁',   clr:'#a7f3d0', desc:'原料/生态'},
    L11:{n:'时限',   clr:'#fdba74', desc:'快见效承诺'},
  };
  try{
    const r = await fetch('/admin/expand-seed?seed='+encodeURIComponent(seed));
    const j = await r.json();
    if(j.error){ modal.querySelector('.modal').innerHTML = `<h3>❌ 失败</h3><p>${j.error}</p><button class="btn" onclick="this.closest('.modal-bg').remove()">关闭</button>`; return; }
    const layers = j.layers || {};
    const total = Object.values(layers).reduce((a,v)=>a+v.length,0);
    let body = `<h3>🕳 「${seed}」 10 层扩展方案</h3>
      <div style="color:#8a93a6;font-size:13px;margin-bottom:12px;">共 ${total} 个变体 · 跨 ${Object.keys(layers).length} 层</div>
      <div style="display:flex;gap:8px;margin-bottom:10px;">
        <button class="btn primary" onclick="ingestAllExpansion(${JSON.stringify(seed).replace(/"/g,'&quot;')})">全部入库</button>
        <button class="btn" onclick="this.closest('.modal-bg').remove()">关闭</button>
      </div>`;
    for(const [key, kws] of Object.entries(layers)){
      const m = LAYER_META[key] || {n:key, clr:'#888', desc:''};
      body += `<div style="margin-bottom:10px;border:1px solid var(--border-1);border-radius:8px;padding:10px;">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">
          <span class="itag" style="background:${m.clr};color:#000;font-weight:600;">${key} ${m.n}</span>
          <span style="color:#8a93a6;font-size:12px;">${m.desc} · ${kws.length} 个</span>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:4px;">`;
      kws.forEach(kw=>{
        body += `<span class="itag" style="background:var(--bg-3);font-size:11.5px;">${kw}</span>`;
      });
      body += `</div></div>`;
    }
    modal.querySelector('.modal').innerHTML = body;
  }catch(e){
    modal.querySelector('.modal').innerHTML = `<h3>❌ 失败</h3><p>${e.message||e}</p><button class="btn" onclick="this.closest('.modal-bg').remove()">关闭</button>`;
  }
}
async function ingestAllExpansion(seed){
  try{
    const r = await fetch('/admin/expand-seed?seed='+encodeURIComponent(seed));
    const j = await r.json();
    const all = [];
    Object.values(j.layers||{}).forEach(kws => all.push(...kws));
    if(!all.length){ toast('无变体可入库',false); return; }
    const ir = await fetch('/admin/ingest-suggestions',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({keywords: all, source:`rabbit-hole:${seed}`}),
    });
    const ij = await ir.json();
    toast(`✓ 「${seed}」 10 层扩展入库 ${ij.added}/${ij.requested}`);
    document.querySelector('.modal-bg')?.remove();
    setTimeout(load, 1200);
  }catch(e){ toast('入库失败: '+e,false); }
}
async function adminStrategies(){
  toast('🎯 策略执行中...');
  try{
    const r = await fetch('/admin/run-strategies',{method:'POST'});
    const j = await r.json();
    const t = j.tagged || {};
    toast(`✓ 已打标签: 爆款反查 ${t.lookalike||0} · 痛点空白 ${t.paingap||0} · 季节 ${t.seasonal||0}`);
    setTimeout(load, 800);
  }catch(e){toast('策略失败: '+e,false);}
}

function filterStrategy(k){
  // 兼容老函数名, 转到 data-strategy pill
  const el = document.querySelector('.toolbar .pill[data-strategy="'+(k==='all'?'':k)+'"]');
  if(el) el.click();
}
async function daemonStart(){
  try{const r=await fetch('/admin/daemon/start',{method:'POST'});const j=await r.json();toast(j.ok?'✓ 守护进程已启动':'启动失败: '+(j.error||''),j.ok);setTimeout(load,2000);}
  catch(e){toast('启动失败: '+e,false);}
}
async function daemonStop(){
  if(!confirm('确定停止守护进程吗? 停止后将不再自动抓取.')) return;
  try{const r=await fetch('/admin/daemon/stop',{method:'POST'});const j=await r.json();toast(j.ok?'✓ 守护进程已停止':'停止失败: '+(j.error||''),j.ok);setTimeout(load,2000);}
  catch(e){toast('停止失败: '+e,false);}
}

async function showPlaybook(domain){
  try{
    const r = await fetchJSON('/playbook/'+encodeURIComponent(domain));
    let html = `<div class="modal-bg" onclick="if(event.target===this)this.remove()"><div class="modal" style="max-width:780px;max-height:90vh;overflow-y:auto;">`;
    html += `<h3>🔍 ${r.brand_name} · 战术深度拆解</h3>`;
    html += `<div style="color:#8a93a6;font-size:12px;margin-bottom:14px;">${r.domain} · ${r.keyword}</div>`;

    // 综合评分条
    html += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px;">';
    html += `<div class="stat"><div class="v">${r.score||0}</div><div class="l">综合分</div></div>`;
    html += `<div class="stat"><div class="v">${r.expert_score||0}/10</div><div class="l">专家分</div></div>`;
    html += `<div class="stat"><div class="v">${r.tactical_score||0}/10</div><div class="l">战术分</div></div>`;
    html += `<div class="stat"><div class="v">${r.signal_score||0}/30</div><div class="l">商业信号</div></div>`;
    html += '</div>';

    const section = (title, items) => {
      if(!items.length) return '';
      let h = `<h4 style="color:#e6e6e6;margin:14px 0 6px;font-size:14px;">${title}</h4><div style="display:flex;flex-direction:column;gap:6px;">`;
      items.forEach(([k,v,hint])=>{
        if(v==null||v==='')return;
        h += `<div style="padding:8px 12px;background:#1a2030;border-radius:6px;display:flex;gap:12px;align-items:flex-start;">
          <div style="min-width:120px;color:#8a93a6;font-size:12px;">${k}</div>
          <div style="flex:1;"><div style="font-weight:600;color:#d5d8de;">${v}</div>
          ${hint?`<div style="color:#93c5fd;font-size:11px;margin-top:2px;">${hint}</div>`:''}</div>
        </div>`;
      });
      return h+'</div>';
    };

    html += section('📊 市场态势', [
      ['跟风窗口', r.copycat_risk, r.copycat_strategy],
      ['投放阶段', r.stage_hint, ''],
      ['每周活跃广告', r.weekly_ad_velocity?r.weekly_ad_velocity+' 条/周':'', ''],
    ]);

    html += section('🛒 落地页战术', [
      ['类型', r.funnel_type, r.funnel_hint],
      ['TLD 信任', r.tld?r.tld+' (信任 '+r.tld_trust+'/10)':'', ''],
      ['子域前缀', r.subdomain_prefix, r.root_domain?'根域名: '+r.root_domain:''],
    ]);

    html += section('🧠 品牌画像', [
      ['命名风格', r.archetype, r.archetype_hint],
      ['轨道评分', r.orbit_score, r.orbit_score>=70?'跨多关键词出现, 强势':r.orbit_score>=40?'成长期品牌':'孤立出现'],
    ]);

    html += section('💰 商业信号', [
      ['价格', r.price_main?`$${r.price_main}`:'', ''],
      ['折扣', r.discount_pct?`${r.discount_pct}%`:'', ''],
      ['评论数', r.review_count?r.review_count.toLocaleString():'', r.review_count>=1000?'社会证明强':''],
      ['星级', r.star_rating?r.star_rating+'★':'', ''],
      ['紧迫感', r.has_urgency?'✓ 有':'', '稀缺营销'],
      ['保障', r.has_guarantee?'✓ 有':'', '风险消除'],
      ['权威', r.has_authority?'✓ 有':'', '医生/临床/FDA 背书'],
    ]);

    html += section('🎯 文本信号', [
      ['痛点', r.top_pain, ''],
      ['场景', r.top_scene, ''],
      ['产品词', r.top_product, ''],
    ]);

    // 竞品同场
    if(r.competitors_same_keyword && r.competitors_same_keyword.length){
      html += `<h4 style="color:#e6e6e6;margin:16px 0 6px;font-size:14px;">⚔️ 同关键词竞品 (${r.market_heat} 家在此词下)</h4>`;
      html += '<div style="max-height:220px;overflow-y:auto;">';
      r.competitors_same_keyword.forEach(c=>{
        html += `<div style="padding:6px 10px;background:#1a2030;border-radius:4px;margin-bottom:3px;display:flex;gap:8px;align-items:center;">
          <span class="score-pill ${c.score>=90?'s-90':c.score>=70?'s-70':'s-60'}">${c.score}</span>
          <span style="flex:1;">${c.brand_name} <span style="color:#8a93a6;">(${c.domain})</span></span>
          <span style="color:#8a93a6;font-size:11px;">${c.ad_age_days}d · ${c.is_shopify_shape?'Shopify':''}</span>
        </div>`;
      });
      html += '</div>';
    }

    // 最终策略
    html += `<div style="margin-top:16px;padding:12px;background:linear-gradient(135deg,#1e3a5f,#1a2030);border-radius:8px;">
      <div style="font-weight:700;color:#4ade80;margin-bottom:6px;">🎲 ${r.verdict||''}</div>
      <div style="color:#d5d8de;font-size:12px;line-height:1.6;">${r.copycat_strategy||''}</div>
    </div>`;

    html += '<button onclick="this.closest(\'.modal-bg\').remove()" style="margin-top:16px;">关闭</button></div></div>';
    document.body.insertAdjacentHTML('beforeend', html);
  }catch(e){toast('深度拆解失败: '+e,false);}
}
async function showCoverage(domain){
  try{
    const r = await fetchJSON('/brands/'+encodeURIComponent(domain)+'/countries');
    let html = `<div class="modal-bg" onclick="if(event.target===this)this.remove()"><div class="modal" style="max-width:680px;">`;
    html += `<h3>🌍 ${domain} · 地区覆盖分析</h3>`;

    html += `<div style="margin-top:8px;"><b style="color:#8a93a6;font-size:12px;">✓ 已投放 (${r.covered.length}):</b></div>`;
    html += '<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;">';
    if(r.covered.length){
      r.covered.forEach(c=>{
        html += `<div class="flag flag-covered" style="display:flex;flex-direction:column;align-items:flex-start;padding:6px 10px;">
          <div style="font-size:14px;">${c.flag} ${c.name}</div>
          <div style="font-size:10px;opacity:.8;">人均 $${(c.gdp_per_capita/1000).toFixed(0)}K</div>
        </div>`;
      });
    }else{
      html += '<span style="color:#8a93a6;">暂无</span>';
    }
    html += '</div>';

    html += `<div style="margin-top:16px;"><b style="color:#8a93a6;font-size:12px;">🎯 未覆盖机会 (按人均 GDP 倒序排, ${r.missing.length} 个):</b></div>`;
    html += '<div style="display:flex;flex-direction:column;gap:4px;margin-top:6px;max-height:320px;overflow-y:auto;">';
    if(r.missing.length){
      r.missing.forEach(c=>{
        const gdp = (c.gdp_per_capita/1000).toFixed(0);
        const tier = c.gdp_per_capita >= 80000 ? '💎 顶级' : c.gdp_per_capita >= 55000 ? '🔥 高净值' : '✓ 可试';
        html += `<div style="display:flex;gap:10px;align-items:center;padding:6px 10px;background:#1a2030;border-radius:6px;">
          <div style="font-size:15px;min-width:28px;">${c.flag}</div>
          <div style="min-width:90px;"><b>${c.name}</b></div>
          <div style="color:#93c5fd;font-size:12px;min-width:60px;">$${gdp}K</div>
          <div style="color:#4ade80;font-size:11px;min-width:72px;">FB ${(c.fb_penetration*100).toFixed(0)}%</div>
          <div style="color:#fbbf24;font-size:11px;min-width:60px;">${tier}</div>
          <div style="color:#8a93a6;font-size:11px;flex:1;">${c.notes}</div>
        </div>`;
      });
    }else{
      html += '<span style="color:#8a93a6;">已全覆盖</span>';
    }
    html += '</div>';

    if(r.opportunity){
      const topMissing = r.missing.slice(0,3).map(c=>c.flag+c.name).join(' · ');
      html += `<div style="margin-top:14px;padding:10px;background:#1e3a5f;border-radius:6px;color:#93c5fd;font-size:13px;">
        💡 <b>选品师建议</b>: 此品牌在 ${r.covered[0]?.flag||''}${r.covered[0]?.name||''} 已验证可行, 推荐优先切入高人均 GDP 区域: <b>${topMissing}</b>. 策略: 直接复制美国素材 + 本地物流+支付+必要翻译.
      </div>`;
    }
    html += '<button onclick="this.closest(\'.modal-bg\').remove()">关闭</button></div></div>';
    document.body.insertAdjacentHTML('beforeend', html);
  }catch(e){toast('查询失败: '+e,false);}
}

const COLS = {
  recs: [
    {k:'score',t:'分',f:v=>scorePill(v),w:50},
    {k:'expert_score',t:'🎯专家',f:(v,r)=>`<span class="score-pill ${v>=8?'s-90':v>=6?'s-80':v>=4?'s-70':'s-60'}">${v??0}</span>`,w:65},
    {k:'tactical_score',t:'⚔战术',f:v=>v?`<span class="score-pill ${v>=8?'s-90':v>=6?'s-80':'s-60'}">${v}</span>`:'',w:60},
    {k:'verdict',t:'判断',f:v=>`<span class="truncate" style="font-weight:600;color:${(v||'').includes('强烈')?'#4ade80':(v||'').includes('推荐')?'#93c5fd':(v||'').includes('观察')?'#fbbf24':'#8a93a6'}">${v||''}</span>`,w:120},
    {k:'category_name',t:'类目',f:(v,r)=>v&&v!=='未分类'?`<span class="itag" style="background:${r.in_season?'#14532d':'#1e3a5f'};color:${r.in_season?'#86efac':'#93c5fd'};" title="${r.category_advice||''}">${v}${r.in_season?' 🌟':''}</span>`:'',w:100},
    {k:'price_fit',t:'价带',f:(v,r)=>v?`<span class="itag" style="background:${v==='sweet'?'#14532d':v==='premium'?'#854d0e':v==='low'?'#3f3f46':'#7c2d12'};color:${v==='sweet'?'#86efac':v==='premium'?'#fde68a':'#cbd5e1'};" title="${r.price_fit_hint||''}">${({sweet:'甜蜜带',premium:'高客单',low:'低客单',high:'超高'}[v])||v}</span>`:'',w:75},
    {k:'copycat_risk',t:'跟风窗口',f:v=>v?`<span class="itag" style="background:${(v||'').includes('蓝海')||(v||'').includes('黄金')?'#14532d':(v||'').includes('红海')?'#7c2d12':'#1e3a5f'};color:#fff;">${v}</span>`:'',w:100},
    {k:'stage',t:'投放阶段',f:v=>v?`<span class="itag">${({scaling_hard:'规模化扩量',proven_scaling:'稳步扩量',cash_cow:'印钞机',testing:'测品中',long_tail:'长尾'}[v])||v}</span>`:'',w:100},
    {k:'funnel_type',t:'落地页',f:v=>v?`<span class="itag">${v}</span>`:'',w:90},
    {k:'brand_name',t:'品牌',f:v=>`<span class="truncate">${v||''}</span>`},
    {k:'domain',t:'域名',f:(v,r)=>r.landing_url?`<a href="${r.landing_url}" target="_blank" rel="noopener" class="truncate">${v}</a>`:v},
    {k:'is_shopify_shape',t:'🛒',f:v=>v?'<span class="shopify">✓</span>':'',w:45},
    {k:'ad_age_days',t:'阶段',f:v=>tierBadge(v||0),w:100},
    {k:'ads_count',t:'广告',w:55},
    {k:'is_active',t:'活跃',f:v=>v?'<span class="active-dot">●</span>':'',w:50},
    {k:'price_main',t:'价格',f:v=>v?`$${v}`:'',w:60},
    {k:'discount_pct',t:'折扣',f:v=>v?`${v}%`:'',w:55},
    {k:'review_count',t:'评论',f:v=>v?(v>=1000?(v/1000).toFixed(0)+'K':v):'',w:60},
    {k:'star_rating',t:'星',f:v=>v?`${v}★`:'',w:50},
    {k:'signal_score',t:'商信号',f:v=>v?`<span class="sig-badge">${v}</span>`:'',w:65},
    {k:'orbit_score',t:'轨道',w:55},
    {k:'insight_tags',t:'选品洞见',f:(v,r)=>(v||[]).map(t=>`<span class="itag">${t}</span>`).join(' ')},
    {k:'domain',t:'🔍',f:(v,r)=>`<button class="linkbtn" onclick="showPlaybook('${v}')" title="战术深度拆解">🔍</button>`,w:40},
    {k:'domain',t:'🌍',f:(v,r)=>`<button class="linkbtn" onclick="showCoverage('${v}')" title="国家覆盖">🌍</button>`,w:40},
    {k:'scanned_at',t:'扫描时间',f:(v,r)=>{const s=(v||'').slice(0,10);const fs=(r.first_seen_at||'').slice(0,10);return s?`<span class="truncate" style="color:#8a93a6;font-size:11px;" title="扫描: ${v||''}&#10;首次入库: ${r.first_seen_at||''}">${s}${fs&&fs!==s?`<br><span style="color:#6a7080;">📥${fs}</span>`:''}</span>`:'';},w:90},
    {k:'keyword',t:'来源关键词'},
  ],
  brands: [
    {k:'domain',t:'域名'},
    {k:'canonical_name',t:'名称'},
    {k:'orbit_score',t:'轨道分',f:v=>scorePill(v||0)},
    {k:'appearance_count',t:'扫描次数'},
  ],
  keywords: [
    {k:'keyword',t:'关键词',f:v=>`<span class="truncate" style="font-weight:500;">${v||''}</span>`,w:280},
    {k:'kw_type',t:'类型',f:v=>{
      const clr={pain:'#f87171',scene:'#60a5fa',product:'#4ade80',brand:'#fbbf24'}[v]||'#8a93a6';
      return `<span class="itag" style="background:${clr};color:#000;font-weight:600;">${v||'—'}</span>`;
    },w:70},
    {k:'subtype',t:'子类/来源',f:v=>{
      if(!v) return '';
      const m={
        'rh-l2':{t:'L2 属性',bg:'#7dd3fc'},
        'rh-l3':{t:'L3 场景',bg:'#93c5fd'},
        'rh-l4':{t:'L4 人群',bg:'#c4b5fd'},
        'rh-l5':{t:'L5 根因',bg:'#fda4af'},
        'rh-l6':{t:'L6 问句',bg:'#fcd34d'},
        'rh-l7':{t:'L7 TikTok',bg:'#ff69b4'},
        'rh-l8':{t:'L8 身份',bg:'#f0abfc'},
        'rh-l9':{t:'L9 2026',bg:'#86efac'},
        'rh-l10':{t:'L10 清洁',bg:'#a7f3d0'},
        'rh-l11':{t:'L11 时限',bg:'#fdba74'},
        'auto-harvest':{t:'🎯 自学',bg:'#6ee7b7'},
        'auto-extend':{t:'🌱 扩展',bg:'#a7f3d0'},
        'suggest-voice-query':{t:'🗣 问句',bg:'#fcd34d'},
        'suggest-scene-context':{t:'📍 场景',bg:'#93c5fd'},
        'suggest-pain-symptom':{t:'🩹 痛点',bg:'#fca5a5'},
        'suggest-product-item':{t:'📦 产品',bg:'#6ee7b7'},
        'suggest-generic':{t:'💡 通用',bg:'#ddd'},
        'retired-low-roi':{t:'💤 已淘汰',bg:'#6b7280'},
      }[v] || {t:v,bg:'#374151'};
      return `<span class="itag" style="background:${m.bg};color:#000;" title="${v}">${m.t}</span>`;
    },w:110},
    {k:'source_kw',t:'来源平台',f:v=>{
      if(!v) return '';
      const s=String(v);
      if(s.includes('multi-platform')||s.includes('amazon+google')){
        return `<span class="itag" style="background:linear-gradient(90deg,#ff9900,#4285f4);color:#fff;">🌐 6平台</span>`;
      }
      if(s.includes('amazon')) return `<span class="itag" style="background:#ff9900;color:#000;">🛒 Amazon</span>`;
      if(s.includes('google'))  return `<span class="itag" style="background:#4285f4;color:#fff;">🔍 Google</span>`;
      if(s.includes('/L'))      return `<span class="itag" style="background:#8b5cf6;color:#fff;" title="${s}">🕳 RH</span>`;
      if(s==='evolution')       return `<span class="itag" style="background:#10b981;color:#fff;">🧬 进化</span>`;
      if(s==='brand')           return `<span class="itag" style="background:#f59e0b;color:#000;">🏷 品牌</span>`;
      return `<span class="truncate" style="font-size:12px;color:#8a93a6;" title="${s}">${s.slice(0,28)}</span>`;
    },w:130},
    {k:'learned_date',t:'入库日期',w:100},
  ],
  rabbit2026: [
    {k:'rh_score',t:'🐰 RH 分',f:v=>`<span class="score-pill ${v>=120?'s-100':v>=100?'s-90':v>=80?'s-80':v>=60?'s-70':'s-60'}">${v||0}</span>`,w:80},
    {k:'rh_siblings',t:'🔁 同窝',f:v=>`<span class="sig-badge" title="同场景/痛点品牌数">${v||0}</span>`,w:60},
    {k:'score',t:'基础分',f:v=>scorePill(v||0),w:60},
    {k:'brand_name',t:'品牌',f:v=>`<span class="truncate">${v||''}</span>`},
    {k:'domain',t:'域名',f:(v,r)=>r.landing_url?`<a href="${r.landing_url}" target="_blank" rel="noopener" class="truncate">${v}</a>`:v},
    {k:'is_shopify_shape',t:'🛒',f:v=>v?'<span class="shopify">✓</span>':'',w:45},
    {k:'top_pain',t:'痛点',f:v=>v?`<span class="truncate" title="${v}">${v}</span>`:'',w:160},
    {k:'top_scene',t:'场景',f:v=>v?`<span class="truncate" title="${v}">${v}</span>`:'',w:140},
    {k:'price_main',t:'价格',f:v=>v?`$${v}`:'',w:60},
    {k:'ad_age_days',t:'阶段',f:v=>tierBadge(v||0),w:100},
    {k:'ads_count',t:'广告',w:55},
    {k:'is_active',t:'活跃',f:v=>v?'<span class="active-dot">●</span>':'',w:50},
    {k:'domain',t:'🔍',f:(v,r)=>`<button class="linkbtn" onclick="showPlaybook('${v}')" title="战术深度拆解">🔍</button>`,w:40},
  ],
};

function render(){
  const allCols = COLS[state.tab];
  const hidden = getHiddenCols();
  const cols = allCols.filter((c,i) => !hidden.has(colId(c,i)));
  const q = document.getElementById('search').value.toLowerCase().trim();
  const activeOnly = document.getElementById('active-only').checked;
  const signalOnly = document.getElementById('signal-only').checked;
  let rows = state.data;
  if(q) rows = rows.filter(r=>JSON.stringify(r).toLowerCase().includes(q));
  if(activeOnly && state.tab==='recs') rows = rows.filter(r=>r.is_active);
  if(signalOnly && state.tab==='recs') rows = rows.filter(r=>(r.signal_score||0)>0);
  if(state.sortKey){
    rows = [...rows].sort((a,b)=>{
      let av=a[state.sortKey], bv=b[state.sortKey];
      if(typeof av==='string'){av=av.toLowerCase();bv=(bv||'').toLowerCase();}
      av=av??0; bv=bv??0;
      return (av>bv?1:av<bv?-1:0)*state.sortDir;
    });
  }
  if(!rows.length){
    document.getElementById('content').innerHTML='<div class="empty">无数据</div>';
    return;
  }
  // 恢复用户保存的列宽
  const savedWidths = JSON.parse(localStorage.getItem('colw_'+state.tab)||'{}');
  const th = cols.map(c=>{
    const cls = state.sortKey===c.k ? (state.sortDir>0?'sort-asc':'sort-desc') : '';
    const w = savedWidths[c.k] || c.w;
    const wStyle = w ? `style="width:${w}px;min-width:${w}px;"` : '';
    return `<th class="${cls}" ${wStyle} data-col="${c.k}" onclick="setSort('${c.k}')">${c.t}<span class="resizer" onmousedown="startResize(event,'${c.k}')" onclick="event.stopPropagation()"></span></th>`;
  }).join('');
  const body = rows.slice(0,2000).map(r=>`<tr class="${r.is_shopify_shape?'row-shopify':''}">`+cols.map(c=>{
    let v;
    try {
      v = r[c.k];
      if(c.f) v = c.f(v,r);
      else v = v==null?'':v;
    } catch(e) {
      v = '<span style="color:#f87171;">!</span>';
    }
    return `<td>${v}</td>`;
  }).join('')+'</tr>').join('');
  const capNote = '';
  document.getElementById('content').innerHTML = capNote + `<div class="tblwrap"><table><thead><tr>${th}</tr></thead><tbody>${body}</tbody></table></div>` + renderPager();
}

function renderPager(){
  const total = state.total || rows_for_pager_total();
  const pages = Math.max(1, Math.ceil(total/state.pageSize));
  const cur = Math.min(state.page, pages);
  const start = (cur-1)*state.pageSize + 1;
  const end = Math.min(cur*state.pageSize, total);
  let h = '<div class="pager">';
  h += `<button ${cur<=1?'disabled':''} onclick="gotoPage(1)">« 首页</button>`;
  h += `<button ${cur<=1?'disabled':''} onclick="gotoPage(${cur-1})">‹ 上</button>`;
  // 滑动窗口 5 页
  const from = Math.max(1, cur-2), to = Math.min(pages, cur+2);
  if(from>1) h += `<span class="info">…</span>`;
  for(let i=from; i<=to; i++){
    h += `<button class="${i===cur?'active':''}" onclick="gotoPage(${i})">${i}</button>`;
  }
  if(to<pages) h += `<span class="info">…</span>`;
  h += `<button ${cur>=pages?'disabled':''} onclick="gotoPage(${cur+1})">下 ›</button>`;
  h += `<button ${cur>=pages?'disabled':''} onclick="gotoPage(${pages})">末页 »</button>`;
  h += `<span class="info">第 <input type="number" min="1" max="${pages}" value="${cur}" onkeydown="if(event.key==='Enter')gotoPage(parseInt(this.value))"> / ${pages} 页 · 共 ${total.toLocaleString()} 条</span>`;
  h += `<select onchange="setPageSize(parseInt(this.value))">`;
  [50,100,200,500,1000,2000,5000].forEach(n=>{ h+=`<option value="${n}" ${n===state.pageSize?'selected':''}>${n}/页</option>`; });
  h += '</select>';
  h += '</div>';
  return h;
}
function rows_for_pager_total(){ return state.data.length; }
function gotoPage(p){
  const pages = Math.max(1, Math.ceil((state.total||state.data.length)/state.pageSize));
  state.page = Math.max(1, Math.min(pages, p||1));
  load();
}
function setPageSize(n){
  state.pageSize = n;
  state.page = 1;
  try{localStorage.setItem('pageSize', String(n));}catch(e){}
  load();
}

function startResize(e,k){
  e.stopPropagation();
  const th = e.target.parentElement;
  const startX = e.clientX;
  const startW = th.offsetWidth;
  function onMove(ev){
    const w = Math.max(40, startW + (ev.clientX - startX));
    th.style.width = w + 'px';
    th.style.minWidth = w + 'px';
  }
  function onUp(){
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    // 保存
    const saved = JSON.parse(localStorage.getItem('colw_'+state.tab)||'{}');
    saved[k] = th.offsetWidth;
    localStorage.setItem('colw_'+state.tab, JSON.stringify(saved));
  }
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}
function setSort(k){
  if(state.sortKey===k) state.sortDir=-state.sortDir;
  else{state.sortKey=k; state.sortDir=-1;}  render();
}

// ── 列显示选择 ──────────────────────────────────────────────────────────
function colId(c, idx){ return (c.k||'') + '|' + (c.t||'') + '|' + idx; }
function getHiddenCols(){
  try { return new Set(JSON.parse(localStorage.getItem('hidden_cols_'+state.tab)||'[]')); }
  catch(e) { return new Set(); }
}
function setHiddenCols(set){
  localStorage.setItem('hidden_cols_'+state.tab, JSON.stringify([...set]));
}
function toggleCol(cid, visible){
  const h = getHiddenCols();
  if(visible) h.delete(cid); else h.add(cid);
  setHiddenCols(h);
  render();
}
function setAllCols(visible){
  const all = COLS[state.tab] || [];
  if(visible){ setHiddenCols(new Set()); }
  else { setHiddenCols(new Set(all.map((c,i)=>colId(c,i)))); }
  openColPicker(); render();
}
function resetCols(){
  localStorage.removeItem('hidden_cols_'+state.tab);
  openColPicker(); render();
}
function openColPicker(){
  const picker = document.getElementById('col-picker');
  const all = COLS[state.tab] || [];
  const hidden = getHiddenCols();
  const seen = new Set();
  const html = all.map((c,i)=>{
    const cid = colId(c,i);
    const title = c.t || c.k || '(无名)';
    // 同名列合并(如 domain 重复) — 只显示首次出现的切换
    if(seen.has(title)) return '';
    seen.add(title);
    const ck = !hidden.has(cid);
    const cidAttr = cid.replace(/'/g,'&apos;');
    return `<label style="display:flex;align-items:center;gap:6px;padding:4px 0;color:#cbd5e1;font-size:13px;cursor:pointer;">
      <input type="checkbox" ${ck?'checked':''} onchange="toggleCol('${cidAttr}', this.checked)">
      <span>${title}</span>
    </label>`;
  }).join('');
  document.getElementById('col-picker-body').innerHTML = html || '<div style="color:#8a93a6;">无列</div>';
  picker.style.display = picker.style.display==='none' ? 'block' : 'none';
}
function exportCSV(){
  if(state.tab==='recs'){
    const only=document.getElementById('shopify-only').checked?'&only_shopify=true':'';
    const ep='&exclude_platforms=' + (state.excludePlatforms ? 'true' : 'false');
    window.location.href='/recommendations.xlsx?min_score='+state.minScore+only+ep;
    return;
  }
  // 其他 tab: 客户端导出
  const rows = state.data;
  if(!rows.length)return;
  const keys = Object.keys(rows[0]);
  const csv = [keys.join(',')].concat(rows.map(r=>keys.map(k=>JSON.stringify(r[k]??'')).join(','))).join('\n');
  const blob = new Blob([csv],{type:'text/csv;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = state.tab+'-'+new Date().toISOString().slice(0,10)+'.csv';
  a.click();
}

load();

// 粘性偏移自动同步: toolbar 会因 pills 换行而长高, 若不同步, 表头 sticky top 会落到 toolbar 覆盖区被遮挡
(function syncStickyOffsets(){
  const root = document.documentElement;
  const hdr = document.querySelector('header');
  const tabs = document.querySelector('nav.tabs');
  const tool = document.querySelector('.toolbar');
  const update = ()=>{
    if(hdr)  root.style.setProperty('--hdr-h',  hdr.offsetHeight+'px');
    if(tabs) root.style.setProperty('--tabs-h', tabs.offsetHeight+'px');
    if(tool) root.style.setProperty('--tool-h', tool.offsetHeight+'px');
  };
  update();
  window.addEventListener('resize', update);
  if(tool && typeof ResizeObserver!=='undefined') new ResizeObserver(update).observe(tool);
})();

// 智能自动刷新: 30s 间隔 SWR 模式 (保留旧数据后台刷); visibilitychange 立即刷; ops tab 60s
let _autoRefreshTimer = null;
function _scheduleAutoRefresh(){
  if(_autoRefreshTimer) clearInterval(_autoRefreshTimer);
  _autoRefreshTimer = setInterval(()=>{
    if(document.hidden) return;
    if(state.tab === 'ops'){
      // ops 60s 周期 (重活: log-tail + queue)
      if(!state.lastFetched || Date.now()-state.lastFetched > 55000){
        try{ load(true); }catch(e){ console.warn('auto-refresh failed:', e); }
      }
    }else{
      try{ load(true); }catch(e){ console.warn('auto-refresh failed:', e); }
    }
  }, 30 * 1000);
}
_scheduleAutoRefresh();
// 切回前台立即刷 (但若 5s 内刚刷过就跳过)
document.addEventListener('visibilitychange', ()=>{
  if(!document.hidden && (!state.lastFetched || Date.now()-state.lastFetched > 5000)){
    try{ load(true); }catch(e){ console.warn('visibility refresh failed:', e); }
  }
});
// 切窗口聚焦立即刷
window.addEventListener('focus', ()=>{
  if(!state.lastFetched || Date.now()-state.lastFetched > 5000){
    try{ load(true); }catch(e){ console.warn('focus refresh failed:', e); }
  }
});
</script>
</body>
</html>
"""


@app.get("/v2", response_class=HTMLResponse)
def dashboard_v2():
    """2026 v2 信号面板: rerank-v2 · LP 指纹 · velocity · cluster."""
    return V2_DASHBOARD_HTML


@app.get("/v3", response_class=HTMLResponse)
def dashboard_v3():
    """SelectPro V3 世界级自动化智能选品工作台."""
    return V3_DASHBOARD_HTML


@app.get("/V3", response_class=HTMLResponse)
def dashboard_v3_upper():
    """SelectPro V3 uppercase shortcut."""
    return V3_DASHBOARD_HTML


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


# ── /v2 加速器 + D2C 卖货专属过滤 ──────────────────────────────────────────
# 把 fb-ad-scrape-fast 的全部能力集中到 v2 命名空间下:
#   POST /v2/scrape-fast            启动并发抓取 session (D2C 关键词预筛 + 多 worker)
#   POST /v2/scrape-fast/auto       自动从 learned/queue 拉 D2C 关键词后启动
#   GET  /v2/scrape-fast/sessions   列出所有 session
#   GET  /v2/scrape-fast/{sid}      单 session 实时状态
#   POST /v2/lp-probe               便捷转发 lp-probe (强制 force=False, 高 worker)
#   GET  /v2/d2c-winners            服务端拉 expert recs + LP 指纹 + D2C 硬过滤
#   GET  /v2/throughput             throughput + LP coverage 一站式视图

@app.post("/v2/scrape-fast")
def v2_scrape_fast(payload: V2ScrapeFastRequest):
    """启动一个并发 D2C 抓取 session; dry_run=true 时只返回计划, 不访问外部目标."""
    from .v2_scrape_fast import start_scrape_session, filter_seed_keywords

    raw_kws = payload.keywords or []
    pre_dropped = []
    kept = raw_kws
    if raw_kws:
        kept, pre_dropped = filter_seed_keywords(raw_kws)
        if not kept:
            raise HTTPException(
                status_code=400,
                detail=f"all {len(raw_kws)} keywords filtered out as non-D2C",
            )
    elif payload.auto_pull_limit <= 0:
        raise HTTPException(status_code=400, detail="provide keywords or auto_pull_limit > 0")

    result = start_scrape_session(
        keywords=kept or None,
        auto_pull_limit=payload.auto_pull_limit,
        workers=payload.workers,
        country=payload.country,
        headless=payload.headless,
        strategy=payload.strategy,
        dry_run=payload.dry_run,
    )
    if "error" in result:
        status = 429 if "too many active" in str(result["error"]) else 400
        raise HTTPException(status_code=status, detail=result)
    result["pre_filter_dropped"] = len(pre_dropped)
    result["pre_filter_dropped_sample"] = pre_dropped[:10]
    return result


@app.post("/v2/scrape-fast/auto")
def v2_scrape_fast_auto(
    limit: int = Query(40, ge=1, le=300),
    workers: int = Query(5, ge=1, le=16),
    country: Optional[str] = Query(None),
    headless: bool = Query(True),
    dry_run: bool = Query(False),
    strategy: str = Query("auto", pattern="^(auto|quick|deep|playwright)$"),
):
    """便捷入口: 不传关键词, 服务端自动从 learned 库拉 D2C 友好词后开抓."""
    from .v2_scrape_fast import start_scrape_session
    res = start_scrape_session(
        auto_pull_limit=limit, workers=workers,
        country=country, headless=headless, dry_run=dry_run, strategy=strategy,
    )
    if "error" in res:
        status = 429 if "too many active" in str(res["error"]) else 400
        raise HTTPException(status_code=status, detail=res)
    return res


@app.get("/v2/health")
def v2_health():
    from .v2_scrape_fast import get_sessions_summary

    warnings: list[str] = []
    scraper = _safe_scraper_status(warnings)
    sessions = get_sessions_summary()
    status = "degraded" if warnings or scraper.get("in_cooldown") else "ok"
    return {
        "status": status,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scraper": scraper,
        "sessions": sessions,
        "warnings": warnings,
    }


@app.get("/v2/scrape-fast/sessions")
def v2_scrape_fast_sessions():
    from .v2_scrape_fast import list_sessions
    return {"sessions": list_sessions()}


@app.get("/v2/scrape-fast/{sid}")
def v2_scrape_fast_status(sid: str, tail: int = Query(50, ge=1, le=300)):
    from .v2_scrape_fast import get_session_state
    state = get_session_state(sid, tail=tail)
    if not state:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    return state


@app.post("/v2/scrape-fast/{sid}/cancel")
def v2_scrape_fast_cancel(sid: str):
    from .v2_scrape_fast import cancel_session
    state = cancel_session(sid)
    if not state:
        raise HTTPException(status_code=404, detail=f"session {sid} not found")
    return state


@app.post("/v2/lp-probe")
def v2_lp_probe(
    limit: int = Query(300, ge=1, le=2000),
    workers: int = Query(8, ge=1, le=16),
    force: bool = Query(False),
    dry_run: bool = Query(False),
):
    """v2 转发 lp-probe; dry_run=true 时只返回计划, 不访问外部目标."""
    from .lp_prober import probe_domains
    return probe_domains(limit=limit, max_workers=workers, force_refresh=force, dry_run=dry_run)


@app.get("/v2/d2c-winners")
def v2_d2c_winners(
    top: int = Query(500, ge=1, le=5000),
    min_ads: int = Query(2, ge=1),
    min_price: float = Query(9.0, ge=0),
    days: int = Query(14, ge=1, le=180),
    only_active: bool = Query(True),
):
    """独立站卖货广告专属白名单.

    流程: 拉 expert recs (only_shopify) → 读 lp_fingerprints 表 → 应用硬过滤
    (LP 指纹 / CTA / 落地页形态 / 价格信号 / ads_count / 上线天数).

    返回: {kept_count, dropped_count, drop_reasons, winners[], generated_at}
    """
    from .v2_scrape_fast import collect_d2c_winners
    return collect_d2c_winners(
        top=top, min_ads=min_ads, min_price=min_price,
        days=days, only_active=only_active,
    )


@app.get("/v2/throughput")
def v2_throughput():
    """一站式视图: scraper throughput + LP coverage + 当前 sessions + layer 分布."""
    from .persistence import get_persistence
    from .v2_scrape_fast import list_sessions

    out = {"sessions": list_sessions()}

    # throughput
    try:
        db = get_persistence()
        with db._conn() as conn:
            now = datetime.now()
            for label, hours in (("last_1h", 1), ("last_6h", 6), ("last_24h", 24)):
                since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                kw_count = conn.execute(
                    "SELECT COUNT(DISTINCT keyword) FROM scans WHERE scan_date >= ?",
                    (since,),
                ).fetchone()
                ad_count = 0
                try:
                    ad_count = conn.execute(
                        "SELECT COUNT(*) FROM ad_archive WHERE inserted_at >= ?",
                        (since,),
                    ).fetchone()[0] or 0
                except Exception:
                    pass
                out[label] = {
                    "keywords": kw_count[0] if kw_count else 0,
                    "ads": ad_count,
                    "kw_per_hour": round((kw_count[0] if kw_count else 0) / hours, 1),
                    "ads_per_hour": round(ad_count / hours, 1),
                }
    except Exception as e:
        out["throughput_error"] = f"{type(e).__name__}: {e}"

    # LP coverage
    try:
        db = get_persistence()
        with db._conn() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM recommendations WHERE landing_url IS NOT NULL AND landing_url != ''"
            ).fetchone()[0]
            probed = conn.execute("SELECT COUNT(*) FROM lp_fingerprints").fetchone()[0]
            out["lp_coverage"] = {
                "total_brands_with_url": total,
                "probed": probed,
                "coverage_pct": round(100 * probed / max(total, 1), 1),
            }
    except Exception as e:
        out["lp_coverage_error"] = f"{type(e).__name__}: {e}"

    # v10: layer breakdown — 扫描 RESULTS_DIR 最近文件, 按 source 字段聚合
    # 显示 graphql_replay 实际激活率, BrowserForge 减少 challenge 的效果
    try:
        out["layers"] = _layer_breakdown(hours=1)
    except Exception as e:
        out["layers_error"] = f"{type(e).__name__}: {e}"

    return out


def _layer_breakdown(hours: int = 1) -> dict:
    """Scan RESULTS_DIR for files modified within `hours`, tally by source field.

    Returns:
        {
          'window_hours': int,
          'files_scanned': int,
          'counts': {source_label: file_count, ...},
          'ads': {source_label: ad_count, ...},
          'graphql_replay_hit_rate': float,   # replay / (replay + patchright-non-replay)
          'avg_elapsed_sec_by_source': {source_label: avg_sec, ...},
        }
    """
    import time as _time
    import json as _json
    from .paths import RESULTS_DIR

    cutoff = _time.time() - hours * 3600
    counts: dict[str, int] = {}
    ads_by_source: dict[str, int] = {}
    elapsed_sum: dict[str, float] = {}
    elapsed_n: dict[str, int] = {}
    replay_files = 0
    replay_ads_added = 0
    scanned = 0

    try:
        entries = list(RESULTS_DIR.iterdir())
    except Exception:
        entries = []
    # 时间倒序, 早停
    entries.sort(key=lambda p: getattr(p.stat(), "st_mtime", 0), reverse=True)
    for path in entries:
        try:
            st = path.stat()
            if st.st_mtime < cutoff:
                break
            if path.suffix.lower() != ".json":
                continue
        except Exception:
            continue
        try:
            with open(path, "rb") as f:
                data = _json.loads(f.read().decode("utf-8", errors="replace"))
        except Exception:
            continue
        scanned += 1
        source = str(data.get("source") or "unknown")
        counts[source] = counts.get(source, 0) + 1
        ad_n = int(data.get("count") or 0)
        ads_by_source[source] = ads_by_source.get(source, 0) + ad_n
        if data.get("graphql_replay_used"):
            replay_files += 1
            replay_ads_added += int(data.get("graphql_replay_added") or 0)
        # elapsed (best-effort — only some payloads include it)
        # Note: save_result strips elapsed_sec, but raw result dict had it.
        # Look in graphql meta or skip.

    # hit rate: graphql_replay files / (graphql_replay + patchright-without-replay)
    replay_label = "L1.5-graphql-replay"
    base_label = "L1.5-patchright"
    base_n = counts.get(base_label, 0)
    replay_n = counts.get(replay_label, 0)
    hit_rate = round(replay_n / max(replay_n + base_n, 1), 3)

    return {
        "window_hours": hours,
        "files_scanned": scanned,
        "counts": counts,
        "ads": ads_by_source,
        "graphql_replay_hit_rate": hit_rate,
        "graphql_replay_files": replay_files,
        "graphql_replay_ads_added": replay_ads_added,
    }


def run(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    """Launch uvicorn serving this app."""
    import uvicorn
    uvicorn.run(
        "pipeline.server:app" if reload else app,
        host=host, port=port, reload=reload,
    )

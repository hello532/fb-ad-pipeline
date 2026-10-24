"""V3 世界级自动化智能选品工作台 HTML."""

V3_DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>FB SelectPro V3</title>
<style>
:root{--bg:#090f1f;--panel:#121a2d;--panel2:#172238;--line:#263650;--text:#edf3ff;--muted:#91a0b8;--accent:#7c3aed;--accent2:#06b6d4;--ok:#22c55e;--warn:#f59e0b;--bad:#ef4444;--shadow:0 18px 42px rgba(0,0,0,.28)}
:root.light{--bg:#f7f9fc;--panel:#fff;--panel2:#eef3fb;--line:#d8e0ec;--text:#0f172a;--muted:#64748b;--shadow:0 14px 32px rgba(15,23,42,.12)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Inter,-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;letter-spacing:0}button,input,select,textarea{font:inherit}button{cursor:pointer;border:0}
.top{position:sticky;top:0;z-index:50;display:flex;align-items:center;gap:16px;padding:12px 20px;background:rgba(9,15,31,.9);border-bottom:1px solid var(--line);backdrop-filter:blur(16px)}.light .top{background:rgba(247,249,252,.9)}.logo{width:32px;height:32px;border-radius:9px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;font-weight:900}.brand{font-size:18px;font-weight:850}.kpis{display:flex;gap:8px;flex-wrap:wrap}.pill{border:1px solid var(--line);background:var(--panel);color:var(--muted);border-radius:999px;padding:7px 10px;white-space:nowrap}.pill b{color:var(--text);margin-left:6px}.grow{flex:1}.btn{background:var(--panel2);border:1px solid var(--line);color:var(--text);border-radius:8px;padding:8px 12px}.btn:hover{border-color:var(--accent2)}.btn.primary{background:linear-gradient(135deg,var(--accent),#2563eb);border-color:transparent;color:white}.btn.good{background:rgba(34,197,94,.18);border-color:rgba(34,197,94,.5);color:#86efac}.btn.warn{background:rgba(245,158,11,.18);border-color:rgba(245,158,11,.5);color:#fcd34d}.btn.bad{background:rgba(239,68,68,.16);border-color:rgba(239,68,68,.5);color:#fca5a5}.btn.small{font-size:12px;padding:6px 8px}
.wrap{padding:18px 20px 32px}.tabs{display:flex;gap:8px;margin-bottom:16px;border-bottom:1px solid var(--line);overflow:auto}.tab{background:transparent;color:var(--muted);padding:12px 14px;border-bottom:2px solid transparent;white-space:nowrap}.tab.active{color:var(--text);border-color:var(--accent2);font-weight:800}.view{display:none}.view.active{display:block}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;box-shadow:var(--shadow)}.span4{grid-column:span 4}.span5{grid-column:span 5}.span7{grid-column:span 7}.span8{grid-column:span 8}.span12{grid-column:span 12}h2{font-size:15px;margin:0 0 12px}h3{font-size:13px;margin:0 0 8px;color:var(--muted)}.muted{color:var(--muted)}.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center}.control{background:var(--panel2);border:1px solid var(--line);border-radius:8px;color:var(--text);padding:8px 10px}.control input,.control select{background:transparent;border:0;outline:0;color:var(--text);min-width:70px}.metric{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:12px}.metric b{display:block;font-size:27px;margin-top:5px}.log{height:260px;overflow:auto;background:#020617;color:#ccfbf1;border:1px solid var(--line);border-radius:8px;padding:10px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;white-space:pre-wrap}.lanes{display:grid;grid-template-columns:repeat(3,minmax(260px,1fr));gap:12px}.lane-title{display:flex;justify-content:space-between;color:var(--muted);font-weight:800;margin-bottom:8px}.cards{display:flex;flex-direction:column;gap:10px}.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;transition:transform .16s ease,border-color .16s ease,box-shadow .16s ease}.card:hover,.card.selected{transform:translateY(-2px);border-color:var(--accent2);box-shadow:var(--shadow)}.card-head{display:flex;gap:10px;align-items:center}.score{width:48px;height:48px;border-radius:50%;display:grid;place-items:center;background:conic-gradient(var(--accent2) calc(var(--v)*1%),rgba(148,163,184,.2) 0);font-weight:900}.score span{background:var(--panel);width:34px;height:34px;border-radius:50%;display:grid;place-items:center}.domain{font-weight:850;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.badge{display:inline-flex;border-radius:999px;padding:3px 7px;font-size:11px;font-weight:850}.b-boom{background:rgba(6,182,212,.18);color:#67e8f9}.b-win{background:rgba(34,197,94,.18);color:#86efac}.b-watch{background:rgba(245,158,11,.18);color:#fcd34d}.tags{display:flex;gap:5px;flex-wrap:wrap;margin:10px 0}.tag{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:3px 6px}.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.bar{height:10px;background:var(--panel2);border-radius:999px;overflow:hidden}.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:inherit}.audit-row{display:grid;grid-template-columns:170px 1fr 60px;gap:10px;align-items:center;margin:9px 0}.table{width:100%;border-collapse:collapse}.table th,.table td{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}.table th{color:var(--muted);font-size:12px}.advice{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.advice-card{background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:12px}.selected-box{min-height:160px}.domain-result{margin-top:10px;padding:10px;border-radius:8px;background:var(--panel2);border:1px solid var(--line)}
.v3-auto-hero{grid-column:span 12;background:linear-gradient(135deg,rgba(249,115,22,.18),rgba(236,72,153,.18),rgba(124,58,237,.18));border:1px solid rgba(236,72,153,.45);box-shadow:0 18px 44px rgba(124,58,237,.18)}.v3-auto-hero h2{font-size:20px;margin:0 0 8px}.v3-auto-hero textarea{min-height:84px;resize:vertical}.v3-auto-result{margin-top:12px;max-height:420px;overflow:auto}.kw-chip{display:inline-flex;gap:6px;align-items:center;margin:4px;padding:6px 9px;border:1px solid var(--line);border-radius:999px;background:var(--panel2);font-size:12px}.kw-chip b{color:#fcd34d}.run-row{display:grid;grid-template-columns:1.5fr .6fr .6fr .7fr 1.5fr;gap:8px;padding:8px 0;border-bottom:1px solid var(--line);font-size:12px}.run-row.head{color:var(--muted);font-weight:850}.follow-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px}.follow-card{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:13px}.follow-card.go{border-color:rgba(34,197,94,.65);box-shadow:0 0 0 1px rgba(34,197,94,.12)}.follow-card.test{border-color:rgba(245,158,11,.6)}.decision{display:inline-flex;padding:4px 8px;border-radius:999px;font-size:12px;font-weight:900}.decision.go{background:rgba(34,197,94,.18);color:#86efac}.decision.test{background:rgba(245,158,11,.18);color:#fcd34d}.prio{font-size:26px;font-weight:950;color:#fcd34d}.follow-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}.follow-domain{font-weight:900;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.follow-why{margin:8px 0;color:var(--muted);line-height:1.45}.follow-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
@media(max-width:1100px){.span4,.span5,.span7,.span8{grid-column:span 12}.lanes{grid-template-columns:1fr}.advice{grid-template-columns:1fr}.run-row{grid-template-columns:1fr}}@media(max-width:680px){.top{align-items:flex-start;flex-wrap:wrap}.wrap{padding:14px}.grid{grid-template-columns:1fr}.span12,.span8,.span7,.span5,.span4,.v3-auto-hero{grid-column:span 1}}
</style>
</head>
<body>
<header class="top">
  <div class="logo">V3</div><div class="brand">FB SelectPro V3</div>
  <div class="kpis">
    <span class="pill">总入选 <b id="k-total">0</b></span>
    <span class="pill">高分 <b id="k-high">0</b></span>
    <span class="pill">待审核 <b id="k-pending">0</b></span>
    <span class="pill">最近吞吐 <b id="k-throughput">0/s</b></span>
  </div>
  <div class="grow"></div>
  <a class="btn primary" href="/" title="返回总面板">总面板</a>
  <button class="btn" id="themeBtn">明暗</button>
</header>
<main class="wrap">
  <nav class="tabs">
    <button class="tab active" data-view="console">自动发现控制台</button>
    <button class="tab" data-view="follow">自动可跟品</button>
    <button class="tab" data-view="radar">爆款雷达</button>
    <button class="tab" data-view="audit">过滤审计</button>
    <button class="tab" data-view="profit">利润与测试建议</button>
    <button class="tab" data-view="feedback">人工反馈闭环</button>
  </nav>

  <section id="console" class="view active">
    <div class="grid">
      <div class="panel v3-auto-hero">
        <h2>⚡ V3 Auto · 自动抓下拉词 + 自动跑 Facebook 数据</h2>
        <p class="muted">输入种子词后，会并发抓 Amazon / Google / Bing / DuckDuckGo / YouTube / Wikipedia 下拉词，筛选 2026 趋势最强关键词，再直接抓 FB Ad Library 并处理入库。</p>
        <div class="toolbar">
          <textarea class="control" id="autoSeeds" placeholder="vagus nerve, red light therapy, glp-1 alternative" style="flex:1;min-width:360px"></textarea>
          <label class="control">Top N <input id="autoTopN" type="number" value="30" min="1" max="100"></label>
          <label class="control">国家 <input id="autoCountry" value="US" maxlength="2"></label>
          <label class="control">滚动 <input id="autoScrolls" type="number" value="5" min="1" max="20"></label>
          <label class="control"><input id="autoAlphabet" type="checkbox" checked> A-Z</label>
          <label class="control"><input id="autoProcess" type="checkbox" checked> 处理入库</label>
          <button class="btn primary" id="runV3Auto">⚡ 开始自动跑</button>
        </div>
        <div class="v3-auto-result muted" id="autoResult">等待启动</div>
      </div>
      <div class="panel span5">
        <h2>V3 Core 自动分析</h2>
        <div class="toolbar">
          <label class="control">扫描量 <input id="runLimit" type="number" value="800" min="1" max="20000"></label>
          <label class="control">并发 <input id="runConc" type="number" value="96" min="4" max="300"></label>
          <label class="control">批量 <input id="runBatch" type="number" value="500" min="50" max="5000"></label>
          <button class="btn primary" id="runCore">一键运行 V3 Core</button>
        </div>
        <p class="muted" id="runState">空闲</p>
        <div class="log" id="runLog"></div>
      </div>
      <div class="panel span7">
        <h2>运行指标</h2>
        <div class="grid">
          <div class="metric span4">Accepted<b id="m-accepted">0</b></div>
          <div class="metric span4">Rejected<b id="m-rejected">0</b></div>
          <div class="metric span4">Failed<b id="m-failed">0</b></div>
          <div class="metric span4">Elapsed<b id="m-elapsed">0s</b></div>
          <div class="metric span4">Throughput<b id="m-throughput">0/s</b></div>
          <div class="metric span4">Run ID<b id="m-runid" style="font-size:15px">-</b></div>
        </div>
      </div>
      <div class="panel span12">
        <h2>独立站快速校验</h2>
        <div class="toolbar">
          <input class="control" id="checkUrl" placeholder="https://brand.com/products/item" style="min-width:360px">
          <button class="btn" id="checkDomain">校验域名</button>
        </div>
        <div id="domainResult" class="domain-result muted">等待输入</div>
      </div>
    </div>
  </section>

  <section id="follow" class="view">
    <div class="panel span12">
      <h2>🤖 自动智能化选品 · 系统已判好可跟品</h2>
      <p class="muted">这里不是人工审核队列，只列出系统判断“可以跟”的产品。按优先级排序，看到“立即跟品”就可以拆品、拆素材、上测试。</p>
      <div class="toolbar">
        <label class="control">最低优先级 <input id="followMin" type="number" value="74" min="0" max="100"></label>
        <label class="control">数量 <input id="followLimit" type="number" value="50" min="1" max="200"></label>
        <button class="btn primary" id="refreshFollow">刷新可跟品</button>
      </div>
      <div id="followSummary" class="muted" style="margin:10px 0">加载中...</div>
      <div class="follow-grid" id="followList"></div>
    </div>
  </section>

  <section id="radar" class="view">
    <div class="toolbar">
      <label class="control">最低分 <input id="minScore" type="number" value="0" min="0" max="100"></label>
      <label class="control">引擎 <select id="engineFilter"><option value="">全部</option><option>shopify</option><option>woocommerce</option><option>bigcommerce</option><option>magento</option><option>shopline</option><option>wixstores</option><option>custom_dtc</option></select></label>
      <button class="btn primary" id="refreshRadar">刷新雷达</button>
    </div>
    <div class="lanes" id="radarLanes"></div>
  </section>

  <section id="audit" class="view">
    <div class="grid">
      <div class="panel span5">
        <h2>拒绝原因 Top</h2>
        <div id="rejectTop"></div>
      </div>
      <div class="panel span7">
        <h2>独立站引擎分布</h2>
        <div id="engineDist"></div>
      </div>
      <div class="panel span12">
        <h2>最近运行</h2>
        <table class="table"><tbody id="latestRun"></tbody></table>
      </div>
    </div>
  </section>

  <section id="profit" class="view">
    <div class="grid">
      <div class="panel span4 selected-box">
        <h2>当前选中</h2>
        <div id="selectedPick" class="muted">在爆款雷达里点击一张卡片</div>
      </div>
      <div class="panel span8">
        <h2>测试建议队列</h2>
        <div class="advice" id="adviceList"></div>
      </div>
    </div>
  </section>

  <section id="feedback" class="view">
    <div class="grid">
      <div class="panel span4">
        <h2>反馈统计</h2>
        <div id="reviewStats"></div>
      </div>
      <div class="panel span8">
        <h2>待审核高分样本</h2>
        <table class="table"><thead><tr><th>域名</th><th>分</th><th>标签</th><th>建议</th><th>操作</th></tr></thead><tbody id="reviewQueue"></tbody></table>
      </div>
    </div>
  </section>
</main>

<script>
const API='/api/v3/core';
const state={stats:null,picks:[],followItems:[],selected:null,running:false};
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
async function j(url,opt){const r=await fetch(url,opt);const txt=await r.text();let d={};try{d=txt?JSON.parse(txt):{}}catch(e){d={raw:txt}}if(!r.ok)throw new Error(d.detail||d.error||txt);return d}
async function copyText(txt,label='内容'){
  const value=String(txt||'');
  try{await navigator.clipboard.writeText(value)}
  catch(e){const ta=document.createElement('textarea');ta.value=value;ta.style.position='fixed';ta.style.left='-9999px';document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove()}
  alert(label+' 已复制，可直接粘贴到 ChatGPT / Claude / AI 数据库');
}
function getFollowItem(id){return (state.followItems||[]).find(x=>String(x.ad_id)===String(id))||{};}
function copyPrompt(id,key){
  const p=getFollowItem(id);const ai=(((p.follow_playbook||{}).ai_ingest)||{});const pack=ai.prompt_pack||{};
  const map={master_operator:'总控赚钱Prompt',product_decision:'选品决策Prompt',creative_director:'素材Prompt',video_production:'视频Prompt',landing_page:'落地页Prompt',geo_strategy:'GEO Prompt',supply_chain:'供应链利润Prompt',compliance:'合规Prompt',learning_review:'复盘学习Prompt',all:'完整Prompt包',payload:'产品Payload'};
  const txt=key==='payload'?JSON.stringify(ai.payload||{},null,2):key==='all'?JSON.stringify(pack,null,2):(pack[key]||ai.prompt||'');
  copyText(txt,map[key]||key);
}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('.view').forEach(v=>v.classList.toggle('active',v.id===b.dataset.view));});
$('themeBtn').onclick=()=>document.documentElement.classList.toggle('light');
function log(line){const el=$('runLog');el.textContent+=line+'\n';el.scrollTop=el.scrollHeight}
function badge(label){if(label==='潜力爆发品')return 'b-boom';if(label==='已验证爆款')return 'b-win';return 'b-watch'}
function adviceClass(a){return a==='立即测'?'good':a==='放弃'?'bad':'warn'}

async function loadStats(){
  const d=await j(API+'/stats');state.stats=d;
  $('k-total').textContent=d.total_picks||0;$('k-high').textContent=d.high_score||0;$('k-pending').textContent=d.pending_review||0;
  const lr=d.latest_run||{};$('k-throughput').textContent=((lr.requested||0)/Math.max(lr.elapsed_sec||1,1)).toFixed(1)+'/s';
  renderAudit();renderFeedback();
}
async function loadPicks(){
  const min=+$('minScore').value||0;const eng=$('engineFilter').value;
  const d=await j(`${API}/picks?limit=500&min_score=${min}${eng?'&engine='+encodeURIComponent(eng):''}`);
  state.picks=d.items||[];renderRadar();renderAdvice();renderFeedback();
}
async function loadFollowPicks(){
  const min=+$('followMin').value||74;const limit=+$('followLimit').value||50;
  $('followSummary').textContent='系统判断中...';
  const d=await j(`${API}/follow-picks?limit=${limit}&min_priority=${min}`);
  state.followItems=d.items||[];
  const s=d.summary||{};
  const risks=s.risk_mix||{}, geos=s.geo_mix||{};
  const top=(s.top_3_actions||[]).map(x=>`<li><b>${esc(x.domain||'')}</b> · ${esc(x.decision||'')} · ${esc(x.action||'')}</li>`).join('');
  $('followSummary').innerHTML=`<div><b>老板决策：</b>${esc(s.portfolio_verdict||'')}</div>
    <div>共 ${d.count||0} 个 · 立即 ${s.immediate||0} · 小测 ${s.test||0} · 观察 ${s.watch||0} · Day1预算 ${esc(s.total_recommended_budget_day1||'')}</div>
    <div class="tags"><span class="tag">合规风险 ${risks.compliance||0}</span><span class="tag">物流风险 ${risks.logistics||0}</span><span class="tag">饱和风险 ${risks.saturation||0}</span><span class="tag">低毛利 ${risks.low_margin||0}</span>${Object.entries(geos).map(([k,v])=>`<span class="tag">${esc(k)} ${v}</span>`).join('')}</div>
    <ul>${top}</ul>`;
  $('followList').innerHTML=(d.items||[]).map(followCard).join('')||'<div class="muted">暂无达到阈值的可跟品，降低最低优先级或先跑 V3 Auto。</div>';
}
async function runCore(){
  if(state.running)return;state.running=true;
  const payload={limit:+$('runLimit').value||800,concurrency:+$('runConc').value||96,batch_size:+$('runBatch').value||500};
  $('runState').textContent='运行中';$('runLog').textContent='';log('V3 Core started '+new Date().toLocaleTimeString());
  const t0=performance.now();let tick=setInterval(()=>{$('runState').textContent='运行中 '+((performance.now()-t0)/1000).toFixed(1)+'s'},500);
  try{
    const d=await j(API+'/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    $('m-accepted').textContent=d.accepted;$('m-rejected').textContent=d.rejected;$('m-failed').textContent=d.failed;
    $('m-elapsed').textContent=d.elapsed_sec+'s';$('m-throughput').textContent=d.throughput_per_sec+'/s';$('m-runid').textContent=d.run_id;
    log(JSON.stringify({accepted:d.accepted,rejected:d.rejected,failed:d.failed,throughput:d.throughput_per_sec},null,2));
    $('runState').textContent='完成';await loadStats();await loadPicks();
  }catch(e){log('ERROR '+e.message);$('runState').textContent='失败: '+e.message}
  finally{clearInterval(tick);state.running=false}
}
$('runCore').onclick=runCore;

function renderAutoResult(d){
  const kws=d.top_keywords||[];const rows=(d.scrape||{}).results||[];
  let html=`<div><b>完成</b> · 生成 ${d.generated_count||0} · 选中 ${d.selected_count||0} · 入库 ${d.ingested||0} · 保存 ${(d.scrape||{}).saved||0}</div>`;
  html+=`<h3>2026 Top Keywords</h3><div>${kws.slice(0,80).map(k=>`<span class="kw-chip"><b>${k.trend_score||0}</b>${esc(k.keyword||'')}</span>`).join('')||'<span class="muted">暂无关键词</span>'}</div>`;
  html+=`<h3 style="margin-top:12px">抓取结果</h3><div class="run-row head"><span>关键词</span><span>状态</span><span>广告</span><span>耗时</span><span>文件</span></div>`;
  html+=rows.map(r=>`<div class="run-row"><span>${esc(r.keyword||'')}</span><span>${r.ok?'通过':'异常'} ${esc(r.status||'')}</span><span>${r.count||0}</span><span>${r.duration_s||0}s</span><span class="muted">${esc(r.file||'—')}</span></div>`).join('')||'<div class="muted">暂无抓取结果</div>';
  $('autoResult').innerHTML=html;
}
async function runV3Auto(){
  const payload={
    text:$('autoSeeds').value.trim(),
    top_n:+$('autoTopN').value||30,
    country:($('autoCountry').value||'US').trim().toUpperCase(),
    max_scrolls:+$('autoScrolls').value||5,
    alphabet:$('autoAlphabet').checked,
    process:$('autoProcess').checked,
    ingest:true,
    fast:true,
    delay_sec:.15,
    refresh_every:10,
  };
  $('autoResult').textContent='运行中：抓下拉词 → 趋势排序 → FB 抓取 → 处理入库...';
  try{const d=await j('/v3/auto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});renderAutoResult(d);await loadStats();await loadPicks();}
  catch(e){$('autoResult').innerHTML=`<span style="color:#fca5a5">失败: ${esc(e.message||e)}</span>`}
}
$('runV3Auto').onclick=runV3Auto;

function renderRadar(){
  const groups={'潜力爆发品':[], '已验证爆款':[], '观察池':[]};
  state.picks.forEach(p=>(groups[p.status_label]||groups['观察池']).push(p));
  $('radarLanes').innerHTML=Object.entries(groups).map(([label,items])=>`<div><div class="lane-title"><span>${label}</span><span>${items.length}</span></div><div class="cards">${items.map(card).join('')||'<div class="muted">暂无</div>'}</div></div>`).join('');
}
function card(p){
  const score=p.adjusted_score??p.quality_score??0;const advice=p.test_advice||{};
  return `<article class="card ${state.selected?.ad_id===p.ad_id?'selected':''}" onclick="selectPick('${esc(p.ad_id)}')">
    <div class="card-head"><div class="score" style="--v:${score}"><span>${score}</span></div><div style="min-width:0"><div class="domain">${esc(p.landing_domain)}</div><div class="muted">${esc(p.shop_engine)} · ${p.active_days||0}d · ${esc(p.price_band)} · ${esc(p.volume_class)}</div></div></div>
    <div class="tags"><span class="badge ${badge(p.status_label)}">${esc(p.status_label)}</span><span class="tag">$${p.price_value??'?'}</span><span class="tag">growth ${p.impressions_growth_pct||0}%</span><span class="tag">${esc(advice.level||'')}</span></div>
    <div class="muted">${(p.reasons||[]).slice(0,4).map(esc).join(' / ')}</div>
    <div class="actions"><button class="btn good small" onclick="event.stopPropagation();review('${esc(p.ad_id)}','approve')">approve</button><button class="btn warn small" onclick="event.stopPropagation();review('${esc(p.ad_id)}','watch')">watch</button><button class="btn bad small" onclick="event.stopPropagation();review('${esc(p.ad_id)}','skip')">skip</button><a class="btn small" href="${esc(p.landing_url)}" target="_blank" onclick="event.stopPropagation()">打开</a></div>
  </article>`;
}
function followCard(p){
  const d=p.auto_decision||{};const pb=p.follow_playbook||{};const ai=pb.ai_ingest||{};const sc=p.executive_scorecard||{};const layers=sc.layers||{};const cls=d.decision==='立即跟品'?'go':d.decision==='小预算跟测'?'test':'';const score=p.adjusted_score??p.quality_score??0;
  const plays=(p.reasons||[]).filter(x=>String(x).startsWith('2026打法')).slice(0,6).map(x=>`<span class="tag">${esc(String(x).replace('2026打法:',''))}</span>`).join('');
  const creative=(pb.creative_brief||[]).slice(0,3).map(x=>`<li>${esc(x)}</li>`).join('');
  const video=(pb.video_brief||[]).slice(0,2).map(x=>`<li>${esc(x)}</li>`).join('');
  const geo=(pb.geo||[]).map(x=>`<span class="tag">${esc(x)}</span>`).join('');
  const layerRows=Object.entries(layers).map(([k,v])=>`<div class="audit-row"><span>${esc(k)} ${esc(v.label||'')}</span><div class="bar"><i style="width:${v.score||0}%"></i></div><b>${v.score||0}</b></div>`).join('');
  const flags=[...(sc.green_flags||[]).slice(0,2).map(x=>`<span class="tag">${esc(x)}</span>`),...(sc.red_flags||[]).slice(0,2).map(x=>`<span class="tag" style="border-color:#ef4444;color:#fecaca">${esc(x)}</span>`)].join('');
  const plan=(pb.next_72h_action_plan||[]).map(x=>`<li><b>${esc(x.window||'')}</b> ${esc(x.owner||'')}：${esc(x.action||'')} → ${esc(x.output||'')}</li>`).join('');
  const ladder=(pb.budget_ladder||[]).map(x=>`<li><b>${esc(x.stage||'')}</b> ${esc(x.budget||'')} / ${esc(x.duration||'')}：${esc(x.pass||'')}</li>`).join('');
  const kills=(pb.kill_criteria||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  const pack=ai.prompt_pack||{};
  const promptPack=Object.entries(pack).map(([k,v])=>`<details><summary>${esc(k)}</summary><pre>${esc(v)}</pre></details>`).join('');
  return `<article class="follow-card ${cls}">
    <div class="follow-head"><div style="min-width:0"><div class="follow-domain">${esc(p.landing_domain)}</div><div class="muted">${esc(p.shop_engine)} · ${p.active_days||0}d · ${esc(p.price_band)} · ${esc(p.volume_class)}</div></div><div class="prio">${d.priority||0}</div></div>
    <div style="display:flex;gap:6px;align-items:center;margin-top:8px"><span class="decision ${cls}">${esc(d.decision||'观察')}</span><span class="tag">原分 ${score}</span><span class="tag">growth ${p.impressions_growth_pct||0}%</span></div>
    <div class="follow-why"><b>主理人判断：</b>${esc(sc.operator_verdict||d.operator_verdict||'')} · ${esc(sc.money_making_logic||d.money_making_logic||'')}</div>
    <div class="tags">${plays}${flags}</div>
    <div><b>预算：</b>${esc(d.budget||'')}<br><b>下一步：</b>${esc(d.next_step||'')}</div>
    <div class="follow-why"><b>打法：</b>${esc(pb.product_follow||'')} · ${esc(pb.angle||'')}<br><b>Offer：</b>${esc(pb.offer||'')}<br><b>GEO：</b><span class="tags">${geo}</span></div>
    <details><summary>八层赚钱评分卡 / 详细理由</summary><div class="follow-why">${esc(d.why||'')}</div>${layerRows}</details>
    <details><summary>72小时作战卡 / 预算阶梯 / Kill Criteria</summary><ul>${plan}</ul><b>预算阶梯：</b><ul>${ladder}</ul><b>Kill Criteria：</b><ul>${kills}</ul></details>
    <details><summary>素材制作 / 视频制作</summary><b>素材制作：</b><ul>${creative}</ul><b>视频制作：</b><ul>${video}</ul></details>
    <details><summary>ChatGPT / AI 入库 Prompt & Payload</summary><div class="follow-why">${esc(ai.prompt||'')}</div><pre>${esc(JSON.stringify(ai.payload||{},null,2))}</pre></details>
    <details><summary>完整 AI 提示词包：选品/素材/视频/落地页/GEO/供应链/合规/复盘</summary>${promptPack}</details>
    <div class="follow-actions"><button class="btn primary small" onclick="copyPrompt('${esc(p.ad_id)}','master_operator')">复制总控</button><button class="btn small" onclick="copyPrompt('${esc(p.ad_id)}','creative_director')">复制素材</button><button class="btn small" onclick="copyPrompt('${esc(p.ad_id)}','video_production')">复制视频</button><button class="btn small" onclick="copyPrompt('${esc(p.ad_id)}','landing_page')">复制落地页</button><button class="btn small" onclick="copyPrompt('${esc(p.ad_id)}','supply_chain')">复制供应链</button><button class="btn small" onclick="copyPrompt('${esc(p.ad_id)}','all')">复制全套</button><button class="btn small" onclick="copyPrompt('${esc(p.ad_id)}','payload')">复制Payload</button></div>
    <div class="follow-actions"><a class="btn primary small" href="${esc(p.landing_url)}" target="_blank">打开落地页</a><button class="btn good small" onclick="review('${esc(p.ad_id)}','approve')">标记跟品</button><button class="btn warn small" onclick="review('${esc(p.ad_id)}','watch')">观察</button><button class="btn bad small" onclick="review('${esc(p.ad_id)}','skip')">跳过</button></div>
  </article>`;
}
window.selectPick=id=>{state.selected=state.picks.find(p=>p.ad_id===id)||null;renderRadar();renderAdvice();};
async function review(id,action){await j(`${API}/review/${encodeURIComponent(id)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,note:'v3-dashboard'})});await loadStats();await loadPicks();}

function renderAudit(){
  const s=state.stats||{};const rt=s.reject_top||[];const max=Math.max(1,...rt.map(x=>x.n||0));
  $('rejectTop').innerHTML=rt.map(x=>`<div class="audit-row"><span>${esc(x.reject_reason)}</span><div class="bar"><i style="width:${(x.n/max*100).toFixed(1)}%"></i></div><b>${x.n}</b></div>`).join('')||'<div class="muted">暂无拒绝记录</div>';
  const eng=s.engines||[];const em=Math.max(1,...eng.map(x=>x.n||0));
  $('engineDist').innerHTML=eng.map(x=>`<div class="audit-row"><span>${esc(x.shop_engine)}</span><div class="bar"><i style="width:${(x.n/em*100).toFixed(1)}%"></i></div><b>${x.n}</b></div>`).join('')||'<div class="muted">暂无引擎数据</div>';
  const lr=s.latest_run||{};$('latestRun').innerHTML=['run_id','status','requested','accepted','rejected','failed','elapsed_sec','started_at','finished_at'].map(k=>`<tr><th>${k}</th><td>${esc(lr[k]??'-')}</td></tr>`).join('');
}
function renderAdvice(){
  const rows=(state.selected?[state.selected]:state.picks).slice(0,9);
  $('selectedPick').innerHTML=state.selected?`<b>${esc(state.selected.landing_domain)}</b><p>${esc(state.selected.status_label)} · ${state.selected.quality_score}分 · ${esc(state.selected.shop_engine)}</p><p class="muted">${esc((state.selected.test_advice||{}).reason||'')}</p>`:'在爆款雷达里点击一张卡片';
  $('adviceList').innerHTML=rows.map(p=>{const a=p.test_advice||{};return `<div class="advice-card"><h3>${esc(p.landing_domain)}</h3><b class="${adviceClass(a.level)}">${esc(a.level||'-')}</b><p>${esc(a.budget||'')}</p><p class="muted">${esc(a.reason||'')}</p></div>`}).join('')||'<div class="muted">暂无建议</div>';
}
function renderFeedback(){
  const rev=(state.stats||{}).reviews||{};$('reviewStats').innerHTML=['approve','watch','skip'].map(k=>`<div class="metric">${k}<b>${rev[k]||0}</b></div>`).join('');
  $('reviewQueue').innerHTML=state.picks.filter(p=>!p.review_action).slice(0,30).map(p=>`<tr><td>${esc(p.landing_domain)}</td><td>${p.adjusted_score??p.quality_score}</td><td>${esc(p.status_label)}</td><td>${esc((p.test_advice||{}).level||'')}</td><td><button class="btn good small" onclick="review('${esc(p.ad_id)}','approve')">approve</button> <button class="btn warn small" onclick="review('${esc(p.ad_id)}','watch')">watch</button> <button class="btn bad small" onclick="review('${esc(p.ad_id)}','skip')">skip</button></td></tr>`).join('')||'<tr><td colspan="5" class="muted">暂无待审核</td></tr>';
}
$('refreshRadar').onclick=loadPicks;$('refreshFollow').onclick=loadFollowPicks;$('followMin').oninput=()=>setTimeout(loadFollowPicks,120);$('followLimit').oninput=()=>setTimeout(loadFollowPicks,120);$('minScore').oninput=()=>setTimeout(loadPicks,100);$('engineFilter').onchange=loadPicks;
$('checkDomain').onclick=async()=>{const url=$('checkUrl').value.trim();if(!url)return;const d=await j(`${API}/domain-check?url=${encodeURIComponent(url)}`);$('domainResult').innerHTML=`<b>${d.ok?'通过':'拒绝'}</b> · ${esc(d.domain||'')} · ${esc(d.shop_engine||d.reject_reason||d.error||'')}`};
async function boot(){await loadStats();await loadPicks();await loadFollowPicks();setInterval(loadStats,30000)}
boot();
</script>
</body>
</html>"""

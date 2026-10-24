---
name: fb-ad-selection-pipeline
description: |
  Facebook Ad Library product selection and profit pre-screening.
  Offline-first: read the local SQLite archive for demand signals, scrape only
  when the data is missing or stale, then gate candidates on real unit economics
  (price + COGS) before any spend decision.
  Output is a ranked shortlist plus optional evidence slots for a downstream
  deep-verify; final GO is never granted by the FB score alone.
  Use for FB product research, ad selection, picks, competitor ad longevity,
  D2C storefront ad scans, or profit pre-screening.
---

# FB Ad Selection Pipeline · picks_cli

> **一句话**：先查库找信号，缺数据才抓，再用真实售价与成本过利润闸门。
> FB 推荐分只回答“值得研究吗”，不回答“已经能赚钱吗”。

## 路径协议（四层，按序执行）

```
[0] 内存直扫   python3 memory_scan.py --bench/--library   ← 无 HTTP；词库进内存扫 FB
[1] 查库出信号   python3 picks_cli.py picks/keyword      ← 默认；离线只读
[2] 缺词或陈旧   fb-scan-pipeline-v8.py --v3-*           ← no_scan 或超过 14 天
[3] 利润预筛     python3 picks_cli.py profit-screen       ← 真实售价 + COGS 必填
[4] 人看界面     --serve → 127.0.0.1:8000                ← 仅看 dashboard/导出（非默认）
```


## [1] 选品信号（picks_cli.py · 离线直读）

```bash
cd "$(git rev-parse --show-toplevel)"

# Top 信号表：0-120 分 + orbit + 广告寿命 + 痛点；不是利润结论
# ads≥30 的行自动标 ≈（密度截断），keyword 视图带 [密度截断·比率才可信] tag
# --light-only：剔除枕/毯/家具/器械等大件重货（📦标记），只留直发友好小件——快钱前置过滤
python3 picks_cli.py picks --days 30 --min-score 85 --limit 20 --light-only
python3 picks_cli.py picks --days 0 --min-score 100 --json   # 全历史高分，机读

# 单词竞争全景（= /scans/{kw} 同源）：独立站几家、最老广告几天、平台压不压
python3 picks_cli.py keyword "knee compression sleeve"

# 品牌档案（首见/广告量/orbit/关联词）
python3 picks_cli.py brand kliyra.com

# → gshopping-rising r10 深度验证：export-r8 预填 FB 三证据槽（带来源与 checked_at）
#   r10 起可同时 ingest amz 的 ABA 槽：--aba-slots（来自 amz-keyword-scanner export-r8）+
#   --fb-slots（本命令），merge_external_slots 只填 unknown 槽，人工值优先。
python3 picks_cli.py export-r8 --keywords "knee compression sleeve,foot massager" \
  --out /tmp/fb_slots.json

# 体检 / 自测
python3 picks_cli.py doctor
python3 picks_cli.py selftest
```

### Agent 执行协议

1. **新鲜度**：scan >14 天自动标 `[STALE]` —— 引用时必须带上扫描日期，STALE 数据只能当
   「历史竞争面参考」，不得表述为当前事实；重要决策先重扫（命令见 STALE 提示）。
2. **平台剔除**：Temu/Amazon/Shein/马甲域（shoptemu 类）自动归 PLAT，不计入独立站竞争数；
   但要在结论里报告平台压制情况（如「Temu 22 条 DPA 在场」）。
3. **品牌自播降噪**：keyword≈brand 的行默认剔除（那是品牌广告，不是品类需求）。
4. **不编造**：`no_scan` 就是没有，给抓取命令，绝不猜数字。

### [1.5] 跟品时机四象限（寿命字段的判定口径）`[本机] 高`

`picks` 已输出广告寿命，但寿命是**字段**，不是**动作**。同样的高分要拆成三种完全不同的动作：

```sql
-- ad_archive 实测：16637 行，active_days / start_date_iso 100% 覆盖
select count(*) ads, count(distinct domain) sites,
  1.0*sum(case when active_days>=90 then 1 else 0 end)/count(*)  long_run_rate,
      sum(case when active_days>=365 then 1 else 0 end)          printer_count,
  1.0*sum(case when julianday(:scan_date)-julianday(start_date_iso)<45
               then 1 else 0 end)/count(*)                        new_share
from ad_archive
where keyword=:kw and start_date_iso is not null;
```

**核心逻辑：Meta 不会让亏钱的广告跑 90 天。** 竞品真实 ROAS 看不到，但"这条广告活了 503 天"是硬事实——这是本机唯一可观测的竞品盈利性代理指标（对齐 r11「广告寿命=利润」）。

| 象限 | 条件 | 动作 |
|---|---|---|
| **成熟印钞** | `long_run≥50%` 且 `new_share<15%` | **最佳跟品**：素材已验证、无新增竞争，直接逆向 TOP 素材 |
| **★活跃窗口** | `long_run≥40%` 且 `new_share≥15%` | 边验证边跟，最快启动；窗口会关，2–4 周内进 |
| **新涌入未验证** | `new_share≥40%` 且 `long_run<30%` | 观察。等 45 天看谁活下来，再跟活的那个 |
| 混合待查 | 其余 | 补 Trends + 素材实查 |

存活分层（全库实测）：<7 天 21.9%（尚未证明任何事）· 7–30 天 32.9% · 30–90 天 28.8% · 90 天–1 年 13.7%（**在赚钱**）· 1 年+ 2.5%（**424 条印钞机，素材已被市场彻底验证**）。

门禁：`long_run_rate<15%` 且 `new_share>50%` → 全是新广告试水、无人跑通，**降级观望**，别当先烈。`printer_count≥5` → 去逆向那几条素材。

实测对照（2026-08-04 快照，`ads≥12 & <30` 避开截断）：`discomfort in your feet, knees, and back`（29 广告 / 21 站 / 长跑 **90%** / **19 条印钞** / 新投 0%）= 成熟印钞，该立刻逆向跟品；`tired of living with missing teeth?`（29 广告 / 9 站 / 长跑 **0%** / 新投 69%）= 新涌入未验证，该按住手等 45 天。**两者广告数相同、密度分几乎一致，动作完全相反。**

### [1.6] 三个必须规避的数据陷阱 `[本机] 高`

1. **密度截断**：927 个关键词中 113 个（12%）的 `ad_count` 恰好 = 30，是抓取上限而非真实密度。凡等于上限的词须标 `密度截断`，广告密度分不作为区分依据，改用 `long_run_rate` / `printer_count` / `collation_count`（变体数）/ 跨关键词复现次数。
2. **品牌词污染 + join 放大（两个坑连在一起）** `[本机] 高`
   `keyword` 混有 `learned_keywords.kw_type='brand'` 3601 条，做品类判断必须过滤，否则 `Häagen-Dazs`、`Cologuard®` 会被当品类信号（已实测出现）。此规则与既有「品牌自播降噪」互补：那条按 keyword≈brand 剔行，这条按词性过滤。
   **但过滤方式不能用 join**：`learned_keywords` 9254 行只有 5337 个唯一 keyword，`aging` 重复 **125 行**、`at work` 85 行、`muscle tension` 22 行，直接 join 会产生笛卡尔积。
   ```sql
   -- ✗ 错：28 条广告 × 22 条词表行 = 虚报 616 条
   from ad_archive a join learned_keywords l on l.keyword=a.keyword where l.kw_type in ('product','pain')
   -- ✓ 对：DISTINCT 子查询
   from ad_archive a
   where a.keyword in (select distinct keyword from learned_keywords where kw_type in ('product','pain'))
   ```
   放大最高 125 倍，`ads`/长跑条数/印钞条数/变体数全废；**比率型指标（长跑率、new_share）因分子分母同步放大而侥幸不变**，所以比率看着正常、绝对数全错，极难察觉。自查：任何词 `ads` > 抓取上限（本库 30）即为放大信号。
3. **花费字段不可用**：`spend_lower/upper`、`imp_lower/upper`、`video_duration_sec` 覆盖率实测 **0/16637**。任何"对手日耗 $X / 曝光 Y 万"都是编造，禁止输出。`total_active_time` 仅 14%，只作辅助。

**寿命的边界**：长跑只证明"没亏到被关"，**不能反推 ROAS 或利润率数额**。它是方向性代理指标，不替代 [3] 的 r9 利润闸门。
5. **信号不等于利润**：不得把推荐分、广告数或长寿广告直接表述成“能赚钱”。
6. **最终授权**：只有 gshopping-rising `MONEY_DECISION.md` 的 `GO_TEST` 才能触发花钱测试。
7. **输出格式**：先写信号强弱与数据日期，再写利润验证状态和下一证据缺口。

## [0] 内存扫描（推荐抓取入口 · 无 HTTP）

```bash
cd "$(git rev-parse --show-toplevel)"

# 小样本测速 + 一天产能估算
python3 memory_scan.py --bench --limit 8 --workers 4

# 词库进内存，纯 Python 扫 FB Ad Library（不启 8000）
python3 memory_scan.py --library --limit 50 --workers 5

# 指定词
python3 memory_scan.py --keywords "knee brace,posture corrector" --workers 3

# 持续跑（估一天）
python3 memory_scan.py --library --limit 2000 --workers 5 --hours 2
```

> **r3 resilience（2026-08-29）**：L0 直连不可用时自动落到 L1 stealthy（结果 source 标 `+L0-down-fallback`）；
> 只有 L0+L1 双双失败才报 `l0_unavailable`，且 error 字段带可执行恢复指引（查代理池 / `--v3-scrape --scrape-headful`）。

要点：
- **无 FastAPI / 无 8000**；结果先进 `MemoryStore`，可选 flush 到 `logs/memory_scan_*.json`
- 复用 scrapling 三层引擎 + 现有代理池
- 输出含 `kw_per_hour` 与 conservative/realistic/aggressive 日产能
- 本机历史峰值约 **225–240 kw/h** → 现实日产能约 **3000–3500 词/天**
- 短 bench 若全走 L0-direct 会虚高（数千 kw/h）；以 ≥50 词、≥10 分钟实测为准

## [2] 抓取（V3 直连，缺数据或陈旧才用）

```bash
cd "$(git rev-parse --show-toplevel)"

# 指定关键词直连抓取（最常用）
python3 fb-scan-pipeline-v8.py --v3-scrape "red light therapy, posture corrector" \
  --v3-country US --v3-max-scrolls 5 --max-pages 1

# 自动读原词库/队列抓一批
python3 fb-scan-pipeline-v8.py --v3-scrape-library --scrape-limit 20 \
  --v3-country US --v3-max-scrolls 5 --max-pages 1

# seed 自动扩词后抓
python3 fb-scan-pipeline-v8.py --v3-auto-scrape "vagus nerve, glp-1 alternative" \
  --v3-top-n 30 --v3-country US --v3-max-scrolls 5 --max-pages 1

# 队列式后台抓取
python3 fb-scan-pipeline-v8.py --auto-scrape --scrape-limit 5 --max-pages 1

# 抓完补处理 / 完整处理链
python3 fb-scan-pipeline-v8.py --process-raw --update-excel --update-intel
python3 fb-scan-pipeline-v8.py --full-pipeline
```

抓完即可回到 [1] 查库。调试浏览器卡点加 `--scrape-headful`；只抓原始数据加 `--v3-no-process`。


## [3] r9 利润预筛（真实输入必填）

```bash
python3 picks_cli.py profit-screen "knee compression sleeve" \
  --aov 44 --cogs-low 4 --cogs-high 6 \
  --structure pain_device \
  --retail-source "https://competitor.example/product" \
  --supplier-source "https://detail.1688.com/offer/example.html"
```

> **r3 现金通道（2026-08-29）**：输出新增 `现金通道` 行——`⚡快钱通道`（直发 BE ≤2.8，
> 中国直发 7-14 天、小批量快测快回款）vs `🐢慢通道`（直发不成立，须美仓 bulk 备货压资金）。
> 快钱打法（趋势+体积小+快速回款）只做 ⚡；🐢 品要么轻量化改造（降克重/拆件/拉 AOV），要么放弃。

输出 downside breakeven ROAS、目标 CAC、目标 ROAS、利润缓冲与最优履约模式，并给出：

- `KILL_UNIT_ECON`：真实输入下数学不成立。
- `HOLD_NO_FB_SCAN` / `HOLD_STALE_FB`：竞争证据缺失或陈旧。
- `HOLD_WEAK_FB_EVIDENCE` / `HOLD_CROWDED_FB`：需求过弱或竞争过密。
- `READY_FOR_DEEP_VERIFY`：只代表可进入终审，不代表 GO。

禁止使用占位 URL、猜测售价或猜测成本做真实决策。预筛通过后执行 `export-r8`，再交给 `gshopping-rising/scripts/deep_verify.py ingest`。

## [4] Dashboard（仅人看）

```bash
python3 fb-scan-pipeline-v8.py --serve --host 127.0.0.1 --port 8000
```

不要把 8000 服务当默认数据路径——picks_cli 与它同源（同一批模块/文件），离线即可。

## 与其它 skill 的分工

| skill | 角色 | 何时用 |
|---|---|---|
| **本 skill（旧目录 NEW）** | FB 数据库 + 信号出口 + 抓取 + 利润预筛 | FB 选品默认入口 |
| gshopping-rising | Google 侧发现 + r9 利润终审 | 本 skill 供 FB 槽；最终 GO 由其 ingest 决定 |
| fb-ad-scrape-fast | 会话式 D2C 加速抓取器 | 明确要「只抓独立站卖货广告」的抓取会话 |
| fb-ad-picker | xlsx 周报口径选品复盘 | 读 facebook-ad-keywords-master.xlsx 的批量分析 |

## 操作边界

- 默认不改服务端代码（`pipeline/server.py` / `pipeline/v2_scrape_fast.py`），用户点名才动。
- picks_cli 永不写库（sqlite `mode=ro`）；WAL 大于 300MB 只提示，不自动 checkpoint。
- `profit-screen` 复用 gshopping-rising 的唯一利润模型，不复制第二套阈值。
- 大批量抓取小批次迭代；风控/速率下降时调低 `--scrape-limit` / `--v3-top-n`。
## 文件地图

```
NEW/
├── memory_scan.py           # ★ 纯内存扫描入口（无 HTTP）
├── picks_cli.py             # ★ 信号/利润预筛出口（picks/keyword/brand/export-r8/profit-screen）
├── fb-scan-pipeline-v8.py   # 抓取/处理 CLI 入口（内部 pipeline/ v9 模块）
├── pipeline/                # v9 业务模块（scraper/scoring/insights/playbook/recommendations…）
├── pipeline.db              # SQLite（当月写库；历史在 shards/）
├── facebook-ad-keywords-master.xlsx   # 周报口径出口（fb-ad-picker 读这个）
└── logs/ · shards/ · launchd/
```

## 验证

```bash
python3 picks_cli.py selftest
python3 picks_cli.py doctor
python3 memory_scan.py --bench --limit 6 --workers 3
```

要求离线自测全绿。`doctor` 的 freshness FAIL 表示必须重扫，不得把历史竞争面当作当前事实。


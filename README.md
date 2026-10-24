# FB Ad Library Pipeline

Turn Facebook's public Ad Library into a product-research dataset: scrape ads,
score them by how long competitors have been paying for them, and get a ranked
shortlist you can act on.

**Ad longevity is the core signal.** An ad that has been running for 400 days is
almost certainly profitable for the advertiser. An ad that died in 3 days is a
failed test. That single fact is more reliable than any creative opinion.

```
scrape → normalize → SQLite → score → shortlist → profit gate
```

> **Not a spend bot.** The shortlist is a signal, not a verdict. Every `picks`
> run prints `FB scores must not directly trigger a spend decision` — run a profit
> model before you put money behind anything.

---

## What it actually produces

```bash
$ python3 picks_cli.py picks --min-score 60 --limit 8
score orbit  ads age_d  keyword              brand            domain              pain
  108   100    8   633  <keyword-a>          <brand-x>        <brand-x>.com       <pain>
  108    91   11   266  <keyword-b>          <brand-y>        <brand-y>.com       <pain>
  108    85    8   210  <keyword-c>          <brand-z>        <brand-z>.com       <pain>
  106    86    5   298  <keyword-d>          <brand-w>        <brand-w>.com       <pain>

! 下一闸门：对候选运行 profit-screen；FB 分数本身不得直接触发花钱测试。
```

*(brand and keyword values are redacted; `score` / `orbit` / `ads` / `age_d` are live output shape)*

And a per-keyword competitive map:

```bash
$ python3 picks_cli.py keyword "<your-keyword>"
# <your-keyword> · scan <date> (Nd) [密度截断·比率才可信]
ads=30 brands=10 · 独立站 10 家 · 平台 0 家 · 最老独立站广告 545d
  DTC  <brand-a>   <brand-a>.com   ads=3  age=545d  爆品
  DTC  <brand-b>   <brand-b>.com   ads=1  age=344d  爆品
  DTC  <brand-c>   <brand-c>.com   ads=5  age=234d  爆品
```

Note it separates **DTC sites** from **marketplaces** (Amazon/Temu ads mean
nothing about a DTC opportunity) and labels `爆品` only when an *independent-store*
ad has survived a long time.

---

## Install

```bash
git clone <this-repo> fbpipe && cd fbpipe
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Requires Python 3.11+. `sqlite3` is stdlib.

Browser engines are optional and interchangeable — install whichever you plan to use:

| Engine | Install | Env |
|---|---|---|
| Patchright (default) | `pip install patchright` | `FB_ENGINE=playwright` |
| Camoufox | `pip install 'camoufox[geoip]'` | `FB_CAMOUFOX=1` |
| Scrapling | `pip install scrapling` | `FB_ENGINE=scrapling` |
| Firecrawl (hosted) | `pip install firecrawl-py` | `FB_FIRECRAWL=1` + `FIRECRAWL_API_KEY` |

## First run

```bash
# 1. create the schema
python3 fb-scan-pipeline-v8.py --init-db

# 2. see what would be scanned
python3 fb-scan-pipeline-v8.py --list-queue

# 3. scrape a few keywords (start small)
python3 fb-scan-pipeline-v8.py --v3-scrape "neck massager,posture corrector" \
  --v3-country US --v3-max-scrolls 5

# 4. process raw → normalized → scored
python3 fb-scan-pipeline-v8.py --process-raw
python3 fb-scan-pipeline-v8.py --update-excel

# 5. read the shortlist
python3 picks_cli.py picks --min-score 60
python3 picks_cli.py doctor      # health check
python3 picks_cli.py selftest    # 9 offline assertions
```

## CLI reference

### `fb-scan-pipeline-v8.py` — the only entry point

| Flag | Does |
|---|---|
| `--init-db` | Create schema |
| `--v3-scrape "kw1,kw2"` | Scrape keywords (v3 engine, recommended) |
| `--v3-auto-scrape "seed1,seed2"` | Expand seeds → scrape derived keywords |
| `--process-raw` | Batch-process raw JSON → normalized rows |
| `--update-excel` | Rebuild Excel export |
| `--update-intel` | Refresh keyword intelligence |
| `--rabbit-hole` | Walk from a keyword to adjacent ones |
| `--serve --port 8000` | FastAPI dashboard |
| `--daemon --scrape-limit 24` | Long-running scheduled scraper |
| `--login` | Open a visible browser to establish a session |
| `--list-queue` | Show pending keywords |
| `--show-orbits` | Brand co-occurrence clusters |

*(the file is named `v8` for historical reasons; internally it loads `pipeline/` v9 modules)*

### `picks_cli.py` — the offline decision layer

| Subcommand | Does |
|---|---|
| `picks` | Ranked shortlist (`--min-score --days --limit --only-shopify --light-only --json`) |
| `keyword` | Full competitive picture for one keyword |
| `brand` | Brand dossier |
| `profit-screen` | Unit-economics pre-screen (needs price + COGS) |
| `export-r8` | Emit evidence slots for a downstream deep-verify |
| `doctor` | Environment + data health |
| `selftest` | Offline assertions, no network |

---

## How scoring works

Scores are **0-120** and combine, roughly:

- **Ad longevity** — days the advertiser has kept paying (dominant weight)
- **Orbit score** — how many brands cluster around this keyword; a crowded orbit
  means the demand is real, a lone advertiser means unproven
- **Signal richness** — price/discount/review count/urgency/guarantee/authority
  extracted from the landing page
- **FB fitness** — how well the product suits a paid-social demo (visual payoff,
  impulse price, low policy risk)
- **Penalties** — marketplace ads, brand self-promotion, heavy/bulky goods

`--light-only` exists because heavy items (pillows, furniture, equipment) look
great on paper and die on shipping cost. For a fast test, filter them out first.

## Environment variables

Nothing is required; everything degrades gracefully.

```bash
# politeness / throughput
FB_DELAY_MIN_SEC=60          # delay between keyword scrapes
FB_DELAY_MAX_SEC=180
FB_DAILY_QUOTA=24            # hard cap per day
FB_CYCLE_MINUTES=360         # daemon cycle
FB_BLOCK_RESOURCES=1         # skip images/fonts/css (5-10x faster)

# fetch engine
FB_ENGINE=playwright|scrapling|auto
FB_CAMOUFOX=1
FB_FIRECRAWL=1
FB_PROXY=http://127.0.0.1:8080
FB_PROXY_LIST=http://a:1,http://b:2

# session
FB_ANONYMOUS=1               # default: no login

# optional enrichment (absent = that collector stays quiet)
ANTHROPIC_API_KEY=
PERPLEXITY_API_KEY=
SERPAPI_KEY=
FIRECRAWL_API_KEY=
```

## Architecture

```
fb-scan-pipeline-v8.py        CLI entry, arg parsing, orchestration
core/logger.py                structured logging

pipeline/
  scraper.py                  Playwright human-emulation, circuit breaker, quota
  scraper_v9.py               concurrent multi-context fast path
  scrapling_scraper.py        alternate engine
  camoufox_scraper.py         anti-fingerprint engine
  firecrawl_scraper.py        hosted engine
  graphql_sniffer.py          capture the internal GraphQL responses
  graphql_replay.py           replay them for backfill
  commerce_filter.py          DTC vs marketplace classification  ← key
  fb_fitness.py               paid-social suitability scoring     ← key
  scoring.py                  brand score
  recommendations.py          0-120 ranked table
  veteran_signals.py          "20-year buyer" seven-question review
  v3_selection_core.py        selection engine
  keyword_evolution.py        keyword rabbit-holing
  persistence.py              SQLite WAL + monthly shard views
  server.py                   FastAPI + inline dashboard
  ...

picks_cli.py                  offline read-only decision CLI
```

## Data model (SQLite)

| Table | Rows in a working install | Holds |
|---|---|---|
| `ad_archive` | ~11k | every ad seen: page, domain, dates, copy |
| `ad_fts` | ~11k | FTS5 index over ad copy — search hooks directly |
| `brands` | ~68k | brand dossiers, orbit scores |
| `recommendations` | ~3.3k | scored shortlist, 31 columns |
| `scans` | ~475 | per-keyword scan history |
| `learned_keywords` | ~4.7k | auto-derived keyword graph |

`ad_fts` is the underrated one — full-text search over every competitor's ad
copy tells you which hooks a niche actually uses.

## Scheduling

`launchd/` has ready plists (macOS). They assume the repo lives at `$HOME/.claude/skills/NEW`
— edit the paths before loading:

```bash
cp launchd/*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.doi.fbpipeline.daemon.plist
```

Use cron/systemd elsewhere. Keep the daily quota conservative.

---

## Known issues

**`pipeline/signals/` is shadowed by `pipeline/signals.py`.** Python resolves the
module, not the package, so the optional signal collectors in `pipeline/signals/`
cannot be imported. Nothing in the main path depends on them — scraping, scoring,
`picks`, and the dashboard all work. Untangling it is a rename (`signals.py` →
`signals_core.py`), which is left to a future change so the upstream layout stays
recognizable.

**`picks_cli.py profit-screen` needs an external profit model.** It looks for
`GSHOPPING_SCRIPTS` (default: `../gshopping-rising/scripts`). Without it, `picks`
and `keyword` still work; only `profit-screen` / `export-r8` report unavailable.
Point `GSHOPPING_SCRIPTS` at your own unit-economics model to enable them.

## Data this repo deliberately does not ship

The interesting part of a working install is the **dataset**, not the code:

- the ad archive, brand dossiers, and scored shortlist (your research)
- the browser profile (a live session — never publish it)
- the keyword library and competitor domain lists
- Amazon ABA exports (third-party data)

See [SECURITY.md](SECURITY.md) for the full list and the reasoning.

## Legal

Facebook Ad Library is a public transparency surface, but **you** remain
responsible for complying with Meta's Terms of Service and local law. The scraper
ships with deliberately slow defaults; if you lower them you take on the risk.
Do not use this to profile individuals or reach non-public surfaces.

[MIT](LICENSE)

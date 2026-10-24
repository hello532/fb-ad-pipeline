# Security & Responsible Use

## What this project does

It automates reading **public** ads from Facebook Ad Library for product research.
It is a data-collection and scoring tool, not an advertising or bypass tool.

## What is NOT in this repository, by design

| Excluded | Why |
|---|---|
| `pipeline.db` (147 MB) | Contains your scanned ads, brands, scores — your proprietary dataset |
| `.browser_profile/` (~1 GB) | A real Chromium profile. Contains device cookies and cache |
| `d2c_domains.txt` (4,563 domains) | Your competitor list — the actual value of a working pipeline |
| `kw_scanner.db`, Amazon ABA rows | Licensed/scraped third-party data |
| `logs/`, `shards/`, `archives/`, `exports/` | Operational history |
| `tabapi.local.md`, `secrets/` | Credential locations |

**No credentials are hardcoded anywhere.** Every key is read from the environment:

```
ANTHROPIC_API_KEY   optional — AI intent extraction
PERPLEXITY_API_KEY  optional — AI search grounding
SERPAPI_KEY         optional — keyword intent
FIRECRAWL_API_KEY   optional — alternate fetch engine
FB_PROXY            optional — HTTP proxy, defaults to none
```

## Legal and platform-policy position

Facebook Ad Library is a **public transparency surface**. Reading it is different from
scraping a private feed. Still:

1. **You are responsible** for compliance with Meta's Terms of Service in your jurisdiction.
2. **Rate-limit yourself.** The scraper ships with conservative defaults
   (`FB_DELAY_MIN_SEC` / `FB_DELAY_MAX_SEC`, daily quota, quiet hours, circuit breaker).
   Lowering them shifts risk onto you.
3. **Do not use it to** build profiles of individuals, harvest personal data, or
   circumvent access controls on non-public surfaces.
4. **Anonymous mode is the default** (`FB_ANONYMOUS=1`). Running it against a logged-in
   personal account is your choice and your risk.

## Data you collect is your responsibility

The scoring output is a **signal**, not a fact. `picks_cli.py` prints this on every run:

> FB scores must not directly trigger a spend decision. Run `profit-screen` first.

Respect it. A high ad-longevity score means "someone is spending money on this",
not "this product is profitable for you".

## Reporting a vulnerability

Open a private security advisory on GitHub. Do not file a public issue for anything
involving credentials, session handling, or proxy configuration.

## If you fork this and add features

The failure modes that matter:

- **Never commit `pipeline.db`.** It is gitignored; if you rename it, ignore the new name.
- **Never commit `.browser_profile/`.** After debugging with `--login`, that directory
  contains a live session.
- **Credentials belong in env vars**, never in a settings file that gets committed.

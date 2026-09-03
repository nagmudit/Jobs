# wellfound-probe

A **feasibility probe**, not a product. It answers one question empirically: *which
path for getting structured job data out of Wellfound actually works, what does it
cost, and how fragile is it?*

The findings and the recommendation live in **[REPORT.md](REPORT.md)**. This file
is just how to run things.

## Install

```bash
pip install httpx beautifulsoup4 lxml
pip install playwright && playwright install chromium   # only needed for P4
```

## Run

```bash
python -m src.run --probe p1     # static HTTP + embedded JSON
python -m src.run --probe p2     # JSON-LD + robots/sitemap surface
python -m src.run --probe p3     # RSS/Atom/JSON feeds
python -m src.run --probe p4     # real browser, persistent profile
python -m src.run --probe p6     # ATS resolution + hit rate
python -m src.run --probe all
```

Each probe writes `results/<probe>.json`. `--probe all` runs them in dependency
order, because **P2 and P6 consume `results/p1_static.json`** (P2 needs job URLs,
P6 needs company names). Run `p1` first if you run them individually.

### Useful flags

| Flag | Meaning |
|---|---|
| `--role` / `--location` | Wellfound slugs. Defaults: `artificial-intelligence-engineer` / `bangalore` |
| `--pages N` | Pages of results (probe is capped at 2 by default) |
| `--refresh` | Bypass the cache and re-fetch |
| `--limit N` | P6 only: cap how many companies to resolve |
| `--headed` | P4 only: run the browser visibly |
| `--login` | P4 only: one-time manual login (see below) |
| `--no-strict-role` | P1 only: don't abort on silent role fallback (see below) |

## The role slug is the biggest footgun

`/role/l/{role}/{location}` **does not 404 on an invalid role slug.** It quietly
serves an unfiltered, location-wide search. You get HTTP 200 and hundreds of
plausible-looking jobs that were never filtered by role at all.

```
artificial-intelligence          -> role IGNORED, 1150 jobs (all Bangalore jobs)
artificial-intelligence-engineer -> role APPLIED,  596 jobs   <-- correct
```

P1 detects this by reading the GraphQL cache key that Wellfound embeds in the page
(`seoLandingPageJobSearchResults({"location":"bangalore","page":1,"role":"..."})`)
— the server's own record of what it actually filtered on — and raises
`SilentRoleFallback` rather than returning wrong data. To find a valid slug, try
candidates and check whether `role_applied` is true in `results/p1_static.json`.

## P4 login (one-time, manual)

```bash
python -m src.run --probe p4 --login
```

Opens a headed Chromium against the persistent profile in `browser-profile/`. Log
in by hand, close the window; later runs reuse that profile. Credentials are never
automated and no challenge widget is ever touched.

## Caching

Every response — including errors — is cached under `cache/<host>/<sha256>.{meta.json,body}`.
Re-runs hit the cache, so you can iterate on parsing without re-fetching. A cached
403 is deliberate: it's evidence, and re-requesting it is both slow and rude.

## Conduct

- `robots.txt` is enforced in code ([src/robots.py](src/robots.py)); a disallowed
  URL raises rather than being fetched. `/search` and `?role=` params are
  disallowed by Wellfound and are never touched.
- 1 request per 3–5s to Wellfound, concurrency 1, honest `User-Agent` with a
  contact address (override with `PROBE_CONTACT`).
- Public ATS APIs (Greenhouse/Lever/Ashby/Workable) use a shorter 0.4–0.9s delay:
  they're documented, keyless, machine-facing endpoints built for polling. Still
  strictly serial, still cached.
- **No anti-bot evasion** — no CAPTCHA solvers, no residential proxies, no TLS/JA3
  spoofing, no stealth plugins. Any path that needs those is recorded as
  `BLOCKED — not pursued`.

## Layout

```
src/models.py            Job dataclass (12 fields) + coverage scoring
src/cache.py             URL-keyed cache + throttled fetcher
src/robots.py            robots.txt enforcement
src/probes/p1_static.py  P1  static HTTP + __NEXT_DATA__
src/probes/p2_structured.py  P2  JSON-LD + sitemaps
src/probes/p3_feeds.py   P3  feed discovery
src/probes/p4_browser.py P4  Playwright + persistent profile
src/probes/p6_ats.py     P6  resolve_ats() + hit rate
src/run.py               orchestrator
results/                 raw JSON per probe
```

P5 (licensed third parties) is desk research only — no code. See REPORT.md.

# jobsearch

A local tool that crawls Wellfound job listings for whatever roles and locations you
specify, stores everything in SQLite, and serves a small web UI where you filter and
sort the corpus yourself and click through to apply.

It does not score, rank, or recommend anything. **You filter; it fetches and displays.**

## Quickstart

```bash
pip install httpx beautifulsoup4 lxml pyyaml fastapi uvicorn
# edit targets.yaml -- roles and locations live there, not in the code
python -m src.cli crawl
python -m src.cli serve          # http://localhost:8000
```

## Commands

```bash
python -m src.cli roles --location bangalore     # discover + validate role slugs
python -m src.cli crawl                          # uses targets.yaml
python -m src.cli crawl --roles ai-engineer --locations remote
python -m src.cli enrich --ids 123,456           # detail fetch (the UI calls this)
python -m src.cli stats                          # corpus, fill rates, slice truncation
python -m src.cli serve --port 8000
```

`--roles` / `--locations` override `targets.yaml` for a single run. Nothing about
roles or locations is hardcoded anywhere in `src/`.

## targets.yaml

```yaml
roles:     [ai-engineer, machine-learning-engineer, data-engineer]
locations: [bangalore, remote, india]
max_pages_per_slice: 20
delay_range: [3.0, 5.0]
user_agent: "jobsearch-personal/0.1 (+you@example.com)"
```

`location: remote` is special — it maps to `/role/r/{role}`, a different page type
whose query carries `remote:true` instead of a location. Everything else maps to
`/role/l/{role}/{location}`.

## Finding role slugs

There is no published list, and **an invalid slug does not 404** — it silently serves
an unfiltered location-wide search. `python -m src.cli roles` discovers candidates by
slugifying `primaryRoleTitle` off job nodes already in your DB (Wellfound's own
canonical role names) and validates each one, printing a block you can paste into
`targets.yaml`.

Crawl once with a known-good slug first so there is something to harvest from.

Footer link blocks are deliberately **not** used as a discovery channel: Wellfound
serves an identical generic 14-slug list on every role page, so crawling them is a
dead end.

## The two tiers

**Breadth — `crawl`.** Search pages only, ~1 request per 20 companies. A nine-slice
run is roughly 20 minutes. This is what fills the corpus.

**Depth — `enrich`.** Job detail pages, one request each, for equity and better
salary. **Never run over the whole corpus** — 10,000 jobs is 11+ hours. Filter in the
UI down to a few dozen rows and hit **Enrich filtered results**; it fetches exactly
those. The button warns with a projected wall time before starting anything over 100
rows.

Equity exists *only* on detail pages — not in the search payload, not in any ATS.

## Slicing defeats the page cap

Each `(role, location)` slice is capped by Wellfound at roughly 15–20 pages regardless
of what `totalJobCount` claims. A slice claiming 6,624 jobs still returns a few
hundred.

So slices are **not** how you narrow the search — they're how you get more out of it.
Overlapping slices each get their own budget and dedupe sorts it out. `bangalore` is a
subset of `india`, but under a per-slice cap it recovers jobs the `india` slice
truncates away. Adding `hyderabad`, `pune`, `mumbai`, `delhi` works the same way.

The UI's **slice truncation** panel shows `jobs_recovered / total_claimed` per slice.
A ratio near 1.0 means that slice is complete; a low one means it needs
sub-partitioning. Observed on a real run:

| slice | recovered/claimed | ratio | ended |
|---|---|---|---|
| machine-learning-engineer @ bangalore | 32/32 | 1.00 | page_wrap |
| data-engineer @ bangalore | 206/230 | 0.90 | page_wrap |
| ai-engineer @ remote | 693/1954 | 0.36 | max_pages |

## Storage

Raw first. The Apollo node is stored verbatim in `job_raw.raw_json`; every filterable
column is derived in the `jobs` view via `json_extract` plus the functions in
`src/derive.py`. Improving a salary regex re-derives every row on the next query — no
migration, no re-crawl.

`user_state` is keyed separately from `job_raw` so shortlist/applied/hidden survive
re-crawls that rewrite job rows.

Salary parses `$25k – $50k`, `₹15L – ₹25L`, `₹1.2Cr`. Anything unrecognised yields
`NULL`, never a guess, and `salary_raw` is always kept alongside.

## Conduct

- `robots.txt` parsed and enforced in `src/fetch.py`; a disallowed URL raises.
  `/_jobs/` is disallowed. `/company/` was re-verified as allowed.
- 3–5 s between requests, concurrency 1, honest User-Agent with a contact address.
  A `delay_range` floor below 3.0 s is rejected at config load.
- **No anti-bot evasion** — no proxies, no CAPTCHA services, no stealth plugins, no
  fingerprint spoofing.
- `cf-ray` and `cf-mitigated` are logged for every response in `request_log`. On the
  first non-`None` mitigation or first 403/429 the crawl **halts**, commits, and
  reports. It never retries through it.
- Responses cached to `cache/` by URL hash; re-runs hit cache. Everything is resumable
  — `crawl_unit` tracks completed pages, so an interrupted run resumes at the page it
  stopped on.

## The four assertions

All of these return HTTP 200 with plausible data, so they raise rather than warn:

| Assertion | What it catches |
|---|---|
| `SilentRoleFallback` | Invalid role slug → unfiltered location-wide results |
| `PageWrap` | Server re-serving page 1 past the end, byte-identical, forever |
| `YieldFloor` | A page returning 0 jobs after full pages → broken parse |
| `SchemaDrift` | `__NEXT_DATA__` gone, `self.__next_f.push` present → App Router |

`PageWrap` is caught by the crawl loop and closes the slice cleanly as
`ended_reason='page_wrap'` — it's the normal end of a large slice.

## Tests

```bash
python -m pytest tests -q      # 38 tests, no network
```

Assertions run against synthetic `__NEXT_DATA__` fixtures in `tests/fixtures.py`
reproducing each failure shape.

## Layout

```
targets.yaml          roles + locations + conduct settings
jobs.db               SQLite corpus
src/config.py         config loading, CLI overrides
src/fetch.py          httpx + robots + rate limit + cache + cf logging
src/parse.py          __NEXT_DATA__ / Apollo extraction
src/assertions.py     the four silent-failure guards
src/store.py          schema + the jobs view
src/derive.py         salary/equity/size parsing, registered as SQL functions
src/crawl.py          slice loop, telemetry, resume
src/enrich.py         detail pages, JSON-LD + rendered comp
src/cli.py            entry point
src/web/app.py        FastAPI, SQL-side filtering
src/web/static/       one HTML page, vanilla JS, no build step
```

## Known gaps

- `src/ats.py` (company → Greenhouse/Lever/Ashby/Workable board) is not built. The
  `has ATS board` filter uses Wellfound's own `atsSource` field, which is populated on
  roughly a third of jobs and needs no extra requests.
- Detail-page enrichment is the only source of equity, so `has equity` matches nothing
  until you enrich.

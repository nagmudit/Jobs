---
status: current
last_verified: 2026-09-03
applies_to: [jobsearch, wellfound-probe]
---

# Commands

Every command below was run on 2026-09-03 unless marked otherwise. Run from
`jobsearch/` unless the table says otherwise.

## jobsearch

| Task | Command | Verified |
|---|---|---|
| Install | `pip install httpx beautifulsoup4 lxml pyyaml fastapi uvicorn pytest` | ✅ all import |
| Test | `python -m pytest tests -q` | ✅ ran, 133 pass (2026-09-04) |
| Crawl (targets.yaml) | `python -m src.cli crawl` | ✅ ran, 9 slices, 1836 jobs |
| Crawl (override) | `python -m src.cli crawl --roles ai-engineer --locations remote` | ✅ ran |
| Crawl (force re-ingest) | `python -m src.cli crawl --no-resume` | ✅ ran |
| Discover role slugs | `python -m src.cli roles --location bangalore` | ⚠️ not run — makes live requests |
| Enrich specific jobs | `python -m src.cli enrich --ids 123,456` | ✅ ran, 3 enriched |
| Enrich next N unenriched | `python -m src.cli enrich --limit 25` | ⚠️ not run |
| Stats | `python -m src.cli stats` | ✅ ran |
| **Fetch a role from every source** | `python -m src.cli fetch --roles artificial-intelligence-engineer` | ✅ ran, 3 sources |
| Fetch several roles | `python -m src.cli fetch --roles a,b --sources remoteok` | ✅ ran |
| Expand ATS boards for a role | `python -m src.cli fetch --roles <role> --sources greenhouse` | ✅ ran, 47/55 companies |
| Expand Ashby for a role | `python -m src.cli fetch --roles <role> --sources ashby` | ✅ ran, 73/88 companies |
| Ingest all API sources (no role) | `python -m src.cli ingest` | ✅ ran |
| Ingest one source | `python -m src.cli ingest --sources himalayas` | ✅ ran, 500 kept / 478 new |
| Ingest vickybytes | `python -m src.cli ingest --sources vickybytes` | ✅ ran 2026-09-08, 316 claimed / 76 kept / 236 stale |
| Prune stale (dry run) | `python -m src.cli prune` | ✅ ran, reported 1747/2941 |
| Prune stale (delete) | `python -m src.cli prune --apply` | ✅ ran on a copy, removed 1883/6266, kept marked |
| Widen the age cutoff | `python -m src.cli crawl --no-resume --max-age-days 90` | ⚠️ not run |
| Serve UI | `python -m src.cli serve` | ✅ ran on :8013, UI + all API routes |
| Serve UI read-only (as hosted) | `JOBSEARCH_READONLY=1 python -m src.cli serve` | ✅ ran, 4 crawl routes 404 |
| Adopt a downloaded corpus (dry run) | `python -m src.cli sync /tmp/corpus.db` | ✅ ran |
| Adopt a downloaded corpus | `python -m src.cli sync /tmp/corpus.db --apply` | ✅ ran on a copy, 2 marks carried |
| Publishable copy, no user marks | `python -m src.cli export --out corpus.db` | ✅ ran, 6266 jobs / 0 marks |

There is **no lint, formatter, typecheck, or build step** configured. Don't document
or claim one.

`--roles` / `--locations` / `--max-age-days` work both before and after the subcommand.

**Age cutoff.** `max_age_days` in `targets.yaml` (default 30) drops older jobs at
ingest, skips them during enrichment, and seeds the UI filter. It saves **no requests**
— Wellfound does not order results by date, so every page is still fetched (ADR-006).
Widening it later needs no network: the raw pages stay in `cache/`, so
`crawl --no-resume --max-age-days 90` re-ingests from disk.

## Backing up the database

WAL mode means the newest rows may live in `jobs.db-wal`, not `jobs.db`. Copying the
main file alone can produce a backup that reads as empty. Checkpoint first:

```bash
python -c "import sqlite3;sqlite3.connect('jobs.db').execute('PRAGMA wal_checkpoint(TRUNCATE)')"
cp jobs.db jobs.db.bak
```

## Repo hygiene

Run from the repository root.

| Task | Command | Verified |
|---|---|---|
| Validate context docs | `python quality/validate_context.py` | ✅ ran, clean |
| Validate test manifest | `python quality/validate_manifest.py` | ✅ ran, clean |

Run both before claiming a doc is current or a test gap is closed. Neither needs
network or a database.

## wellfound-probe (frozen)

Run from `wellfound-probe/`.

| Task | Command | Verified |
|---|---|---|
| One probe | `python -m src.run --probe p3` | ✅ ran, cache hit |
| All probes | `python -m src.run --probe all` | ✅ ran previously (2026-09-02) |
| Comparison matrix | `python -m src.matrix` | ✅ ran previously |
| P4 login (manual) | `python -m src.run --probe p4 --login` | ⚠️ never run — needs a human at a browser |

Probes replay from `wellfound-probe/cache/`; re-runs are offline unless `--refresh`.

## Prerequisites

- Python 3.11+ (developed and verified on 3.14.2).
- No services, no containers, no env vars required.
- `wellfound-probe` P4 additionally needs `playwright` + `playwright install chromium`.

## Timing — these are slow on purpose

Crawling is rate-limited to one request per 3–5 s with concurrency 1, and that floor is
enforced in `config.py`. Budget accordingly:

| Operation | Cost |
|---|---|
| One search page | ~4 s |
| One slice (20 pages) | ~1.5 min |
| Full 9-slice crawl | ~15–20 min |
| Enrichment | ~4 s **per job** — never run corpus-wide |

Run long crawls as a background task. Do not shorten the delay to fit a timeout.

## Gotchas

- **`python -m src.cli` from the repo root does nothing useful.** Both projects have a
  `src/` package; `cd` into the right one first.
- **`sqlite3 jobs.db "SELECT * FROM jobs"` fails** with `no such function: size_label`.
  The view calls Python functions registered in `store.connect()`. Query through
  `store.connect()`, or query the `job_raw` / `company_raw` tables directly.
- A second `serve` on the same port silently fails in the background; check the log.
- `crawl` resumes by default. To re-ingest pages already fetched, pass `--no-resume` —
  it still replays from the HTTP cache, so it costs no requests.
- The first `crawl` on a clean checkout creates `jobs.db`; it is gitignored.

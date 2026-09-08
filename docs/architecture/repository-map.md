---
status: current
last_verified: 2026-09-03
applies_to: [jobsearch/src, wellfound-probe/src]
---

# Repository Map

## Shape

Two independent Python projects side by side, no shared package, no monorepo tooling.
They are separated by *lifecycle*, not by layer:

- **`wellfound-probe/`** — a finished feasibility study. It answered "can we read
  Wellfound, and how" and produced `REPORT.md`. It is evidence, kept because
  re-deriving it costs a day of live requests. Do not extend it.
- **`jobsearch/`** — the tool built on that answer. All active work happens here.

`jobsearch/` is a layered pipeline with one hard rule: **every byte from the network
enters through `src/fetch.py`, and every byte stored stays raw until the SQL view
derives it.**

```
targets.yaml → config → crawl ─┬→ fetch ──→ (network, rate-limited, robots-gated)
                               └→ parse ──→ store (raw JSON)
                                              ↓
                                      jobs VIEW ← derive.py (SQL functions)
                                              ↓
                                     web/app.py → static/index.html
```

## Directories

### `jobsearch/src/`

**Holds:** the whole tool. Flat module layout — 9 modules, no sub-packages except `web`.
**Entry:** `python -m src.cli`

| Module | Holds | Depends on |
|---|---|---|
| `config.py` | `targets.yaml` loading, CLI overrides, the delay-floor guard | nothing internal |
| `fetch.py` | **The only network egress.** robots, rate limit, disk cache, cf logging | `assertions` |
| `parse.py` | `__NEXT_DATA__` → Apollo → raw nodes. No normalisation | `assertions` |
| `assertions.py` | The four corpus-integrity guards | nothing internal |
| `store.py` | Schema, writers, the `jobs` view, `rebuild_locations`, `migrate`, `prune_stale`. `connect()` is concurrency-safe and rebuilds views only when stale | `derive` |
| `derive.py` | salary/equity/size parsing, registered as SQLite functions | nothing internal |
| `crawl.py` | Wellfound slice loop, telemetry, resume | `fetch`, `parse`, `store`, `assertions` |
| `roles.py` | `fetch_role` — one role across every source, with `filter_mode` | `crawl`, `sources`, `store` |
| `relevance.py` | Local role matching (title + categories). No internal imports | — |
| `ats.py` | Company -> ATS board token resolution, cached in `company_ats` | `store` |
| `sources/greenhouse.py` | Company-scoped board expansion + core view | `assertions`, `store` |
| `sources/ashby.py` | Hosted-board index + matched posting details + core view | `assertions`, `store` |
| `sources/__init__.py` | `CORE_COLUMNS` contract, registry, drift check | — |
| `sources/wellfound.py` | Core view only (ingest lives in `crawl.py`) | — |
| `sources/remoteok.py` | `/api` ingest + core view | `assertions`, `store` |
| `sources/himalayas.py` | `/jobs/api` offset paging + core view | `assertions`, `store` |
| `sources/vickybytes.py` | One `/api/opportunities` call + core view; runs only under a declared robots grant (ADR-013) | `assertions`, `store`, `relevance` |
| `enrich.py` | Detail pages: JSON-LD + rendered comp | `fetch`, `store` |
| `cli.py` | Argument parsing and command dispatch | everything |
| `web/app.py` | FastAPI; all filtering in SQL | `store`, `enrich`, `fetch` |
| `web/static/index.html` | The entire UI. Vanilla JS, no build step | — |

**Rules:**
- Nothing but `fetch.py` opens an HTTP connection. Unenforced by tooling — this is
  convention only, and a real risk (see `docs/plans/active/`).
- `derive.py` must import nothing internal; it is loaded during `store.connect()`
  before the schema exists.
- `parse.py` never normalises. It returns raw dicts; shaping happens in SQL.

### `jobsearch/tests/`

Offline only. `fixtures.py` builds synthetic `__NEXT_DATA__` pages reproducing each
failure shape, so the assertions can be tested without touching Wellfound.

### `wellfound-probe/`

Six probe modules (P1–P6) behind `python -m src.run --probe <id>`, plus `REPORT.md`.
`results/*.json` holds the raw measurements behind every number in the report.

## Where things live

| Looking for | Go to |
|---|---|
| Roles/locations being crawled | `jobsearch/targets.yaml` |
| Rate limiting, robots, cache | `jobsearch/src/fetch.py` |
| Why a crawl stopped | `slice_stats.ended_reason`, or `src/crawl.py` |
| A new filterable column | `store.py` `VIEWS` + maybe `derive.py`. **Not** a re-crawl |
| Real job locations (Pune, SF) | `job_location` table, rebuilt by `store.rebuild_locations` |
| Which URL shape a slice uses | `fetch.search_url` — `anywhere` / `remote` / a place |
| Facet options and cascading | `web/app.py::facets`, built on `_clauses` per dimension |
| Filter SQL | `web/app.py::_where` |
| UI markup, JS, styling | `web/static/index.html` (single file) |
| Why we read Wellfound this way | `wellfound-probe/REPORT.md`, ADR-001 |
| Silent-failure evidence | `wellfound-probe/REPORT.md` §5 |

## Boundaries

| Rule | Enforced? |
|---|---|
| All egress via `Fetcher.get` | **Convention only** — no test, no lint |
| `delay_range` floor ≥ 3.0 s | Enforced in `config.py`, raises at load |
| robots.txt honoured, per origin | Enforced in `fetch.py`, raises `RobotsDisallowed` |
| Unreadable robots.txt blocks the host | Enforced in `fetch.py`, raises `RuntimeError` |
| Halt on mitigation | Enforced in `assertions.check_mitigation` |
| No role/location literals in `src/` | **Convention only** |

The two "convention only" rows are the highest-value automation targets in the repo.

## Surprises

Things that will mislead you if nobody says them:

- **`wellfound-probe/` looks like live code and is not.** It has its own `src/`,
  `cache/`, and CLI. It is frozen. `jobsearch/` does not import from it.
- **The `jobs` view calls Python.** `salary_min`, `salary_max`, `equity_max`,
  `size_label`, `size_min`, `remote_label` are Python functions registered on the
  connection in `store.connect()`. Open the DB with plain `sqlite3` on the command
  line and **every query against `jobs` fails with "no such function"**. Use
  `store.connect()`.
- **`location: remote` is not a location.** It maps to `/role/r/{role}`, a different
  page type whose GraphQL args carry `remote: true` instead of a location member.
  Handled in `fetch.search_url`.
- **Slicing widens results, it does not narrow them.** Overlapping slices are
  deliberate; each gets its own page budget. See ADR-004.
- **`job_provenance.location` is not a place.** It is the slice token we crawled
  (`anywhere`), not where the job is. Places live in `job_location`. Presenting
  provenance as location was a real shipped bug; see ADR-005.
- **ATS boards are not search sources.** Greenhouse and Ashby are company-scoped:
  `fetch_role` expands companies the role already touched. A `filter_mode` of `expand`
  means exactly that. Ashby uses its robots-allowed hosted pages because the documented
  API origin's robots.txt returns 401; see ADR-010.
- **"Fetched for role X" does not mean the source filtered by role.** Only Wellfound
  does. Check `filter_mode` — RemoteOK and Himalayas are filtered locally by
  `src/relevance.py`. See ADR-009.
- **The age cutoff is an ingest filter, not a stop condition.** Search results are
  not date-ordered, so paging must continue past all-stale pages. See ADR-006.
- **`CREATE TABLE IF NOT EXISTS` does not add columns.** New columns go in
  `store.MIGRATIONS` (additive only) or an existing database breaks on first query.
- **`jobs` is three layers deep**: `job_raw` → `jobs_core` (UNION ALL per source) →
  `jobs`. A missing column error usually means a source view drifted from
  `CORE_COLUMNS`, not that the outer view is wrong.
- **Views belong to the file, not the connection.** `store.connect()` therefore
  rebuilds them only when `sqlite_master` disagrees with the source registry, under
  `store._SCHEMA_LOCK`. It used to drop and recreate both on every call, which the
  web app — a connection per request across FastAPI's threadpool — turned into
  `view jobs_core already exists` on page load, and could also drop `jobs` out from
  under a connection querying it. Adding unconditional DDL back to `connect()`
  reintroduces both. See CJ-052.
- **SQLite orders every INTEGER before every TEXT.** `strftime('%s','now')` returns
  TEXT, so any `int_column < strftime(...)` is unconditionally true without a `CAST`.
  This shipped once as an always-expired flag.
- **Back up with `PRAGMA wal_checkpoint(TRUNCATE)` first.** WAL mode means `cp jobs.db`
  can copy an empty-looking database while the rows sit in `jobs.db-wal`.
- **Filter clauses carry a dimension name** so `/api/facets` can exclude one and let
  that dimension's siblings stay selectable. A clause without a dimension silently
  breaks cascading.
- **`page_wrap` is a normal, successful slice ending**, not an error, despite being
  raised as an exception. `crawl_slice` catches it and closes the slice cleanly.
- `wellfound-probe/` and `jobsearch/` both have a `src/` package. Running `python -m
  src.cli` from the wrong directory silently targets the wrong project.

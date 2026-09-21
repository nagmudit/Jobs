---
status: active
created: 2026-09-21
last_updated: 2026-09-21
areas: [jobsearch/src/crawl.py, jobsearch/src/store.py, jobsearch/src/roles.py, jobsearch/src/web/app.py, jobsearch/tests]
---

# Wellfound stopped being fetched, and every run said it was fine

## What happened

Wellfound was last fetched on **2026-09-06**. For fifteen days the daily run
reported success and sent Wellfound **zero requests**.

`crawl_slice` skips a page already marked `done` in `crawl_unit`, and that mark had
no expiry. The daily workflow restores the previous corpus before fetching, and the
corpus carries `crawl_unit`, so every page arrived pre-marked and was skipped before
any request, assertion or cache check ran.

**Why nobody noticed.** The run printed
`wellfound 212 kept / 212 scanned (0 new, 0 off-role, 0 too old)`. `jobs_seen` is
counted from `job_provenance` — rows already stored — which is deliberate for a
resumed slice (`crawl.py`, "Count from provenance rather than from this run's loop"),
and identical whether the pages were fetched or skipped. `pages_walked` counts
skipped pages too. Nothing in the output distinguished a working source from a frozen
one. This is the ADR-003 failure class, in the one place no assertion looks.

Evidence, from the corpus published 2026-09-20:

| Check | Value |
|---|---|
| `max(last_seen)`, Wellfound rows | `2026-09-06T15:02` |
| `max(last_seen)`, every other source | `2026-09-20T19:3x–19:4x` |
| `crawl_unit` rows (311, all `done`) | dated 2026-09-02 … 09-06 |
| Newest Wellfound posting | 2026-09-06 |

**It was days from becoming data loss.** `max_age_days` moved to 20 on 2026-09-19,
so every Wellfound job would have been pruned around **2026-09-26**, with no way for
a new one to arrive. Wellfound is the source ADR-001 exists for.

## The fix

**The page-done mark expires with `cache_ttl_hours`** — the same window, and the same
reason, as a cached listing (ADR-011). A run resumed minutes later still skips ahead;
a run a day later re-walks.

- `store.page_done(conn, slice_key, page, max_age=None)` — `done` **and** marked
  within `max_age`. Timestamps are parsed with `datetime.fromisoformat` and compared
  in Python, never as SQL string/integer comparisons (the repository map records a
  shipped bug from exactly that). An unparsable or missing timestamp counts as
  **stale**: re-fetching costs requests, skipping costs a frozen source.
  `max_age=None` keeps the permanent behaviour, which is what `cache_ttl_hours: 0`
  means under ADR-011.
- `crawl_slice` passes `fetcher.listing_ttl`.
- **`SliceResult.pages_fetched`** counts real requests. `pages_walked` keeps its
  meaning; only this tells the two runs apart.
- A slice that requested nothing now ends with **`resumed_fresh`**, not `exhausted`.
- `fetch_role` carries `fetched` through, and `describe()` prints
  `… [server] 20/20 pages fetched`. `CrawlJob` exposes it to the UI.

Unchanged: `resume=True` at all three call sites, `--no-resume`, pacing, robots, and
the per-host halt.

## Verification

- `tests/test_resume_freshness.py`, 9 tests, **written first and watched fail**
  (7 failed before the fix). It reproduces the incident: crawl, backdate the marks by
  a day as a restored corpus does, crawl again, and assert the pages are requested
  again and the rows refreshed.
- Five mutations, each red on the named test: mark never expires; unparsable
  timestamp treated as fresh; TTL not passed from `crawl_slice`; `pages_fetched` not
  counted; no-op run not reported.
- Full suite: **521 passed**.
- **Live, on a copy of the published corpus** (2026-09-21,
  `fetch --roles software-engineer --sources wellfound`):

  | | Before | After |
  |---|---|---|
  | Wellfound rows | 445 | **590** (+145) |
  | `max(last_seen)` | 2026-09-06 | 2026-09-21 |
  | Newest posting | 2026-09-06 | 2026-09-19 |

  145 jobs from **one** role that the daily run had been missing.

## Cost

Wellfound goes from ~0 requests a day to ~200 (10 roles × up to 20 pages), roughly
13–15 minutes at 3–5 s spacing. The 2026-09-20 run took 69 min against a 300 min
timeout.

## Remaining

- The **next daily run is the real proof**: `wellfound … 20/20 pages fetched` in the
  log, and Wellfound's `last_seen` matching the run. Check the run after this lands.
- The user's local corpus still carries the stale marks; the next local `fetch`
  clears them by re-walking. Nothing to do by hand.
- Other sources were never affected: ATS boards and the JSON APIs re-request every
  run, which is why only Wellfound froze. `company_ats` caches misses deliberately
  (ADR-010) and is a different mechanism.

---
status: active
created: 2026-09-18
last_updated: 2026-09-18
areas: [jobsearch/src/store.py, jobsearch/src/web/app.py, jobsearch/src/web/static/index.html, jobsearch/src/enrich.py, jobsearch/src/cli.py, jobsearch/tests]
---

# Make the UI fast: store the derived columns, stop shipping descriptions

## Objective

A filter click answers in well under 300 ms instead of 1.2–1.7 s, with the
`jobs` view returning the **same columns, in the same order, with the same values**
as it does today. No framework change: the browser is not where the time goes.

Done means the timings in *Acceptance* are met on the real corpus, the suite is
green, and every new guard has been shown to fail when broken.

## Current behavior

Measured 2026-09-18 on the real `jobs.db` (6,342 jobs, 60 MB) with FastAPI's
`TestClient`, warm:

| Endpoint | Time | Payload |
|---|---|---|
| `POST /api/jobs` (limit 100) | 530–1040 ms | 632 KB |
| `POST /api/facets` | ~700 ms | 29 KB |
| `GET /api/pending` | 186 ms | < 1 KB |
| meta / stats / crawl status | < 20 ms | |

Every filter change fires `/api/jobs` and `/api/facets` together. Rendering the
rows in the browser takes a few ms.

Where the time goes (best of 3, same database):

| Query | ms |
|---|---|
| `SELECT COUNT(*) FROM jobs_core` | 9.7 |
| `SELECT * FROM jobs_core` | 335 |
| `SELECT * FROM jobs` | 441 |
| `SELECT remote_label, COUNT(*) FROM jobs GROUP BY 1` (one facet) | 125 |
| the same facet on a stored copy of `jobs` | 20 |
| 100 rows sorted by `posted_ts` on a stored copy | 41 |
| building that stored copy from scratch | 853 |

So about three quarters of the cost is `json_extract` over `raw_json` in each
source's core view, and the Python UDFs (`src/derive.py`) add the rest. The view
does all of that again on every query, and one click runs about 11 of them
(3 in `/api/jobs`, 8 in `/api/facets`). `/api/jobs` also returns `description`
for every row, averaging 5 KB each, although the page only shows it after you
expand a row.

ADR-002 saw this coming: *"Derivation runs per row per query. Fine at this size;
would need materialising well before 10⁶ rows."* In practice the payloads are
large enough that it is already a problem at 6×10³ rows.

## Step 1 — store the derived columns in a table

### Shape

- A new table, **`job_derived`**, filled with
  `INSERT … SELECT` from today's derivation SQL (`jobs_core` joined with
  `job_detail` and provenance, plus the UDF columns and the USD sort keys).
- **`jobs` stays a view**, now a thin one: `job_derived` plus two things that
  cannot be stored:
  - **User state** (`status`, `note`), joined live from `user_state`. A shortlist
    click must show up immediately without a rebuild. This also keeps the marks out of
    the table, so `export`'s `clear_user_state` still removes everything personal and
    the published corpus cannot leak them through a stored copy.
  - **Values that depend on the current time** (`days_old`, `expired`), computed
    at query time from the stored `posted_ts`/`expires_ts`. They are cheap SQL, and
    storing them would make them go stale overnight.
- Every reader (`web/app.py`, `enrich.py`, `ats.py`, `store.py` helpers, `cli.py`,
  the hosted app) keeps querying `jobs` and needs no change.
- Indexes on `job_derived` for the columns filters and sorts touch: `posted_ts`,
  `source`, `remote_label`, `company_size`, `company`, `salary_usd_min`,
  `salary_usd_max`. Settle the final list by measuring with `EXPLAIN QUERY PLAN`,
  not by guessing.
- `raw_json` stays the source of truth. `job_derived` can always be rebuilt from
  it offline, with no network, which is what ADR-002 is protecting.

### Keeping it fresh (the risk ADR-002 rejected this for)

ADR-002 turned down a stored derived table because it *"needs a backfill job and
can drift from the raw data."* Two layers stop drift:

1. **Rebuild wherever the inputs are written.** `rebuild_locations` is already a
   stored derived table with 7 call sites (`cli.py` ×3, `crawl.py`, `roles.py`,
   `web/app.py` ×2). Replace it with one `rebuild_derived(conn)` that rebuilds both
   `job_location` and `job_derived`, so no call site can do one without the other.
   Add calls where `job_detail` or `job_provenance` change and no rebuild happens
   today: `enrich_ids`, `prune_stale(dry_run=False)`, `reconcile_roles(apply)`, and
   the `sync` / `carry_forward_marked` path. Check each in the code before relying on
   it.
2. **A fingerprint check in `connect()`** as a backstop. Store a fingerprint in a
   one-row `derived_meta` table. It combines:
   - the text of the core view SQL, the derivation SQL, and the source of
     `src/derive.py`, so editing a salary regex or a source's field mapping still
     re-derives every row, which is what ADR-002 promises;
   - a cheap summary of the data: `COUNT(*)` and `MAX(last_seen)` of `job_raw`,
     `COUNT(*)` and `MAX(fetched_at)` of `job_detail`, and `COUNT(*)` of
     `job_provenance`.

   If the fingerprint doesn't match, rebuild before returning, under the existing
   `_SCHEMA_LOCK`. Measure what the check costs. The target is under 10 ms, and it
   runs once per connection (the web app reuses one per thread).

Rebuild in **one transaction** (`DELETE` then `INSERT … SELECT`, then write the
fingerprint). WAL lets readers keep the old snapshot until commit, so a fetch
running from the UI never shows a half-built table. It takes about 0.85 s at the
current size, holds the write lock that long, and grows linearly with row count.

### Schema

Bump `SCHEMA_VERSION` to 4. `_build_schema` creates `job_derived` and
`derived_meta` and runs the first fill. `_views_stale` compares the new `jobs`
definition, so an old database rebuilds itself on first connect. Dropping views
before `migrate()` (ADR-008 bug 3) still applies. The table now depends on the
views, so the order becomes: drop views, migrate, create views, rebuild derived.

## Step 2 — load descriptions only when a row is expanded

- `/api/jobs` selects an explicit column list, which is the `jobs` columns minus
  `description`, instead of `j.*`. Search still filters on `description` in SQL; the
  text just isn't sent to the browser.
- New read-only **`GET /api/job?id=<source_job_id>`** returns `description` plus
  the fields the drawer shows. Pass the id as a query parameter, because the id
  contains a `:`. It is a GET, so the read-only hosted deployment serves it with no
  `mutating` change.
- In `index.html`, the drawer shows "loading…", fetches once, and keeps the
  result in `state` so re-expanding is instant. A failed fetch says so in the
  drawer and never throws.
- Expected: about 632 KB becomes about 60 KB per page of 100 rows.

## Tests

Write each one before the change it covers where it can fail first, and break the
code once to confirm it goes red (AGENTS.md).

- **Same answers:** on the fixture database, `SELECT * FROM jobs` from the new
  table-backed view matches the old view definition row for row and column for
  column. Keep the old SQL in the test as the reference. This is the main guard; it
  catches column-order mistakes too, because the columns are matched by position.
- **Column order:** `PRAGMA table_info`-style column list of `jobs` is
  unchanged.
- **Status is live:** after `set_status`, the `jobs` view shows the new
  status with no rebuild.
- **Time is live:** `days_old`/`expired` are computed at query time. Build the
  table, move `posted_ts` back, and check `days_old` follows it without a rebuild.
- **Every writer refreshes:** after `upsert_job` + `rebuild_derived`, `enrich_ids`
  (fake transport), `prune_stale(apply)`, `reconcile_roles(apply)` and `sync`, the
  table reflects the change.
- **The backstop works:** write to `job_raw` directly without rebuilding, reconnect,
  and the row appears. Also change the recorded derivation fingerprint and check
  that reconnecting rebuilds.
- **Export stays clean:** the exported corpus holds no user-state columns in
  `job_derived` (it has none) and still has zero marks and events.
- **API:** `/api/jobs` rows have no `description`; `/api/job?id=` returns it;
  an unknown id gives 404; search still matches on description text.
- **Browser:** add a drawer check to `scripts/check_layout.py`: expanding a row
  shows its description.
- `quality/test-manifest.yaml`: a CAP-STORE journey for "derived table matches
  the raw-derived view and never serves stale rows" (P1), with the tests above.

## Docs

- **ADR-015** (new): store the derived columns; amends ADR-002. It records the
  measurements above, the two protections against drift, and what is still derived at
  query time. Link it from `docs/index.md`.
- Update ADR-002 to say it is amended by ADR-015. Accepted ADRs are not rewritten,
  only linked.
- Update the "Store raw, derive later" line in `AGENTS.md` and the matching
  section in `docs/engineering/conventions.md`: derived columns now live in
  `job_derived`, rebuilt by `rebuild_derived`, and `jobs` is still the only thing to
  query.
- `docs/architecture/repository-map.md`: add the new table.

## Risks

- **Stale reads** if a writer is missed and the fingerprint does not see the
  change, for example an in-place `job_raw` update that changes neither the count
  nor `last_seen`. `upsert_job` always bumps `last_seen`, so check that it does. The
  "every writer refreshes" test is the real guard.
- **Corpus size.** The table holds a second copy of `description` (about 30 MB)
  so that search stays fast. The published corpus is 46 MB after VACUUM, against
  Vercel's 250 MB bundle limit. Measure the exported size. If it matters, the
  fallback is to drop `job_derived` in `export` and let the hosted cold start
  rebuild it (about 1 s, and `stage_corpus` already copies it somewhere writable).
- **Rebuild cost grows with the corpus.** Linear, about 0.85 s per 6k rows. That is
  fine after a 75-minute fetch, but noticeable after a one-job enrich from the UI.
  Only rebuild per row if measurement says it matters.
- **Column order.** The thin view must list columns in exactly today's order, and
  the "same answers" test pins it.

## Out of scope

- Merging `/api/jobs` and `/api/facets` into one request (step 3 of the original
  suggestion). Revisit after measuring; it may not be needed.
- Full-text search (FTS5).
- Speeding up `/api/pending` (186 ms). It runs on load and after an answer, not on
  every click.
- Any frontend framework.

## Acceptance

Re-measured 2026-09-18 on a copy of the real corpus (6,342 jobs), best of 5,
FastAPI `TestClient`:

| | Before | After | Target |
|---|---|---|---|
| `/api/jobs` | 530–1040 ms, 632 KB | **40 ms**, 159 KB | < 100 ms, < 100 KB |
| `/api/jobs` search "python" | — | 133 ms | |
| `/api/facets` | ~700 ms | **220–240 ms** | < 200 ms |
| `/api/facets` search "python" | 672 ms | **187 ms** | |
| `/api/job` (drawer) | — | 5–6 ms, ~5 KB | |
| upsert, per job | — | +0.5 ms (triggers) | |
| first connect on an old DB | — | 932 ms, once | |
| exported corpus | 59.3 MB | **100.5 MB** | measure |

- [x] `/api/jobs` under 100 ms. **Payload target missed:** 159 KB, not < 100 KB.
  What remains is 43 short columns per row, mostly the repeated JSON key names;
  `badges` and `high_concept` are the largest at ~20 KB each. Not pursued.
- [ ] `/api/facets` under 200 ms. **Missed by about 20–40 ms.** The two joins
  (locations 69 ms, roles 42 ms) are most of it. Not pursued. Revisit only if it is
  noticeable in use.
- [x] `SELECT * FROM jobs` identical to the old view on a three-source fixture
  (`test_jobs_matches_the_old_per_query_derivation`, with the old SQL kept verbatim
  in the test as the reference)
- [x] Every writer and the connect-time check are tested and shown to fail when broken
- [x] Exported corpus size measured: 59.3 → 100.5 MB (+41 MB, more than the 30 MB
  estimated). Under Vercel's 250 MB limit; the fallback is recorded in ADR-015.
- [x] ADR-015 written; ADR-002, AGENTS.md, conventions, repository map, README and
  index updated
- [x] Suite green (below)

## Deviations from the plan

- **Triggers instead of rebuild calls at each write site.** Implementation showed
  dozens of existing tests (and `carry_forward_marked`, which writes via ATTACH)
  writing to the input tables and reading `jobs` with no rebuild in between: exactly
  the forgotten-call drift the plan named as its main risk. Triggers on the four
  input tables refresh the affected rows in the writer's transaction. There is no
  `rebuild_derived()`, and the seven `rebuild_locations()` call sites are unchanged.
  The fingerprint shrank to code only (view SQL, triggers, indexes, `derive.py`),
  since data changes can no longer be missed.
- **Consequence: writing the input tables now needs the UDFs**, as reading `jobs`
  already did. One test that rewinds a DB through plain `sqlite3` now calls
  `derive.register(raw)`. Recorded in ADR-015 and the repository map.
- **Provenance became correlated subqueries**, sorted. The grouped CTE was
  materialised over all of `job_provenance`, so a one-row refresh cost 14 ms.
  `found_via_roles` / `found_via_locations` are now alphabetical, where before the
  order was whatever the scan produced. The equivalence test compares them as sets.
- **No `SCHEMA_VERSION` bump.** A missing fingerprint or a view that doesn't match
  already triggers the build; a version number would have added nothing.
- **Added: the search text is matched once per `/api/facets` request** (a temp
  table), not rescanned by each of 8 queries. 672 → 187 ms. Outside the original
  step list, but the same objective. Pinned by
  `test_facets_under_a_search_agree_with_the_row_list` (both `hide_expired` values).
- **Added:** `scripts/check_layout.py` gained a drawer check (red with `/api/job`
  broken, verified) and a tray check (the Hide-button fix from the same day).

## Validation

- `python -m pytest tests -q` → `372 passed in 13.12s`
- Guards broken one at a time; each went red: triggers removed (11 failed),
  fingerprint ignored (1), status stored in the table (4), search hits with
  hide_expired folded in (1), description shipped again (1)
- `python quality/validate_manifest.py` → `manifest valid (60 journeys, 60 tests, 7 open gaps, 0 warning(s))`
- `python quality/validate_context.py` → `context valid (32 docs, ...)`
- `scripts/check_layout.py` against a server on the upgraded copy: all checks pass,
  including TRAY and DRAWER
- **Not run:** the hosted (Vercel) deployment. The exported corpus carries a
  matching fingerprint, so a cold start should not rebuild, but that was not
  observed on Vercel. Not run against a live `fetch` either (it would hit the
  network); the triggers are covered offline by the write-path tests.

## Log

- 2026-09-18: plan written from the measurements above.
- 2026-09-18: implemented steps 1 and 2 as recorded under Deviations. Still open:
  the facets target (~20–40 ms over) and the payload target (159 KB). Neither is
  being pursued unless it is noticeable in use. Move this plan to `completed/`
  once it has been used for a while with no stale-row reports.

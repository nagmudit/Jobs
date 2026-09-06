---
status: completed
created: 2026-09-06
last_updated: 2026-09-06
areas: [jobsearch/src/store.py, jobsearch/src/web/app.py, jobsearch/tests]
---

# `store.connect()` races itself on view creation

## Objective

`connect()` is safe to call concurrently and repeatedly. **Done.**

## The bug

Serving the UI 500'd on load:

```
File "src/web/app.py", line 411, in facets
File "src/store.py", line 336, in connect
    conn.execute(sources.core_view_sql())
sqlite3.OperationalError: view jobs_core already exists
```

`connect()` did two jobs at once — open a connection, and re-establish the schema —
and the schema half unconditionally dropped and recreated both views on every call.
Views are properties of the **database file**, not the connection. The web app opens
a connection per request (`app.py::db`), the UI fires `/api/jobs` and `/api/facets`
without awaiting either (`index.html:303`, and again on boot), and FastAPI runs those
sync endpoints in anyio's threadpool. Two connections interleaved their DROP/CREATE
and the loser died. `migrate()` and `INDEXES` sat inside the window, making it wide.

Measured before the fix: **16 of 20** concurrent `/api/facets` against the real
corpus failed; 6 of 8 bare concurrent `connect()` calls failed.

The quiet half was worse than the crash: one connection could `DROP VIEW jobs` while
another was mid-query against it.

## What changed

**Views are rebuilt only when stale** (`store.py`). `_views_stale` compares the
registry's SQL against `sqlite_master`; `_needs_migration` asks `migrate()`'s own
questions up front. If neither says work is due, `connect()` issues no DDL at all.
The two conditions are OR-ed into one branch deliberately — the drop-views-before-
`migrate()` ordering still has to hold, because a migration that rebuilds a table
cannot DROP it while a view references it.

**The rebuild is serialised** by `store._SCHEMA_LOCK`, which also covers the
`SCHEMA` executescript — on a cold database eight threads racing that produced
`database is locked`.

**`OUTER_VIEW` → `CREATE_OUTER_VIEW`**, a bare statement run via `conn.execute`
rather than a script. `executescript` issues an implicit COMMIT, and the leading
`DROP VIEW IF EXISTS jobs;` was redundant with the explicit drop.

**`assert_core_views` still runs on every connect**, outside the conditional — it is
read-only and AGENTS.md says never bypass it.

**Thread-local connections** in `web/app.py`. The old `db()` opened a handle per
request and never closed it.

## Verification

- `python -m pytest tests -q` → **246 passed** (240 before, plus 6 new).
- New `tests/test_store_concurrency.py` (STORE-015, journey CJ-052), 6 tests.
- Every new guard verified failable by mutation:
  - `_views_stale → False` → 2 red
  - `_needs_migration → False` → 1 red
  - `_SCHEMA_LOCK → nullcontext` → 12/12 runs red
- End-to-end against the real 32MB corpus: pre-fix 4/20 `/api/facets` succeeded,
  post-fix 20/20.

The lock mutant initially went red only 5/12 — a single cold-database round caught it
by coin flip. `test_concurrent_connect_on_a_cold_database` now repeats across 20
independent databases, which took detection to 12/12. A concurrency test that catches
the regression half the time is not a guard.

## Remaining

- `GAP-013` narrowed, not closed. The migration **ordering** is now covered; the
  migrations' own content (each `ADD_COLUMNS` entry, the v1→v2 rewrite) is still
  untested.
- Cross-process concurrency (a CLI run alongside `serve`) rests on the staleness
  check plus SQLite's default 5 s busy timeout. Not exercised by a test.

## Done when

- [x] Concurrent `connect()` raises nothing
- [x] Steady-state `connect()` issues no DDL
- [x] Views still rebuild when the registry changes
- [x] Migrations still run when views look current
- [x] Suite green, new guards verified failable
- [x] Manifest (CJ-052, STORE-015, GAP-013) and docs updated

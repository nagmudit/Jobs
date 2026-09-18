# ADR-015: Store the derived columns in a table, kept exact by triggers

**Status:** accepted · **Date:** 2026-09-18 · **Amends** [ADR-002](ADR-002-raw-first-storage.md)

## Context

ADR-002 derives every filterable column per query, in the `jobs` view, and
predicted this would *"need materialising well before 10⁶ rows."* It was needed
at 6×10³. Measured on the real corpus (6,342 jobs, 60 MB) on 2026-09-18:

| | Before |
|---|---|
| `SELECT * FROM jobs_core` | 335 ms |
| `SELECT * FROM jobs` | 441 ms |
| one facet `GROUP BY` over `jobs` | 125 ms |
| `POST /api/jobs` (100 rows) | 530–1040 ms, 632 KB |
| `POST /api/facets` | ~700 ms |

About three quarters of the cost is `json_extract` over `raw_json`; the Python
UDFs add the rest. Every filter click runs ~11 such queries, so the UI took
1.2–1.7 s to respond. The browser was not the bottleneck: rendering takes a
few ms, so a frontend framework would not have changed this.

## Decision

- **`job_derived`** is a table holding what the old view computed, filled from a
  new view, **`job_derive`**, which holds the derivation SQL. That SQL exists
  once, and the table is filled from it both wholesale and one row at a time.
- **`jobs` stays a view**, and stays the only thing readers query. It is
  `job_derived` plus two things that are never stored:
  - **user state** (`status`, `note`), joined live. A click must show up
    without a rebuild, and the table is part of the published corpus, where marks
    must never appear (`cli export`).
  - **values computed against the clock** (`days_old`, `expired`), which a stored
    copy would get wrong by the next day.

  Its columns and their order are unchanged.
- **Triggers** on `job_raw`, `company_raw`, `job_detail` and `job_provenance`
  re-derive the affected rows in the writer's own transaction. A company row
  feeds every job of that company; an UPDATE refreshes both the old and new key.
- **A fingerprint** in `derived_meta` covers what triggers cannot see: the view
  SQL, the index and trigger definitions, and the bytes of `src/derive.py`.
  `connect()` rebuilds everything when it does not match, in one transaction.
- The provenance columns are correlated subqueries rather than a grouped CTE,
  which made a one-row refresh cost 14 ms by aggregating all of
  `job_provenance`. They are now sorted, where before the order was whatever
  the scan produced.

## Why triggers, not a rebuild call at each write site

The first plan was a `rebuild_derived()` call wherever inputs are written, next
to the seven existing `rebuild_locations()` calls. That is the failure ADR-002
rejected materialising for: the first write site that forgets the call serves
stale rows with HTTP 200. The existing tests showed the risk straight away,
because dozens of them write and then read `jobs` with no rebuild in between. A
trigger cannot be forgotten, and it keeps rows appearing live during a
75-minute fetch, as they did before.

## Consequences

- After: `/api/jobs` 40 ms; `/api/facets` ~220 ms, or ~190 ms while searching
  (670 ms before the search clause was matched once per request instead of once
  per facet); one upsert costs ~0.5 ms more. Upgrading an existing database is a
  one-off ~0.9 s rebuild on first connect.
- **Writing `job_raw` and the other inputs now also needs the UDFs.** ADR-002
  already made reading `jobs` fail from a plain `sqlite3` client; writing the
  input tables now fails the same way ("no such function"), because the triggers
  derive. Write through `store.connect()`, or call `derive.register(conn)` first.
- **ADR-002's promise still holds**, one step later: an improved parser
  re-derives every row on the **next connect** rather than the next query. The web
  server keeps its connections per thread, so a changed `derive.py` needs a
  server restart, which it needed anyway to load the new Python.
- **The published corpus grew** from 59.3 MB to 100.5 MB (measured with
  `cli export`), mostly a second copy of each description, which search reads.
  *Correction, 2026-09-18:* the limit that applies is Vercel's **225 MB per
  function**, uncompressed, not 250 MB. The CI corpus reached 251.9 MB and the
  deploy failed at 329 MB. Fixed by bundling the corpus gzipped (~4× smaller;
  `scripts/vercel-build.sh`) and decompressing it into `/tmp` once per cold start
  (`hosted.stage_corpus`, ~0.3 s per 100 MB). The table stays in the corpus, so
  cold starts do not rebuild it. The GitHub release asset stays uncompressed, so
  `sync` is unchanged. See `docs/engineering/deployment.md`.
- `/api/jobs` no longer returns `description`. The drawer fetches it from
  `GET /api/job?id=…` and caches it for the page's life. Search still filters on
  it server-side.
- The rebuild on a fingerprint mismatch takes the write lock for about a second
  at this size, growing linearly with the corpus. WAL readers keep the old
  snapshot until it commits.

## Alternatives

- **A frontend framework (React, Preact).** Rejected: measurement put the
  whole cost on the server.
- **A rebuild call at each write site.** Rejected: see above.
- **Dirty flags plus a UNION view** (triggers mark rows; `jobs` derives dirty
  rows live). This would keep plain-`sqlite3` writes working, but the view gets
  more complex and slows back down during a fetch, when most rows are dirty. Not
  worth it for writers that do not exist outside tests.
- **FTS5 for search.** Deferred. Matching the search text once per facet request
  was enough for now.

## Related

`jobsearch/src/store.py` (`CREATE_DERIVE_VIEW`, `TRIGGERS`, `_derivation_hash`,
`_build_schema`) · `jobsearch/src/web/app.py` (`/api/job`, `facets`) ·
`jobsearch/tests/test_derived_table.py` · [plan](../../plans/active/ui-query-speed.md)

# ADR-009: One role-driven fetch across all sources; only Wellfound filters server-side

**Status:** accepted · **Date:** 2026-09-04

## Context

The UI had two disconnected workflows: a live role-driven crawl for Wellfound, and a
broad "pull from APIs" ingest for RemoteOK and Himalayas in which the role played no
part. Picking a role only ever fetched Wellfound.

Making the role drive all three required establishing what each source can actually do.
Probed 2026-09-04.

## What the probing found

**Himalayas has no server-side filtering at all.** `search`, `q`, `query`, `category`,
`categories`, `keyword`, `title` and `seniority` are all silently ignored.
`search=engineer` returned *Grants Coordinator*, *Account Executive* and *Industrial
Power Systems Inspector*. Alternate paths (`/jobs/api/<category>`, `/api/jobs`) 404/403.

**RemoteOK's `?tag=` is real but unusable for this**, for two independent reasons:

1. *Low relevance.* Title-relevance by tag: `software` 83%, `machine learning` 60%, then
   a cliff — `backend` 9%, `react` 5%, `devops` 3%, `mobile` 1%. `?tag=react` returns
   *Aviation Maintenance Technician*; `?tag=mobile` returns *Regional Sales Manager*.
2. *The tag endpoints serve an archive.* Measured age distribution:

   | endpoint | n | median age | within 30 days |
   |---|---|---|---|
   | `/api` (no tag) | 100 | **5 days** | **100** |
   | `?tag=ai` | 75 | 112 days | 1 |
   | `?tag=machine learning` | 20 | 144 days | 0 |
   | `?tag=software` | 30 | 136 days | 0 |

   Combined with the 30-day cutoff (ADR-006), any tag yields essentially nothing.

RemoteOK does **not** silently fall back on an unknown tag — it returns zero jobs, which
is safer than Wellfound's silent role fallback (ADR-003). Also note the tag vocabulary is
literal and surprising: `frontend` returns 0 while `front end` returns 100; `fullstack`
returns 0 while `full stack` returns 99.

## Decision

**One `fetch` workflow.** `python -m src.cli fetch --roles <a,b>` and the UI's
"Fetch selected" run every enabled source for each role, via `src/roles.py::fetch_role`.

**Each result carries `filter_mode`, and the UI shows it:**

| mode | source | meaning |
|---|---|---|
| `server` | Wellfound | `/role/{slug}` really is role-filtered by the server |
| `local` | RemoteOK, Himalayas | we fetched the live feed and filtered it ourselves |
| `skipped` | any | no usable query for this role, with a reason |

**RemoteOK role fetches deliberately send no tag.** The live unfiltered feed plus a local
title filter beats a stale tag endpoint. The `role_map` tags remain in `targets.yaml`,
validated and annotated, for deliberately mining older postings.

**Matching is on title and categories, never descriptions** (`src/relevance.py`), with
space-padded word-boundary comparison. And for RemoteOK, **title only** — its own tags are
unreliable enough that `?tag=engineer` returns *Kitchen Technician*, *Joiner* and
*JANITOR*, all genuinely carrying the `engineer` tag. Matching on those tags put
carpenters in `software-engineer` during development.

**Provenance records the role, not the source's query token**, so `found_via_roles` is one
vocabulary across sources (partially closes `GAP-015`).

## Alternatives

- **Use RemoteOK's tag as a pre-filter and locally filter on top** — the original plan.
  Rejected once the archive behaviour was measured: it costs the same request and returns
  almost nothing inside the age cutoff.
- **Deep-page Himalayas until N role matches** — better coverage, ~10 min per role at
  3–5 s over a feed that drifts while you page. Explicitly deferred.
- **Present role fetch as uniform across sources** — rejected. It would imply filtering
  the sources did not do.

## Consequences

- Measured yield per role, per run: Wellfound hundreds (server-side), Himalayas ~40 of
  500 scanned (~8%), RemoteOK ~4 of 100. Both API sources are **supplements**, not
  primary sources, and the numbers say so plainly.
- `filtered_out` ("wrong role") is counted separately from `stale_skipped` ("too old"),
  the same separation ADR-006 established for the age cutoff versus the page cap.
- A role missing from `role_map` keeps everything rather than silently returning nothing;
  `fetch_role` skips it with a reason instead.
- Jobs ingested **before** this change carry provenance like `all`, `dev` or `engineer`
  and were never role-filtered. They remain in the corpus and are visible in the
  "found via role" facet. Filter or prune them deliberately; nothing deletes them
  automatically.
- `/api/crawl` and `/api/ingest` survive for CLI parity and broad ingests; the UI drives
  `/api/fetch`.

## Related

`src/roles.py` · `src/relevance.py` · `src/sources/remoteok.py` ·
`src/sources/himalayas.py` · `jobsearch/tests/test_roles.py` · ADR-003 · ADR-006 · ADR-008

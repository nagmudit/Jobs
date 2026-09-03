# ADR-005: Crawl by role only; filter location locally

**Status:** accepted · **Date:** 2026-09-04

## Context

The crawler originally used `(role x location)` slices, where location was a token
Wellfound pre-filtered on (`bangalore`, `india`, `remote`). Two problems surfaced once
there was a UI on top:

1. **The location filter was wrong.** It offered the three *slice tokens*, because
   that is what `job_provenance` records. The jobs themselves carry 700+ real places
   in `locationNames` — Pune, San Francisco, Bengaluru, Hyderabad. A user filtering
   for Pune had no way to express it.
2. **Pre-filtering by location costs recall.** Each slice is capped at ~15-20 pages
   regardless of `totalJobCount`, so asking Wellfound to narrow first spends the page
   budget on a subset. `/role/{slug}` claims 14,124 jobs for
   `artificial-intelligence-engineer`; `/role/l/{slug}/bangalore` claims 596.

## Decision

Crawl `/role/{slug}` — a third page shape (`roleSearch`, `role` in the GraphQL args,
no location member), reached with the location token `anywhere`. Filter location
**locally**, in SQL, against a `job_location` table derived from `locationNames` and
`acceptedRemoteLocationNames`.

The UI crawls only the roles the user selects, one crawl at a time.

## Rationale

Wellfound's location filter is a coarse pre-filter over the same data we already
store. Doing it locally is strictly better: 700+ real places instead of 3 tokens,
every place is selectable, and the page budget is spent on breadth rather than on a
slice we could have computed ourselves.

`job_location` is materialised rather than a view because faceting queries it on every
keystroke. It is still a pure derivation — `rebuild_locations()` is one offline pass
over `job_raw` — so it honours ADR-002 and needed no re-crawl to backfill the 1,836
jobs already stored.

## Alternatives

- **Keep location slices** — rejected: spends the page cap on a filter we can apply
  for free, and cannot express "Pune".
- **`json_each` in the query rather than a table** — correct and always in sync, but
  facets run ~7 queries per keystroke and each would re-parse every row's JSON.
- **Crawl every configured role on every run** — rejected: 10 roles x 20 pages is
  ~13 minutes of rate-limited requests for roles the user may not care about today.

## Consequences

- `locations` in `targets.yaml` is now `[anywhere]`. The `remote` and named-place
  shapes still work and are still tested; they are simply not the default.
- Location slices remain useful for **defeating the page cap** (ADR-004) on a role
  with far more jobs than 20 pages can reach. That is now a deliberate choice rather
  than the default.
- `found_via_locations` still exists and still records the slice token. It is
  provenance, not a place, and the UI no longer presents it as one.
- `rebuild_locations()` must run after any crawl. The UI crawl calls it; a CLI crawl
  does too. Forgetting it leaves the location facet stale — an ordinary staleness bug,
  not a silent-wrongness one, since counts would simply lag.
- Adding a role is a `targets.yaml` edit. The API rejects any slug not listed there,
  so the UI cannot crawl arbitrary user input.

## Related

`jobsearch/src/fetch.py::search_url` · `jobsearch/src/store.py::rebuild_locations` ·
`jobsearch/src/web/app.py::facets` · `jobsearch/tests/test_facets_locations.py`

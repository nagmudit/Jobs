---
status: current
last_verified: 2026-09-06
applies_to: [jobsearch/src]
---

# Conventions

Split out of `AGENTS.md` on 2026-09-06: the list kept growing past the 200-line
budget that keeps that file readable. `AGENTS.md` carries the handful of rules
you cannot work without; this carries the rest. **Read both before changing
`src/`.**

- **Store raw, derive later.** Apollo nodes go into `job_raw.raw_json` verbatim.
  Filterable columns are derived in the `jobs` view via `json_extract` plus the SQL
  functions in `src/derive.py`. Never normalise at ingest — re-deriving is free,
  re-crawling is not. See ADR-002.
- **The four assertions raise, never warn.** `SilentRoleFallback`, `PageWrap`,
  `YieldFloor`, `SchemaDrift` all guard failures that return HTTP 200 with plausible
  data. Downgrading one to a log line silently corrupts the corpus. See ADR-003.
- **Nothing about roles or locations is hardcoded.** They live in `targets.yaml`,
  overridable with `--roles` / `--locations`. Adding a literal role slug to `src/` is
  a bug. The web API refuses any slug not listed in `targets.yaml`.
- **Three URL shapes, one function.** `search_url` maps location `anywhere` ->
  `/role/{slug}` (widest, the default), `remote` -> `/role/r/{slug}`, and any other
  token -> `/role/l/{slug}/{token}`. All three are verified live and tested.
- **"Location" means two different things; do not conflate them.** A *slice token*
  (`anywhere`/`remote`/a city) is what we asked Wellfound to pre-filter on, recorded
  in `job_provenance.location`. A *place* (Pune, San Francisco) comes from
  `locationNames` and lives in `job_location`. The UI filters on places. See ADR-005.
- **The age cutoff never ends a slice early.** `max_age_days` (default 30) filters
  jobs at ingest, in enrichment, and in the UI. It must NOT stop paging: Wellfound
  does not order results by date, so a page of only-stale jobs is followed by pages
  with fresh ones. Early termination would silently lose them. See ADR-006.
- **Unknown age is not old.** A job with no `liveStartAt` is kept by the cutoff.
- **IDs are namespaced `"<source>:<native_id>"`.** `S.upsert_job` takes the native id
  and namespaces it; `S.add_provenance` takes the already-namespaced uid. Mixing them
  up silently orphans provenance. See ADR-008.
- **`CORE_COLUMNS` is positional.** UNION ALL aligns by position, not name, so a source
  view that reorders a column silently returns another column's values.
  `assert_core_views` checks this on every connect — never bypass it.
- **Numeric salary only where the source states an annual figure.** Hourly and annual
  in one column sort silently wrong. Carry `salary_currency` / `salary_period`.
- **Sort salary on `salary_usd_*`, display `salary_raw`.** Native figures are not
  comparable — ₹1.2cr outranked $520k on the old sort because the number was bigger.
  The USD value is approximate and exists ONLY to order; never show it as the salary.
  Unknown currency stays NULL rather than defaulting to USD. See ADR-012.
- **`expired` NULL means unknown, not "not expired".** Only Himalayas publishes an
  expiry. Any filter must keep NULL rows.
- **Check the origin, not the path, against robots.** Ashby and Workable publish the
  same path shape on two hosts with different rules (`apply.workable.com` allows all;
  `www.workable.com` disallows `/j/`). A source names ONE origin. See ADR-010.
- **A date with no time of day resolves to the END of that day** — unknown age is not
  old (ADR-006). Ingest and `CORE_VIEW_SQL` must apply the *same* rule, or a job is
  stored then immediately hidden by the UI's age filter.
- **ATS boards cannot be searched by role at all.** Greenhouse's docs are explicit:
  every request needs a `board_token`, there is no cross-board search. They EXPAND
  companies the role already touched (`filter_mode: "expand"`), bounded by the corpus,
  never by the provider. Resolution is cached in `company_ats` — **including misses**,
  because re-probing six candidates for a company with no board is pure cost against a
  third party. See ADR-010.
- **Only Wellfound filters by role server-side.** Himalayas ignores every filter
  parameter; RemoteOK's `?tag=` serves an archive (median age 112-144 days) while its
  unfiltered feed is fresh (median 5 days), so role fetches send NO tag and filter
  locally. Every result carries `filter_mode` (`server` / `local` / `skipped`) and the
  UI must show it — never present a local filter as if the source did it. See ADR-009.
- **Match role keywords on title and categories, never descriptions**, and never on
  RemoteOK's own tags: `?tag=engineer` returns Kitchen Technician, Joiner and JANITOR,
  all genuinely carrying that tag. `src/relevance.py` pads with spaces so `ml` does not
  match `html` and `ai` does not match `retail`.
- **`filtered_out` (wrong role) is counted separately from `stale_skipped` (too old).**
  Conflating them makes the telemetry unreadable.
- **Enrichment is Wellfound-only** — the API sources already return descriptions.
- **`rebuild_locations()` after any crawl or ingest.** `job_location` is derived from
  `job_raw`; the crawl paths call it, and a new derivation never needs a re-crawl.
- **FastAPI request models stay at module level** in `src/web/app.py`. With
  `from __future__ import annotations`, a model defined inside `create_app()` is
  invisible to FastAPI's hint resolution and the body silently degrades into a
  required query param (HTTP 422).
- **Wellfound sends `''`, not `null`,** for absent values. The `jobs` view wraps them
  in `NULLIF`. Skipping that makes every "has salary" filter and fill-rate wrong.
- **Unparsed beats wrongly parsed.** `src/derive.py` returns `NULL` for anything it
  doesn't recognise and always keeps the raw string alongside.
- `user_state` is keyed separately from `job_raw` so shortlist/applied/hidden survive
  re-crawls that rewrite job rows. Don't merge those tables.

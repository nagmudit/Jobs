# ADR-008: Multi-source implemented — namespaced ids, layered views, per-source ingesters

**Status:** accepted · **Date:** 2026-09-04 · **Supersedes the "proposed" half of** [ADR-007](ADR-007-multi-source.md)

## Context

[ADR-007](ADR-007-multi-source.md) fixed per-origin robots and listed four things
blocking multi-source. The [platform survey](../../research/job-platform-survey.md)
identified RemoteOK and Himalayas as readable without evasion. This ADR records what
was actually built and the four bugs it surfaced.

## Decision

**Schema v2.** `PRAGMA user_version` now gates data migrations, alongside the existing
additive column adds.

- **IDs are namespaced** `"<source>:<native_id>"`. A single-column primary key is kept
  deliberately: every dependent table (`job_provenance`, `job_detail`, `job_location`,
  `user_state`) keeps its existing shape, so the migration is a string prefix rather
  than a composite-key rewrite of five tables.
- **`company_raw` gained a composite key** `(source, company_slug)`. Two platforms can
  legitimately use the slug `acme`. SQLite cannot `ALTER` a primary key, so this one
  table is rebuilt.

**Three view layers.** `job_raw` → `jobs_core` (UNION ALL of per-source views) →
`jobs` (provenance, detail, user state, derived scalars). A source supplies a
`CORE_VIEW_SQL` mapping its raw JSON onto `CORE_COLUMNS` and an `ingest()`. Nothing
else in the codebase knows a platform's field names.

**Ingesters are separate from the crawler.** `src/crawl.py` stays the Wellfound
ingester; it carries the slice machinery and the four Wellfound-specific assertions,
none of which mean anything to a documented JSON API. `python -m src.cli ingest`
drives the API sources.

## Rationale

ADR-002 paid off exactly as predicted: `raw_json` already stored whatever shape a
source returned, so 3,087 existing Wellfound rows migrated without a re-crawl and the
new sources needed no storage change.

`CORE_COLUMNS` is checked at connect time by `assert_core_views`. **UNION ALL aligns by
position, not by name** — a source that adds, drops or reorders a column produces a
view that still builds and silently returns another column's values. That is a
compile-clean, test-clean, completely wrong result, so it is asserted rather than
trusted.

## Four bugs this surfaced

1. **`expired` was unconditionally true.** `strftime('%s','now')` returns TEXT, and in
   SQLite's type ordering *every INTEGER sorts before every TEXT*. So
   `expires_ts < strftime(...)` was always true and all 478 Himalayas jobs read as
   expired despite expiring 2026-11-02. Fixed with `CAST`; pinned by
   `test_expired_flag_is_not_always_true`.
2. **Indexes were created before the migration that adds their column**, so an existing
   database failed with `no such column: source`. `INDEXES` now runs after `migrate()`.
3. **Views blocked the table rebuild** — `DROP TABLE company_raw` failed with
   `error in view jobs`. Views are now dropped before `migrate()` and recreated after.
4. **WAL nearly cost the backup.** `cp jobs.db` copied a file whose 3,087 rows were
   still in `jobs.db-wal`; the copy read as empty. Always
   `PRAGMA wal_checkpoint(TRUNCATE)` before copying.

## Correctness choices

- **Hourly salary is never emitted as a number.** 3 of 40 sampled Himalayas rows are
  `salaryPeriod: hourly`; an hourly 70 beside an annual 70000 sorts silently wrong.
  Numeric salary is emitted only for `annual`; everything else keeps a raw string and
  NULL numerics. `salary_currency` and `salary_period` are now carried through.
- **`expired` is NULL where the source publishes no expiry.** NULL means unknown, never
  "not expired", and the UI's hide-expired filter keeps NULL rows.
- **Enrichment is Wellfound-only.** Detail pages, JSON-LD and the rendered equity string
  are Wellfound concepts; the API sources already return full descriptions and numeric
  salary, so a detail request would be pure waste. `enrich_ids` skips non-Wellfound rows.
- **RemoteOK's legal element is skipped by shape, not position.** The terms-of-service
  object is element 0 today; skipping index 0 would break the day they reorder it.

## Consequences

- One lock serialises crawl **and** ingest. Concurrency 1 to the network is a
  process-wide rule, not a per-source one.
- `found_via_roles` is Wellfound-shaped. API sources record their target (a RemoteOK
  tag, or `all`) in the same column, which is honest but means the "found via role"
  facet mixes two vocabularies.
- **Cross-currency salary comparison is still wrong**, and now visibly so: the corpus
  holds USD, INR, EUR, CAD, PLN and BRL in one `salary_min` column. This pre-dates
  multi-source (Wellfound already mixed ₹ and $) — recorded as a known gap rather than
  silently tolerated.
- RemoteOK's terms ask for a follow link back and attribution. This tool is local and
  single-user with no public surface, so the republisher clause does not bite; the
  apply link points at RemoteOK and the UI shows the source on every row. **If this ever
  grows a public page, the link-back becomes a real obligation.**

## Measured result

| source | jobs | salary | numeric salary | location | expiry |
|---|---|---|---|---|---|
| wellfound | 3,087 | 2,451 | 2,324 | 2,841 | 0 |
| himalayas | 478 | 269 | 222 | 471 | **478** |
| remoteok | 227 | 9 | 9 | 208 | 0 |

3,792 jobs, 0 mitigations. Himalayas claims `totalCount` 101,642; 25 pages at the
server-enforced `limit=20` is 500.

## Related

`src/sources/` · `src/store.py::migrate` · `jobsearch/tests/test_sources.py` ·
ADR-002 · ADR-006 · ADR-007

# ADR-002: Store raw Apollo nodes, derive filterable columns in a SQL view

**Status:** accepted · **Date:** 2026-09-03

## Context

The probe normalised into a fixed 12-field `Job` dataclass at ingest and discarded
everything else. `companySize`, `badges`, `highConcept`, `jobType`, `remoteConfig`,
`liveStartAt`, `atsSource` and `primaryRoleTitle` were all present in the payload and
thrown away. Recovering any of them meant re-crawling.

Crawling is the expensive, rate-limited, third-party-visible part of this system. It
is also the part we least control.

## Decision

Persist the Apollo node verbatim in `job_raw.raw_json`. Express every filterable
column as a `jobs` VIEW over `json_extract`, with parsing done by Python functions
registered on the SQLite connection (`src/derive.py`).

## Rationale

Re-deriving a column is a view change costing seconds. Re-crawling to recover a field
someone dropped costs hours of rate-limited requests against a site that may block us.
The asymmetry is extreme, so the design optimises for never needing to re-crawl.

Registering the parsers as SQL functions rather than materialising them means
improving a salary regex re-derives every existing row on the next query — no
migration, no backfill, no stale derived table.

## Alternatives

- **Normalise at ingest** — what the probe did. Rejected: lossy, and the loss is only
  discovered later when it is expensive to undo.
- **Materialised derived table** — rejected: needs a backfill job and can drift from
  the raw data.
- **Pure SQL parsing** — attempted first, abandoned. `₹15L – ₹25L` and `₹1.2Cr` are not
  reasonably parseable with `SUBSTR`/`INSTR`, and the SQL was unreadable.

## Consequences

- Adding a filter dimension is a view edit, not a crawl. This is the point.
- **The `jobs` view is unusable from a plain `sqlite3` client** — it fails with
  `no such function: size_label`. Every reader must go through `store.connect()`.
  This is a real ergonomic cost, accepted knowingly.
- `raw_json` duplicates data that also appears in derived columns. At ~1,800 jobs the
  database is a few MB; irrelevant at this scale.
- Derivation runs per row per query. Fine at this size; would need materialising well
  before 10⁶ rows.
- Unparseable values return `NULL`, never a guess, and the raw string is always kept
  alongside — an unparsed value is strictly better than a silently wrong one.

## Related

`jobsearch/src/store.py` (`VIEWS`) · `jobsearch/src/derive.py` ·
`jobsearch/tests/test_derive_store.py`

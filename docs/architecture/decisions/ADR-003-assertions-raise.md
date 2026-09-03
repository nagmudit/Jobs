# ADR-003: Silent-failure guards raise; they are never warnings

**Status:** accepted · **Date:** 2026-09-03

## Context

Wellfound has at least three failure modes that return **HTTP 200 with
plausible-looking data**. Nothing crashes; the corpus just quietly becomes wrong.
All three were hit during development, not theorised:

1. **Invalid role slug.** `/role/l/{role}/{location}` does not 404 on a bad slug — it
   silently serves an unfiltered, location-wide search. `artificial-intelligence`
   returned 1,150 "results" that were never role-filtered; the valid slug
   `artificial-intelligence-engineer` returned 596. We nearly reported the wrong
   number as a finding.
2. **Pagination wraps.** Past the last real page the server re-serves page 1,
   byte-identical, indefinitely. `pageCount` claimed 26; results exhausted at 15;
   pages 16–20 all returned page 1. A naive `for page in range(1, 27)` loop would
   re-ingest page 1 five times and call it new data.
3. **Counts overstate reachability.** A slice claiming 596 jobs and 514 companies
   yielded ~465, because search pages cap at 3 highlighted jobs per company.

A fourth is anticipated rather than observed: Wellfound migrating from the Next.js
Pages Router to the App Router, replacing `__NEXT_DATA__` with RSC flight chunks.

## Decision

Each failure mode gets a named exception that **raises**: `SilentRoleFallback`,
`PageWrap`, `YieldFloor`, `SchemaDrift`. Plus `MitigationDetected` for Cloudflare.
None of them may be downgraded to a log line, a warning, or a skipped record.

Ground truth is the **GraphQL cache key** the server embeds in the page — the server's
own record of what it actually filtered on. Never the URL we requested.

## Rationale

The failure mode here is not downtime, it is silent wrongness. A crash is discovered
in seconds; a corpus quietly missing its role filter is discovered never. Raising is
the only response that cannot be ignored.

Reading the cache key rather than trusting the URL is what makes trap 1 detectable at
all: the URL looks identical in both the working and the broken case.

## Alternatives

- **Warn and continue** — rejected. The whole point is that the data looks fine.
- **Validate against `totalJobCount`** — rejected: that number is itself unreliable
  (trap 3).
- **Trust the URL** — rejected: it is exactly what the failure exploits.

## Consequences

- A crawl can abort mid-run. Mitigated by checkpointing every page to `crawl_unit`, so
  resume costs minutes, not the whole run.
- `PageWrap` is raised for what is actually a **normal, successful** slice ending.
  `crawl_slice` catches it and records `ended_reason='page_wrap'`. This is deliberate
  but reads oddly — it is an exception used for control flow because continuing would
  silently duplicate data.
- `YieldFloor` needs the previous page's yield to distinguish "results ended" from
  "parser broke", so it takes `prev_yield` rather than being a pure function of the
  current page.
- Every guard is tested against synthetic fixtures in `tests/test_assertions.py`.

## Related

`jobsearch/src/assertions.py` · `jobsearch/tests/test_assertions.py` ·
`wellfound-probe/REPORT.md` §5

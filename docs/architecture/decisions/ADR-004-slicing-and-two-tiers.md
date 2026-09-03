# ADR-004: Overlapping slices to defeat the page cap; breadth and depth as separate tiers

**Status:** accepted · **Date:** 2026-09-03

## Context

Each `(role, location)` search slice is capped by Wellfound at roughly 15–20 pages
regardless of what `totalJobCount` claims. A slice claiming 1,954 jobs returned 693.
The cap is per slice, not per account or per IP — no amount of polite paging gets past
it within one slice.

Separately, equity exists nowhere in the search payload and in no ATS. It is only on
job detail pages, which cost one request each. At 3–5 s per request, enriching a
10,000-job corpus is 11+ hours.

## Decision

**Slicing is a recall mechanism, not a filter.** Crawl overlapping
`(role × location)` slices deliberately — including redundant ones such as `bangalore`
when `india` is already crawled — and dedupe on `source_job_id`, recording every slice
that found a job in `job_provenance`.

**Split breadth from depth.** `crawl` walks search pages only and fills the corpus.
`enrich` fetches detail pages and is invoked on demand — from the UI, against the
current filtered set — never corpus-wide.

## Rationale

Each slice gets its own page budget, so more slices means more total reachable jobs
even when the slices overlap heavily. Measured: 9 slices produced 1,836 unique jobs
from 3,331 provenance rows — roughly 1,500 duplicate findings, and that redundancy is
the mechanism working, not waste.

`bangalore ⊂ india`, but under a per-slice cap the `bangalore` slice recovers jobs the
`india` slice truncates away. The same logic recommends adding `hyderabad`, `pune`,
`mumbai`, `delhi` as sub-partitions.

The breadth/depth split is forced by arithmetic: breadth is ~1 request per 20
companies, depth is 1 request per job. Filtering first and enriching ~40 rows is
minutes; enriching everything is a working day.

## Alternatives

- **One broad unfiltered crawl** — rejected: hits the same cap with no way around it,
  and loses role provenance.
- **Enrich during the crawl** — rejected: multiplies crawl cost by ~20× and spends it
  on rows the user will never look at.
- **Raise `max_pages_per_slice` alone** — helps, and should be tried (`remote` slices
  ended at `max_pages`, not `page_wrap`, so the cap was not reached), but does not
  replace slicing.

## Consequences

- Heavy duplication is expected. `job_provenance` carries it as a feature:
  `found_via_roles` and `n_slices` become filter dimensions.
- The corpus is **cross-role**, not role-pure. A `sales-manager` slice puts
  sales jobs in the same table as AI jobs. That is a filter concern, not a crawl one.
- `slice_stats.jobs_recovered / total_claimed` is the truncation signal telling the
  user which slices need sub-partitioning. Surfaced in the UI. Observed range:
  1.00 (`machine-learning-engineer @ bangalore`, complete) to 0.35
  (`ai-engineer @ remote`, badly truncated).
- `has equity` matches nothing until the user enriches. This is expected and
  documented, not a bug.
- Enrichment must be serialised — the web layer holds a lock and returns HTTP 409 on a
  concurrent call, because two overlapping runs would break the rate limit.

## Related

`jobsearch/src/crawl.py` · `jobsearch/src/enrich.py` · `jobsearch/targets.yaml` ·
`jobsearch/src/web/app.py`

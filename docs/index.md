# Documentation Index

The repository is the source of truth. Start here, read only what your task touches.

## Start

| Doc | Read it when | Status |
|---|---|---|
| [`../AGENTS.md`](../AGENTS.md) | Always, first. Contract + conduct rules | current |
| [architecture/repository-map.md](architecture/repository-map.md) | Finding where code lives, and the traps | current |
| [engineering/commands.md](engineering/commands.md) | Running, testing, crawling, serving | current |

## Working on...

| Area | Read |
|---|---|
| Anything that makes a network request | `AGENTS.md` conduct rules, then [decisions/ADR-003](architecture/decisions/ADR-003-assertions-raise.md) |
| Adding another job platform | [ADR-008](architecture/decisions/ADR-008-multi-source-implemented.md), `jobsearch/src/sources/` |
| Fetching a role from every source | [ADR-009](architecture/decisions/ADR-009-role-fetch-across-sources.md), `jobsearch/src/roles.py` |
| ATS boards (Greenhouse, Ashby, Workable) | [ADR-010](architecture/decisions/ADR-010-ats-boards-company-scoped.md), `jobsearch/src/ats.py` |
| Why a re-run finds new jobs (cache TTL) | [ADR-011](architecture/decisions/ADR-011-cache-freshness.md), `jobsearch/src/fetch.py` |
| Conventions for changing `src/` | [conventions](engineering/conventions.md) |
| Comparing salaries across currencies | [ADR-012](architecture/decisions/ADR-012-cross-currency-salary.md), `jobsearch/src/derive.py` |
| Which platforms are readable at all | [research/job-platform-survey.md](research/job-platform-survey.md) |
| Crawling, pagination, slices | [ADR-004](architecture/decisions/ADR-004-slicing-and-two-tiers.md), [ADR-005](architecture/decisions/ADR-005-crawl-wide-filter-locally.md), `jobsearch/src/crawl.py` |
| Locations, filters, facets | [ADR-005](architecture/decisions/ADR-005-crawl-wide-filter-locally.md), `jobsearch/src/web/app.py` |
| Job age, staleness, pruning | [ADR-006](architecture/decisions/ADR-006-age-cutoff.md), `jobsearch/src/config.py` |
| Adding a filter or column | [ADR-002](architecture/decisions/ADR-002-raw-first-storage.md) — it is a view change, **not** a re-crawl |
| Parsing Wellfound's payload | [ADR-001](architecture/decisions/ADR-001-static-http-over-atsfallback.md), `wellfound-probe/REPORT.md` |
| Equity, detail pages, enrichment | [ADR-004](architecture/decisions/ADR-004-slicing-and-two-tiers.md), `jobsearch/src/enrich.py` |
| The UI | `jobsearch/src/web/app.py` (SQL filtering), `static/index.html` (everything else) |
| Writing or changing tests | [testing/report.md](testing/report.md), [`../quality/test-manifest.yaml`](../quality/test-manifest.yaml) |

## Decisions

Accepted ADRs constrain future work. Read the one covering your area before changing it.

| ADR | Decision |
|---|---|
| [ADR-001](architecture/decisions/ADR-001-static-http-over-atsfallback.md) | Static HTTP + `__NEXT_DATA__`, not the ATS fallback |
| [ADR-002](architecture/decisions/ADR-002-raw-first-storage.md) | Store raw, derive columns in a SQL view |
| [ADR-003](architecture/decisions/ADR-003-assertions-raise.md) | Silent-failure guards raise, never warn |
| [ADR-004](architecture/decisions/ADR-004-slicing-and-two-tiers.md) | Overlapping slices; breadth vs. depth tiers |
| [ADR-005](architecture/decisions/ADR-005-crawl-wide-filter-locally.md) | Crawl by role only; filter location locally |
| [ADR-006](architecture/decisions/ADR-006-age-cutoff.md) | 30-day age cutoff at ingest; never an early stop |
| [ADR-007](architecture/decisions/ADR-007-multi-source.md) | robots is per-origin; what multi-source would cost |
| [ADR-008](architecture/decisions/ADR-008-multi-source-implemented.md) | Multi-source built: namespaced ids, layered views, ingesters |
| [ADR-009](architecture/decisions/ADR-009-role-fetch-across-sources.md) | One role-driven fetch; only Wellfound filters server-side |
| [ADR-010](architecture/decisions/ADR-010-ats-boards-company-scoped.md) | ATS boards expand companies; they cannot be role-searched |
| [ADR-011](architecture/decisions/ADR-011-cache-freshness.md) | Listings expire from the cache; a cached mitigation never does |
| [ADR-012](architecture/decisions/ADR-012-cross-currency-salary.md) | Salary sorts on an approximate USD value, displays the native one |

## Quality

| Doc | Holds |
|---|---|
| [`../quality/test-manifest.yaml`](../quality/test-manifest.yaml) | Capabilities, journeys, tests, and the open-gap ledger |
| [`../quality/validate_manifest.py`](../quality/validate_manifest.py) | Manifest validation. Run it before claiming a gap is closed |
| [`../quality/validate_context.py`](../quality/validate_context.py) | Drift checks on these docs: dead paths, broken links, secrets, bloat |
| [testing/report.md](testing/report.md) | 2026-09-03 audit: verdict, exposures, sequence |

## In flight

[plans/active/](plans/active/) — read before touching an area with an open plan.

- [ats-board-integrations.md](plans/active/ats-board-integrations.md) — integrating the
  four ATS boards one provider at a time. Greenhouse and Ashby done; **Workable next**.
- [close-conduct-test-gaps.md](plans/active/close-conduct-test-gaps.md) — the conduct
  layer is untested; this closes it and adds CI.

## Research

| Doc | Holds |
|---|---|
| [research/job-platform-survey.md](research/job-platform-survey.md) | Which other job platforms are readable without evasion, probed 2026-09-04 |

## Research archive

`wellfound-probe/REPORT.md` — the feasibility study behind ADR-001. Six paths measured
against the live site, with verbatim evidence. Accurate as of 2026-09-02; it describes
a third-party site that can change without notice. **Do not extend the probe code** —
it is frozen. Its `results/*.json` hold the raw measurements behind every number.

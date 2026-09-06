---
status: completed
created: 2026-09-06
last_updated: 2026-09-06
areas: [jobsearch/src/roles.py, jobsearch/src/web, jobsearch/src/config.py, deploy]
---

# Fetch progress heartbeat, and an unattended daily run

## Objective

A running fetch is distinguishable from a hung one, and the fetch runs itself daily
without the user's PC. **Phase 1 done. Phase 2 written, not yet run on a host.**

## The problem

A cold all-sources fetch is ~180 minutes and the only evidence it was alive was a
disabled button. The UI polling was never missing — `pollCrawl()` and
`/api/crawl/status` already ran every 2 s. The counters they read barely moved:

- `_expand_ats` did `if not rec.get("resolved"): continue` with no event. But
  `ATS.resolve` probes up to six token candidates at 3–5 s each, so a company with no
  board burned ~30 s in total silence. Across four ATS providers and hundreds of
  companies that was most of the run.
- `pages` counted *resolved* companies, under-reporting work done.
- `state["current"]` was only ever the role slug — never source, company, or phase.
- Nothing printed server-side: `serve` runs uvicorn at `log_level="warning"` and the
  fetch thread had no printer, unlike `cmd_fetch`.

## What changed — Phase 1

**Heartbeat on `Fetcher.on_response`** (`web/app.py::CrawlJob._note_response`). That is
the single chokepoint all network access goes through, and it fires for every response
including cache hits, *before* `check_mitigation`. Hanging the heartbeat there means no
code path can be busy and silent — including sources added later — and the response
that halts a crawl still records where it died. `requests`, `cached`, `last_at`,
`last_url`.

**Every company reports** (`roles.py::_expand_ats`). The `continue` stays; the event
fires either way, carrying `company`, `resolved`, and `unit`/`unit_total`. The
expansion is bounded by the corpus, so "company 37 of 312" is an honest fraction.

**`snapshot()` carries the server clock.** "last activity 3 s ago" is computed against
`s.now`, never the browser's clock — a skewed client would otherwise invent a hang or
hide a real one.

**One formatter** (`roles.progress_line`), used by `cmd_fetch` and the web fetch
thread, so a UI-triggered run reads identically in the terminal.

**Four duplicated state dicts** collapsed onto `CrawlJob._fresh_state`.

**UI**: source · company · progress bar · requests · elapsed · last-activity, with the
idle number turning red past 15 s (requests are spaced 3–5 s, so a longer gap is
genuinely wedged). `pollCrawl` now owns its interval, so a reload mid-fetch re-arms
polling instead of freezing the box for the rest of a three-hour run.

## What changed — Phase 2

`JOBSEARCH_DB` / `JOBSEARCH_CACHE` env overrides (`config.py`), so the corpus can live
outside a replaceable checkout. Defaults unchanged.

**A VPS was planned here and then rejected by the user on cost.** The systemd units
written for it were deleted. The hosting that was actually built — GitHub Actions
crawling, Vercel serving — is recorded in
[hosted-on-actions-and-vercel.md](hosted-on-actions-and-vercel.md).

## Verification

- `python -m pytest tests -q` → **254 passed**.
- New `tests/test_fetch_progress.py` (FILTER-006, journey CJ-053), 8 tests. The
  `_expand_ats` ones were watched fail first: `only {'alloy'} reported`.
- Offline end-to-end on the silent path — three companies that all 404: heartbeat
  climbed 0→5 requests while `pages` lagged 0→2, max observed idle 0.04 s. That is
  exactly the state that used to read as stuck.
- `validate_manifest` and `validate_context` clean.

`validate_context.py`'s `PATH_RE` matched `jobsearch/` inside absolute host paths
(`/opt/jobsearch/.venv`), so a deployment doc could not be written without false dead-path
failures. Added a `(?<!/)` lookbehind — narrowing, not weakening: a repo-relative path
reference is never preceded by a slash. Verified it still catches a genuinely dead repo
path and still honours the word boundary.

## Remaining

- **Phase 2 was superseded.** The VPS design was declined; see the hosted plan.
- No test drives the real UI — `pollCrawl` rendering is verified by reading, not
  running. The data it reads is covered.
- A daily all-roles × all-sources fetch is ~1,150+ requests/day to third parties,
  indefinitely, because `cache_ttl_hours` is 6. Inside the rate limit and inside the
  design, but a standing load rather than an occasional one. Narrowing the daily job to
  the roles actually applied to was raised with the user and is open.

## Done when

- [x] A running fetch reports what it is touching right now
- [x] A hung fetch is visibly distinguishable from a slow one
- [x] Terminal and UI show the same thing
- [x] Corpus can live outside the checkout
- [x] Daily-run artifacts written and reviewed
- [x] Daily-run design decided (superseded by the hosted plan)

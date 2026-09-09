---
status: completed
created: 2026-09-04
last_updated: 2026-09-09
areas: [jobsearch/src/ats.py, jobsearch/src/sources, jobsearch/src/roles.py]
---

# ATS board integrations, one provider at a time

## Objective

Integrate the four free ATS job-board APIs as company-expansion sources, one at a time,
reading each provider's docs and mapping it onto the platform's requirements.

## Approach (established by Greenhouse, ADR-010)

ATS boards are **company-scoped with no cross-board search**, so they expand companies
the role already touched rather than answering a role query. Each provider needs:

1. Live doc read + endpoint verification (never assume the shape).
2. A `src/sources/<provider>.py` with `CORE_VIEW_SQL` + `expand_company` + `board_url`
   + `parse`, matching the `CORE_COLUMNS` contract.
3. Registration in `sources.registry()` and `ats.IMPLEMENTED`, plus
   `roles.ATS_PROVIDERS`.
4. Tests against the live-verified shape, verified failable.
5. An ADR note if the provider differs materially from Greenhouse.

## Provider status

| Provider | Companies in corpus | Status |
|---|---|---|
| **Greenhouse** | 186 | ✅ **done** — 47/55 resolved on the AI role, 48 jobs |
| **Ashby** | **230** | ✅ **done** — 73/88 resolved on the AI role, 58 jobs |
| **Workable** | 19 | ✅ **done** — 3/4 resolved on the ML role, 1 job kept from 15 |
| **Lever** | 7 | ✅ **done** — 3/5 attempted resolved, 3 jobs on the SWE role |
| Dover | 2 | not planned (tiny; no free board API verified) |

## Log

### 2026-09-04 — Ashby
- Read the official lightweight Job Postings API documentation first. It documents
  `api.ashbyhq.com/posting-api/job-board/{name}`, one whole-board response, optional
  compensation, and the board name as the final hosted-board path component.
- Found a conduct blocker before using it: `api.ashbyhq.com/robots.txt` returns HTTP
  401. The repository refuses to crawl a host whose robots file is unreadable, so the
  documented API is not used and the guard was not weakened.
- Verified the compliant alternative live: `jobs.ashbyhq.com/{board}` and individual
  posting paths are robots-allowed. The board embeds the full current posting index in
  `window.__appData`; matched posting pages embed the detail plus JobPosting JSON-LD.
  Invalid board names return a silent HTTP 200 with `jobBoard: null`, now guarded as
  `SchemaDrift` during resolution.
- The two-tier fetch filters on title/team before requesting details. JSON-LD provides
  `datePosted` and structured salary bounds, currency, and period; numeric salary is
  emitted only for `YEAR`. The detail object provides description, locations,
  workplace type, employment type, and teams.
- Resolution sample: **17/20 = 85%**. Full AI-role run: **73/88 = 83.0%** companies
  resolved; **58 kept / 1,354 scanned** (57 new because two Wellfound aliases resolve
  to the same Marble board), 1,138 off-role, 158 too old, **0 mitigations**. Ashby
  contributed 57 distinct jobs versus Greenhouse's 48.
- Ran: `python -m pytest tests -q --basetemp=.tmp/pytest-final -p no:cacheprovider`
  → **133 passed**. Verified the non-annual salary guard fails when removed, then
  restored it.

### 2026-09-04 — Greenhouse
- Read the docs: **"All endpoints require a specific `board_token` — there is no
  cross-board search capability."** GET needs no auth; no rate limit published.
- Verified live against `singlestore`: whole board in one request, `meta.total`, no
  pagination.
- Measured resolution: Wellfound slug verbatim **45%** (9/20); with candidate
  generation **85%** (47/55 in the live run).
- Live run, AI role: 48 jobs kept from 2,336 scanned — 634 off-role, 1,654 too old.
  **0 mitigations.** The age cutoff does most of the filtering; ATS boards keep old
  postings.
- Found: no salary (`pay_input_ranges` null even on the single-job endpoint);
  entity-encoded HTML content; messy `location.name` versus structured
  `offices[].location`.
- Fixed a gap found during verification: expanded jobs recorded no provenance, so they
  were invisible to the `found_via_roles` facet. Wired `role` through and backfilled the
  48 existing rows.
- Ran: `python -m pytest tests -q` → **126 passed**. Verified the new guards fail when
  broken (content decoding, disambiguator stripping), then restored. One test initially
  passed for the wrong reason and was strengthened.

## Remaining

- **Workable next** — 19 companies, then Lever (7).
- `GAP-016` (pre-ADR-009 unfiltered RemoteOK rows) and `GAP-014` (cross-currency salary)
  remain open and are unaffected by this work.

## Done when

Each provider has a verified source module, tests, and a measured resolution rate
recorded here.

### 2026-09-05 — Workable

- Verified the endpoint live before writing anything:
  `apply.workable.com/api/v1/widget/accounts/{token}`, keyless, one request per board.
  My first probe of `?details=true` 404'd and I nearly recorded "no descriptions
  available" — it had been tested against a token that does not exist. Retested against
  a real board: it works, and returns **full descriptions inline**. So descriptions cost
  zero extra requests here, unlike Ashby's per-posting fetch.
- **Conduct check first.** `apply.workable.com` publishes zero robots rules;
  `www.workable.com` **disallows `/j/`** — the same path shape on a different host. Only
  `apply.` is used, and a test asserts no URL the module builds names `www.`.
- Negative control is an honest **404**, so no `BoardNotFound` equivalent was needed.
  An empty board is a 200 with the correct account name — a real resolution, not a miss.
- **Two bugs the live run found, both fixed with a failing test first:**
  1. Workable emits **one array entry per LOCATION**, all sharing a `shortcode`.
     `lawnstarter` returns 55 entries for **10 distinct postings**. Uncollapsed, every
     count inflated 5.5x and all but the last location was discarded. `collapse()` merges
     them; the live run's `got=` dropped from 55 to 10, which is the true number.
  2. `published_on` is a bare `YYYY-MM-DD`. `datetime.fromisoformat` returned a naive
     datetime read in local time (the view uses `strftime`, which is UTC), and midnight
     made a posting look up to 24 h older than it is. Both fixed in `posted_ts_of()`,
     with `CORE_VIEW_SQL` applying the identical end-of-day rule. This recovered Sweep's
     *Machine Learning Engineer* — the single most relevant posting on any board in the
     run, dropped as "too old" by ~100 minutes.
- **New guard, all providers:** `ats.resolve` binds the FIRST candidate token that
  answers 200, so a token collision would silently attach another company's board to
  ours. Workable returns the account name, so an optional `verify_account` hook now
  rejects a mismatch. Greenhouse and Ashby do not implement it and are unaffected.
- Live: **3/4 companies resolved** (`intuition-machines-1` → `intuition-machines` via
  the existing `-N` stripping), 15 postings scanned, **1 kept**, 9 off-role, 5 too old,
  **0 mitigations**, 9 requests. Small sample — only 4 corpus companies declare Workable
  for this role.
- Also in this change (not Workable): the disk cache never expired, so a second
  `fetch` replayed pages from days earlier and found nothing new. See ADR-011.
  Greenhouse/Ashby/Workable enabled by default; Himalayas budget 25 → 250 pages.
- Ran: `python -m pytest tests -q` → **173 passed**.

## Remaining

- **Lever** (7 companies) — next.
- Dover (2) — not planned.
- Workable's sample is small (4 companies for this role). Re-measure resolution rate
  after a role that touches more of the 19.

### 2026-09-06 — Lever (the last of the four)

- Conduct first. **Three origins**: `api.lever.co` (allows `/`, and the keyless postings
  API we use), `jobs.lever.co` (allows `/`, hosted pages we only link to), and
  `www.lever.co` which **disallows `/api/`**. Third instance of the same trap Ashby and
  Workable set. Only `api.lever.co` is touched, and a test asserts no URL the module
  builds names `www.`.
- **Lever is the only provider that publishes a rate limit: 2 req/s.** We run at 3-5 s,
  ~an order of magnitude under. Recorded in the module docstring so nobody later treats
  the documented ceiling as a target.
- **Bug found by the tests before it ever ran live:** Lever's wire format is a bare JSON
  array, but every other source's `parse` returns an object carrying `jobs`, and
  `ats.resolve` counts `data["jobs"]`. Returning the raw array made `resolve` die with
  `AttributeError: 'list' object has no attribute 'get'` — which would have taken down
  every company in the run, not just Lever. `parse` now adapts to the registry shape.
  Regression-tested (`test_resolve_counts_jobs_without_tripping_on_the_list_shape`).
- **Three shape traps, all verified live and all tested:**
  1. `createdAt` is epoch **milliseconds**; as seconds every posting is permanently fresh.
  2. The 404 body is valid JSON (`{"ok":false,"error":"Document not found"}`), so `parse`
     requires a *list* — a JSON-only check would read an error as a board.
  3. `workplaceType` is `onsite` live but `on-site` in the docs; both map to `Onsite` or
     the UI facet splits in two.
- **Salary is the best of any source**: structured AND states its interval
  (`per-year-salary`), so numeric values are emitted with no guessing. Rare (1/18).
- **Yield will be low, and honestly so.** `createdAt` is a requisition creation date, not
  a publish date. Corpus boards: gridline 0/4 fresh, metabase 0/18 (median age 319 d),
  odin-dynamics 5/6, sambatv 10/59, zaimler 2/13. The 30-day cutoff does most of the
  filtering, as on Ashby.
- Resolution on a corpus probe: **5/7**. `zaimler-ai` → `zaimler` and `1-gridline` →
  `gridline` both via the existing candidate generation; `bolt` and `instrumentl` have
  no Lever board.
- Ran: `python -m pytest tests -q` → **240 passed**. Both validators clean.
- Mutation-verified: ms-as-seconds, both shape checks removed, and a non-annual salary
  emitted as numeric each redden exactly the right tests.

### Live run, 2026-09-06

    lever  0 kept from 1/1 companies resolved  (machine-learning-engineer)
    lever  3 kept from 2/4 companies resolved  (software-engineer, 3 new, 7 off-role, 53 too old)

Only `odin-dynamics` is linked to the ML role, and its 6 postings were off-role or stale —
a correct but thin result, so the pipeline was re-verified on `software-engineer`, which
touches four Lever companies. Stored rows confirm the whole mapping on a real payload:
millisecond `createdAt` -> `2026-08-11 (25d)`, `hybrid` -> `Onsite or Remote`, badges
deduped, descriptions 4.0-6.7k chars, and **EUR 25,000 -> $27,000** via ADR-012's sort key,
which is the first evidence of the currency work and a new source composing. 0 mitigations.

`bolt` and `instrumentl` are genuine misses (no Lever board). Cumulative corpus:
wellfound 3108, himalayas 578, ashby 68, greenhouse 60, remoteok 23, lever 3, workable 1.

**Open, minor:** the Workable expansion reported `seen=2` where one row was stored. No
data is missing (1 job_raw row, 1 provenance row, nothing absent from the `jobs` view), so
this is a telemetry over-count, most likely two company slugs resolving to one board as
Ashby's `marble` already does. Not reproducible from post-hoc state; recorded rather than
guessed at.

## Remaining

**The ATS sweep is complete** — all four providers implemented. Dover (2 companies) stays
unplanned: too small to justify a fifth integration.

---
status: active
created: 2026-09-04
last_updated: 2026-09-05
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
| Lever | 7 | next |
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

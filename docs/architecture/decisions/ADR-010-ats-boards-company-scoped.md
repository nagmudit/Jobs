# ADR-010: ATS boards expand companies; they cannot be searched by role

**Status:** accepted · **Date:** 2026-09-04 · **Updated: 2026-09-06**
**Implemented: Greenhouse, Ashby, Workable, Lever — all four**

## Context

Greenhouse, Lever, Ashby and Workable all publish free, keyless job-board APIs. The
Phase 1 probe used them as a fallback data source and measured a 31.6% company
resolution rate (`wellfound-probe/REPORT.md` §2).

Integrating them into the role-driven `fetch` workflow (ADR-009) runs into a hard
constraint, confirmed in Greenhouse's own documentation:

> **"All endpoints require a specific `board_token` — there is no cross-board search
> capability."**

Greenhouse GET endpoints need no authentication; only application submission does. No
rate limit is published. Ashby's official lightweight Job Postings API is likewise
company-scoped, with the jobs-page name in the URL path.

## The mismatch

Every other source answers *"what jobs match this role"*. An ATS board answers *"what is
on this company's board"*. It cannot be role-searched at all.

## Decision

**ATS boards expand companies, they do not search.** `fetch_role` handles them with
`filter_mode: "expand"`:

1. Find companies **already in the corpus** that the role touched *and* that declare this
   provider via Wellfound's `atsSource` field.
2. Resolve each to a board token (`src/ats.py`), caching hits **and misses**.
3. Pull the whole board index — one request, no pagination.
4. Apply the role keywords locally, and the age cutoff. If the provider needs posting
   details, fetch them only after the local role filter.

Bounded by the corpus, never by the provider: the AI role touches 55 Greenhouse and 88
Ashby companies, not all 2,002 companies.

### Ashby's compliant data surface

Ashby materially differs from Greenhouse. Its documented lightweight endpoint is
`api.ashbyhq.com/posting-api/job-board/{name}`, but that origin's `robots.txt` returned
HTTP 401 on 2026-09-04. The repository's rule is explicit: an unreadable robots file
blocks the host. The implementation does not special-case or bypass it.

The separate `jobs.ashbyhq.com` origin has a readable robots file. It disallows its
internal `/api/` but allows the hosted board and posting paths used here. The hosted
board embeds all current posting summaries in `window.__appData`; a matched posting
page embeds full detail plus JobPosting JSON-LD. Ashby therefore uses one allowed board
request, filters by title/team, then requests details only for matches.

Invalid Ashby board names return HTTP 200 with `jobBoard: null`. Resolution treats that
shape as `SchemaDrift` for the candidate and continues; accepting status alone would
silently resolve nonexistent boards.

## Why this is worth doing

Wellfound shows at most **3 highlighted jobs per company**, and 264 companies in the
corpus sit exactly at that cap. Their real boards are far larger — measured on a
20-company sample: **Astranis 81, Alpaca 59, Bitwarden 37, Alloy 25**, all appearing on
Wellfound as 3 or fewer. Expansion is how that cap is defeated for companies we already
care about.

## Resolution

Wellfound's `atsSource` names the provider, which removes the guessing that dominated
the Phase 1 probe (345 requests for 38 companies). Only the token has to be found.

| provider / approach | rate |
|---|---|
| Greenhouse, Wellfound slug verbatim | **9/20 = 45%** |
| Greenhouse, candidate generation | **47/55 = 85.5%** |
| Ashby, candidate generation sample | **17/20 = 85%** |
| Ashby, full AI-role run | **73/88 = 83.0%** |
| Ashby, cumulative across runs | **98/116 = 84.5%** |
| Workable, ML-role run | **3/4 = 75%** (small sample) |
| Lever, corpus probe | **5/7 = 71%** (small sample) |

The candidates come from measured failure modes, not invention:

    alloy-2, assemblyai-1, 10a-labs-1   Wellfound's own disambiguating -N suffix
    afresh-technologies                 company is just "Afresh"
    arize-ai                            "Arize AI" -> arizeai
    aavaz  (company "Enterpret")        slug and name have diverged; unresolvable

Misses are cached. Re-probing six candidates for a company that has no board is pure
cost against a third party.

## Field mapping and its limits

### Greenhouse

* **No salary.** `pay_input_ranges` is absent from the list endpoint and was `null` on
  the single-job endpoint too. Emitted as NULL, never guessed.
* **`content` is entity-encoded HTML** (`&lt;p&gt;`) — decoded once at ingest rather
  than on every query. A deliberate, documented exception to raw-first (ADR-002),
  matching the `_badges` precedent in `parse.py`. The original is always re-fetchable.
* **`location.name` is messy free text** ("United States ", "United States & EMEA");
  `offices[].location` is preferred where present.
* **No remote flag.** Inferred only from an explicit "remote" in the location; otherwise
  NULL rather than a guessed "onsite".
* **`first_published` is ISO8601 with a UTC offset**, which SQLite's `strftime('%s',…)`
  parses correctly — verified, so no injected epoch column.
* **Job ids are namespaced by board** (`singlestore/8029118`). They look globally unique
  but nothing documents that, and a collision would silently merge two companies' jobs.

Expanded jobs are stored under the **same `company_slug`** as their Wellfound rows, so a
company's jobs stay together across sources.

### Ashby

* **Two tiers.** The board index supplies ids, titles, teams, locations and workplace
  types. Posting details are fetched only after the role filter and supply the full
  description plus JobPosting JSON-LD.
* **Structured salary.** JSON-LD `baseSalary` supplies bounds, currency and `unitText`.
  Numeric values are emitted only when `unitText` is `YEAR`; other periods retain the
  raw string but are never mixed into annual sorting.
* **Published age.** JSON-LD `datePosted` is used for the 30-day cutoff. Missing or
  unparseable dates are kept because unknown age is not old (ADR-006).
* **Remote is explicit.** `workplaceType` maps `OnSite`, `Remote`, and `Hybrid` to the
  canonical `Onsite`, `Remote`, and `Onsite or Remote` labels.
* **Description and categories.** `descriptionHtml` is stripped once at ingest; team
  and department names are retained as badges and are eligible for local role matching.
* **Job ids are namespaced by board** (`ashby/<uuid>`), matching the Greenhouse collision
  defense.

### Workable

Probed live 2026-09-05. `apply.workable.com/api/v1/widget/accounts/{token}`, keyless,
no stated rate limit. It is the **cheapest and richest** of the three.

* **Two origins, and only one may be touched.** `apply.workable.com` publishes a
  robots.txt with **zero rules**; `www.workable.com` **disallows `/j/`** — the same path
  shape on a different host. The same trap Ashby set, and the reason `Fetcher` keys
  robots per origin (ADR-007). `src/sources/workable.py` builds everything from a single
  `BASE` on `apply.` and a test asserts no URL it produces names `www.`.
* **`?details=true` returns full descriptions inline**, so unlike Ashby there is no
  per-posting detail fetch: 55 entries / 635 KB for `lawnstarter`, description non-empty
  on 55/55. **One request per company, descriptions included.**
* **A missing board is an honest 404**, so Workable needs no `BoardNotFound` equivalent.
  An empty board is a 200 carrying the correct account name — a real resolution, not a
  miss, and reading it as a miss would re-probe six candidates forever.
* **One array entry PER LOCATION**, all sharing a `shortcode`. `lawnstarter` returns 55
  entries for **10 distinct postings**. `collapse()` merges them, or every telemetry
  number inflates 5.5x and all but the last location is discarded.
* **`published_on` is a bare `YYYY-MM-DD`.** Two traps, both hit live and both fixed in
  `posted_ts_of()`: `datetime.fromisoformat` returns a naive datetime whose
  `.timestamp()` is read in the machine's local zone (the view uses `strftime`, which is
  UTC), and an unknown time of day read as midnight makes a posting look up to 24 h
  older than it is. Unknown age is not old (ADR-006), so it resolves to end-of-day. The
  `CORE_VIEW_SQL` applies the identical rule — if ingest and the view disagreed, a job
  would be stored and then immediately hidden by the UI's age filter.
* **`telecommuting` is a real boolean**, so `remote_label` can honestly say `Onsite`
  where Greenhouse must leave it NULL.
* **No salary of any kind**, so those columns are NULL rather than guessed.
* **`description` is plain HTML**, not entity-encoded — a single strip, the opposite of
  Greenhouse's unescape-then-strip.

### Lever

Probed live 2026-09-06 against the official `lever/postings-api` docs.
`api.lever.co/v0/postings/{site}?mode=json`, keyless, whole board in one request
with descriptions inline.

* **Three origins, one usable.** `api.lever.co` and `jobs.lever.co` both allow `/`;
  **`www.lever.co` disallows `/api/`**. The third instance of the same trap Ashby and
  Workable set, and the reason `Fetcher` keys robots per origin (ADR-007).
* **The first provider that publishes a rate limit: 2 requests/second.** Our mandated
  3-5 s spacing is about an order of magnitude under it. Recorded so nobody later
  "optimises" toward the documented ceiling and away from this repo's stricter rule.
* **Success is a JSON array; an error is a JSON object.** `{"ok":false,"error":"Document
  not found"}` with HTTP 404. Both are valid JSON, so `parse` requires a list —
  validating only "is it JSON" would read an error body as a board.
* **`parse` returns `{"jobs": [...]}`, not the raw array.** Every other source's `parse`
  returns an object carrying `jobs`, and `ats.resolve` counts `data["jobs"]` for all of
  them. Returning Lever's wire array made `resolve` die with `AttributeError: 'list'
  object has no attribute 'get'` and took the whole run down. Adapting in the source
  module keeps the resolver free of per-provider special cases. Regression-tested.
* **`createdAt` is epoch MILLISECONDS.** Read as seconds it lands ~50,000 years out and
  every posting is permanently fresh. Converted once at ingest; the view must not divide
  again.
* **`createdAt` is a creation date, not a publish date.** Metabase's 18 postings span
  2020-05 to 2026-07, median age 319 days, **nothing** inside the 30-day cutoff. Those
  requisitions really are old, so the cutoff does most of the filtering — as on Ashby.
  Across the corpus boards: gridline 0/4 fresh, metabase 0/18, odin-dynamics 5/6,
  sambatv 10/59, zaimler 2/13.
* **Salary is structured AND states its interval** (`per-year-salary`), so unlike
  Greenhouse there is no guessing and unlike Himalayas no risk of an hourly figure
  landing in an annual column. Rare: 1 of 18 on metabase.
* **`descriptionPlain` is already plain text** — the only source where it is. The full
  posting is that plus the `lists` blocks (whose `content` IS HTML) plus
  `additionalPlain`.
* **`workplaceType` is explicit.** The docs spell it `on-site`; live data sends `onsite`.
  Both map to the canonical `Onsite`, or the UI facet splits in half.

### Account-name verification

`ats.resolve` binds the **first** candidate token that answers 200, so a token collision
would silently attach another company's whole board to ours. Workable is the only
provider that returns the account name, so it is the only one that can rule this out.
The optional `verify_account(data, company_name)` hook is absent on Greenhouse and Ashby,
which are unaffected. It accepts when there is nothing to compare: the guard exists to
catch a wrong match, not to reject an unverifiable one.

## Alternatives

- **A separate `boards` bulk command** walking all 186 Greenhouse-hinted companies.
  Deferred: role-scoped expansion is bounded and fits the existing workflow.
- **Resolve companies with no `atsSource` hint** by trying all four providers. That is
  what cost the probe 9.1 requests per company for a 31.6% rate. Not worth it while the
  hinted subset is unexhausted.

## Consequences

- Greenhouse jobs carry no salary. Ashby exposes structured compensation where an
  employer publishes it; 31 of 57 distinct jobs in the first live run had annual bounds.
- Boards keep old postings, so the age cutoff does most of the filtering: one 288-job
  board contributed 6 jobs after cutoff and role filtering.
- `company_ats` is a new table. Cached misses mean a genuinely new board is not
  discovered until a deliberate `refresh`.
- **All four providers now implemented**, so the ATS sweep is complete. Dover (2
  companies) remains unplanned: too small to justify a fifth integration.
- **Cost per job differs sharply.** Greenhouse ~2.9 requests/job and Workable ~1
  request per *company* (descriptions inline); Ashby ~10 requests/job, because its board
  summary carries no date and forces a fetch per posting. All three are worth having;
  only Ashby is expensive.
- Ashby (230 companies) outnumbers Greenhouse (186) in this corpus. Its first AI-role
  run retained 58 postings from 1,354 scanned (57 distinct after two Wellfound aliases
  resolved to one board), versus Greenhouse's 48 distinct jobs.

## Related

`src/sources/greenhouse.py` · `src/sources/ashby.py` · `src/ats.py` ·
`src/sources/workable.py` · `src/sources/lever.py` · `src/roles.py::_expand_ats` ·
`jobsearch/tests/test_greenhouse.py` · `jobsearch/tests/test_ashby.py` ·
`jobsearch/tests/test_workable.py` · `jobsearch/tests/test_lever.py` · ADR-002 · ADR-006 · ADR-007 · ADR-009 · ADR-011

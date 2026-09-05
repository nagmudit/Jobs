---
status: current
last_verified: 2026-09-05
applies_to: [jobsearch/src/fetch.py]
---

# Job platform survey — what else can be read the way Wellfound is

Probed 2026-09-04 with the repo's own `Fetcher`: robots.txt first, one request per
host, 3–5 s spacing, honest UA, no evasion. Same method as `wellfound-probe/`.

This is a **snapshot of third-party sites**, not a contract. Re-verify before building
on any row.

## Verified working

| Platform | Endpoint | Status | Shape | Volume |
|---|---|---|---|---|
| **RemoteOK** | `remoteok.com/api` | 200 | pure JSON, 427 KB | 101 latest |
| **Himalayas** | `himalayas.app/jobs/api` | 200 | pure JSON, cursor + offset paging | **101,235** `totalCount` |
| **We Work Remotely** | `weworkremotely.com/remote-jobs.rss` | 200 | RSS | 93 latest |
| **Greenhouse / Lever / Ashby / Workable** | keyless board APIs | 200 | pure JSON | per company |

### Fields

**RemoteOK** — `id, position, company, location, description, tags, date, epoch,
salary_min, salary_max, url, apply_url, company_logo, slug`.
Salary already numeric, so `derive.salary_bounds` is not needed.

**Himalayas** — `title, companyName, companySlug, description, excerpt,
employmentType, minSalary, maxSalary, currency, salaryPeriod, seniority,
locationRestrictions, timezoneRestrictions, categories, parentCategories, pubDate,
expiryDate, applicationLink, guid`.

**We Work Remotely** — `title, region, country, state, skills, category, type,
description, pubDate, expires_at, guid, link`.

> **Both Himalayas and WWR carry an explicit expiry date.** Wellfound has no such
> field, which is why ADR-006 has to approximate liveness with a 30-day age heuristic.
> On these sources the real signal is available, and the heuristic should not be
> applied to them blindly.

## Verified — do not pursue

| Platform | Evidence | Reason |
|---|---|---|
| **Naukri** | `robots.txt` itself → **403** | Cannot read the rules. `Fetcher` raises "refusing to crawl blind". Correct, and non-negotiable. |
| **Instahyre** | `/search-jobs/` → 403 | Blocked |
| **Himalayas (HTML)** | `/jobs` → 403; robots disallows `/jobs?page=` | Use the API, which robots explicitly allows |
| **We Work Remotely (HTML)** | `/categories/...` → 403 | Use the RSS feed |
| **Cutshort** | robots disallows `/view/j/`, `/view/c/`, `/vj/` | Job detail pages are off-limits |
| **Work at a Startup (YC)** | `/jobs` → 200, 71 KB, no `__NEXT_DATA__`, no JSON-LD | Login-walled shell |
| **Built In** | `/jobs` → 200, 395 KB, no embedded JSON | Would need real HTML parsing — fragile, and 158 robots rules to honour |
| **LinkedIn / Indeed / Glassdoor** | not probed | Hostile to this approach and their terms are explicit. Out of scope for this repo. |

## Raw robots.txt survey

| Host | robots | rules | notable disallows |
|---|---|---|---|
| workatastartup.com | 200 | 0 | — |
| remoteok.com | 200 | 2 | — |
| weworkremotely.com | 200 | 8 | `/admin/`, `/account/`, `/job-seekers/*` |
| himalayas.app | 200 | 26 | `/apply`, `/jobs?page=`, `/jobs/*?page=` |
| builtin.com | 200 | 158 | `/core/`, `/profiles/` |
| instahyre.com | 200 | 0 | — |
| cutshort.io | 200 | 12 | `/view/j/`, `/view/c/`, `/vj/` |
| wellfound.com | 200 | 31 | `/_jobs/`, `/search`, `?role=` |
| naukri.com | **403** | — | unreadable |
| boards.greenhouse.io | 200 | 1 | `/embed/` |
| api.lever.co | 200 | 1 | — (Allow: /) |
| jobs.lever.co | 200 | 2 | — |
| www.lever.co | 200 | 5 | **`/api/`**, `/studio/` |
| jobs.ashbyhq.com | 200 | 3 | `/meeting/`, `/b/`, `/api/` |
| api.ashbyhq.com | **401** | — | unreadable |
| apply.workable.com | 200 | **0** | — (everything allowed) |
| www.workable.com | 200 | 4 | **`/j/`**, `/admin`, `/auth/google` |
| jobs.workable.com | 200 | 6 | `/search`, `/profile*` |

**Lever splits three ways.** `api.lever.co` (the keyless postings API this tool uses)
and `jobs.lever.co` (the hosted pages we only link to) both allow `/`, while
`www.lever.co` disallows `/api/`. Lever also publishes an explicit rate limit of
2 requests/second — the only provider that does. See ADR-010.

**Workable has the same two-origin split, in the opposite direction.**
`apply.workable.com` allows everything, while `www.workable.com` **disallows `/j/`** —
the identical path shape on a different host. The live tool reads only
`apply.workable.com/api/v1/widget/accounts/{token}`, which is keyless and returns the
whole board in one request; `?details=true` includes full descriptions inline
(verified 2026-09-05: 55 entries / 635 KB for `lawnstarter`). See ADR-010.

Ashby has two distinct origins. `jobs.ashbyhq.com` disallows its internal `/api/` but
allows public board and posting pages. The documented lightweight posting API lives on
`api.ashbyhq.com`; its robots file returned HTTP 401 on 2026-09-04, so the live tool
does not use that origin. It reads the allowed hosted pages instead (ADR-010).

## Why these are easier than Wellfound

Wellfound cost a full probe phase because the data is an undocumented internal Apollo
cache inside HTML, with three silent-failure modes (ADR-003). RemoteOK and Himalayas
return **documented JSON**:

- no `__NEXT_DATA__` to break on an App Router migration
- no `SilentRoleFallback` — there is no role-slug URL to get wrong
- no `PageWrap` — Himalayas pages by cursor, RemoteOK returns a fixed latest set

They need their own guards instead: a JSON **shape** check, and a `totalCount` sanity
check. Different failures, not fewer.

## Cost

Himalayas caps `limit` at 20 regardless of what is requested (asked for 100, got 20).
101,235 jobs ÷ 20 = **~5,060 requests ≈ 5.6 h** at the mandated 3–5 s. Full ingestion
is not sensible; the same slice-and-cap thinking as ADR-004 applies, filtered by
category or seniority.

**Not measured:** whether any of these rate-limit or mitigate under sustained load.
Availability and field shape were checked; durability was not. Assume nothing.

## Recommendation

1. **RemoteOK + Himalayas first.** Pure JSON, no parsing fragility, and Himalayas
   brings a genuine expiry field.
2. **Skip WWR initially** — 93 latest items is a freshness feed, not a corpus.
3. **ATS boards stay enrichment, not discovery** — company-driven, which is what
   `wellfound-probe/REPORT.md` §2 already concluded at a 31.6% resolution rate. There
   are 1,416 companies in the corpus to point them at.

See [ADR-007](../architecture/decisions/ADR-007-multi-source.md) for what the codebase
needs before any of this lands.

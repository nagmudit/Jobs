# Wellfound data acquisition — feasibility report

**Probe target:** role `artificial-intelligence-engineer`, location `bangalore`, first 2 pages.
**Run date:** 2026-09-02. **Conduct:** robots-enforced, 1 req / 3–5 s to Wellfound, concurrency 1,
honest UA with contact address, no anti-bot evasion of any kind.

> **Headline:** the premise that Wellfound is hard to read is wrong at this scale. A plain
> unauthenticated `httpx` GET returns HTTP 200 with the complete listing data embedded in
> `__NEXT_DATA__`. **P1 is the answer; P6 is not.** The ATS hit rate — the number this probe
> existed to produce — came in at **31.6%**, far too low to carry the architecture, though it
> is a genuinely valuable enrichment layer for the third of companies it does resolve.

---

## 1. Comparison matrix

| Probe | Works? | Fields (of 12) | Coverage vs. visible site | Fragility (1–5) | Setup effort | Ongoing cost | Maintenance burden |
|---|---|---|---|---|---|---|---|
| **P1** static HTTP + `__NEXT_DATA__` | ✅ **YES** | **11/12** (all but `equity_raw`) | **100%** — 83/83 visible jobs, 40/40 company slots (38 unique), exact match | **3** | Trivial (`httpx` + `json`) | **$0** | Low–medium |
| **P2** JSON-LD / structured data | ⚠️ **PARTIAL** | **12/12** when combined with detail HTML | Detail pages only — **cannot enumerate** listings | **2** | Trivial | **$0** | Low |
| **P3** RSS / Atom / JSON feeds | ❌ **NO** | 0/12 | None | — | — | — | — |
| **P4** Playwright + real session | ✅ YES (redundant) | 11/12 | 100% — *identical to P1* | **4** | Heavy (browser, profile, manual login) | $0 + RAM/CPU | **High** |
| **P5** Licensed third party | ✅ (paid) | ~10–12 claimed | Vendor-dependent | **4** | Low (API key) | **$1–$3 / 1,000 jobs** | Low, but vendor risk |
| **P6** ATS fallback | ⚠️ **PARTIAL** | 11/12 (no `equity_raw`) | **31.6%** of companies; 18.4% with live jobs | **1** | Moderate (`resolve_ats`) | **$0** | **Very low** |

Fragility, setup, cost and maintenance are my judgement calls; "works", "fields" and "coverage"
are measured and reproducible from `results/*.json` via `python -m src.matrix`.

### Per-field fill rates (measured)

| Field | P1 (n=83) | P2 JSON-LD (n=5) | P6 ATS (n=161) |
|---|---|---|---|
| `source`, `source_job_id`, `company`, `title`, `location_raw`, `description`, `posted_at`, `apply_url`, `fetched_at` | 100% | 100% | 100% |
| `remote` | 100% | 20% | 97.5% |
| `salary_raw` | **63.9%** | 60% | **5.6%** |
| `equity_raw` | **0%** | **80%** | **0%** |

`equity_raw` is the pivot field. It does **not** exist in the search-page Apollo cache and does
**not** exist in JSON-LD. It exists only as rendered text on the job detail page
(`$25k – $50k • 0.0% – 1.0%`). Equity is a Wellfound-specific concept — no ATS models it, which
is why P6 scores 0% and can never do better.

---

## 2. The ATS resolution hit rate (P6) — raw numbers

Of **38 distinct companies** surfaced across 2 pages of Wellfound results:

| Metric | Count | Rate |
|---|---|---|
| Companies seen on Wellfound | 38 | — |
| Resolved to a real ATS board (any confidence) | **13** | **34.2%** |
| Resolved with **high** confidence | **12** | **31.6%** |
| Resolved with medium confidence | 1 | 2.6% |
| **Resolved to a board with ≥1 live posting** | **7** | **18.4%** |
| Total jobs recovered from ATS boards | **161** | — |
| Total HTTP attempts to reach this | 345 | ~9.1 per company |

**By provider:** Greenhouse 5, Workable 5, Ashby 2, Lever 1.

### Showing the work — how this number was kept honest

The raw first-pass number was **44.7%**, and it was wrong in two directions. Both corrections
are in the code and reproducible.

**(a) Negative control — does a 200 mean anything?** Yes. All four providers return a clean 404
for a nonsense slug, so a 200 genuinely means the board exists:

```
slug zzqx-not-a-real-company-9931
  greenhouse  404 {"status":404,"error":"Job not found"}
  lever       404 {"ok":false,"error":"Document not found"}
  ashby       404 Not Found
  workable    404 Not Found
```

**(b) Rate limiting deflated it.** Workable returned **47 × HTTP 429** at a 0.4–0.9 s delay,
affecting 20 unresolved companies. 429 is not a miss. Added escalating backoff (4 s → 10 s →
20 s); Workable hits went 2 → 9. Greenhouse, Lever and Ashby never rate-limited.

**(c) Fuzzy name matching inflated it.** Scoring substring containment a flat 0.9 accepted
first-token slug guesses against unrelated boards:

| Company | Matched | Verdict |
|---|---|---|
| Smart Audit | Workable board named `smart`, 0 jobs | ❌ rejected |
| Riddhi Siddhi Career Point | Workable board named `Riddhi`, 0 jobs | ❌ rejected |
| in-tech | Workable board `Intech Hawaii`, 2 jobs | ❌ different company |
| Pilot (pilotplans.com) | Workable board `Pilot`, 0 jobs | ❌ too generic |
| Moshi Moshi | Workable `Moshi Moshi Media` | ✅ kept (medium) |

Containment is now scored by length ratio (`smart`/`smartaudit` = 0.50 → rejected;
`moshimoshi`/`moshimoshimedia` = 0.67 → medium). That moved 44.7% → **34.2% / 31.6%**.

**(d) Cross-check against Wellfound's own ground truth.** The Apollo cache exposes an
`atsSource` field per job (`AtsIntegration::Greenhouse::Listing`). 8 of 38 companies declared
one; the resolver independently confirmed **7 of those 8** (87.5% agreement). The miss was
**BlueVine** — declares Greenhouse, but `boards-api.greenhouse.io/.../bluevine` now 404s, i.e.
the board moved or closed. That is a real staleness signal, not a resolver bug.

### Why the hit rate is this low

Only **8 of 38 (21%)** companies declare any ATS integration to Wellfound. The rest are small
Indian startups posting **natively on Wellfound** — they have no Greenhouse/Lever/Ashby/Workable
board at all, because Wellfound *is* their ATS. No amount of slug-guessing fixes that. The
ceiling here is a property of the market segment, not of the resolver.

Skew matters too: the 7 companies with live ATS boards are the *larger, better-funded* ones
(SingleStore 33, LogicMonitor 52, EarnIn 30, Cyberhaven 21, Prodigal 12). An ATS-only pipeline
would silently bias your job search toward big companies and drop the early-stage startups that
are arguably the whole point of using Wellfound.

---

## 3. Verbatim evidence

### P1 — static HTTP succeeds (no login, no JS, no evasion)

```
https://wellfound.com/role/l/artificial-intelligence-engineer/bangalore -> 200 (531694 bytes, 431ms)
  role_applied=True page_returned=1 jobs=41 companies=20 total=596
https://wellfound.com/role/l/artificial-intelligence-engineer/bangalore?page=2 -> 200 (531212 bytes, 2143ms)
  role_applied=True page_returned=2 jobs=42 companies=20 total=604
```

Verified against the rendered HTML: 41 job links visible / 41 extracted on page 1, 42/42 on page 2 — zero missed, zero phantom. Data lives at `props.pageProps.apolloState.data`, a normalized Apollo cache:

```
ROOT_QUERY.talent.seoLandingPageJobSearchResults({"location":"bangalore","page":1,"role":"artificial-intelligence-engineer"})
  -> {totalJobCount: 596, totalStartupCount: 514, perPage: 20, pageCount: 26, startups: [20 refs]}
StartupResult:40574        -> {name, slug, companySize, highConcept, badges, highlightedJobListings}
JobListingSearchResult:... -> {title, description, compensation, locationNames, remote, remoteConfig,
                               liveStartAt, slug, jobType, atsSource, primaryRoleTitle}
```

**Durability burst — 20 consecutive requests, 3–5 s apart, 80.8 s total:**

```
statuses      Counter({200: 20})     # zero 403, zero 429, zero challenges
cf_mitigated  Counter({'None': 20})  # Cloudflare never mitigated a single request
next_data     all True
```

No degradation, no interstitial, no rate limiting. Cloudflare is present (`server: cloudflare`,
`cf-ray` on every response) but took no action against polite, low-volume, honestly-identified
traffic.

### P2 — JSON-LD present on detail pages, absent from search pages

```
search page 200: 0 JSON-LD blocks    types=[]
detail 200 ld_blocks=1 jobposting=1  types=['JobPosting']   (5 of 5 detail pages)
```

`JobPosting` keys available: `applicantLocationRequirements, baseSalary, datePosted, description,
directApply, employmentType, experienceRequirements, hiringOrganization, identifier, image,
industry, jobBenefits, jobLocation, jobLocationType, title`.

⚠️ **`identifier` is the company name, not a job ID.** Taking it verbatim silently produces wrong
IDs; the probe derives the ID from the URL instead.

### P2 — sitemaps are a dead end

```
https://wellfound.com/sitemap.xml.gz  -> 200, sitemapindex, 1 loc  -> /basics.xml.gz
https://wellfound.com/basics.xml.gz   -> 200, 86 locs
   families: {hire: 61, browse: 7, recruit: 6, candidates: 3, discover: 3, about: 1,
              job-collections: 1, jobs: 1, remote: 1, web3: 1, blog: 1}
```

**Zero job URLs, zero company URLs** — 86 marketing pages. The plain `/sitemap.xml` the brief
assumed returns **403**, and it is an S3 error, not a bot block:

```
HTTP 403  content-type: application/xml  server: cloudflare
<?xml version="1.0" encoding="UTF-8"?>
<Error><Code>AccessDenied</Code><Message>Access Denied</Message>
<RequestId>GWSJQ0MEZB0PFQTY</RequestId>...</Error>
```

### P3 — no feeds exist

```
403 /jobs.rss      403 /jobs.atom    403 /jobs.json    404 /feed
403 /feed.xml      404 /rss          403 /rss.xml      403 /atom.xml
404 /jobs/feed     404 /location/bangalore.rss
200 /role/l/artificial-intelligence-engineer/bangalore.rss   <- SPA shell, text/html, not a feed
```

`<link rel="alternate">` feeds declared in page heads: **0**.

⚠️ Cloudflare serves its 403 bodies as `application/xml` beginning with `<?xml`, which trips a
naive feed sniff — an early version of this probe reported 3 false "feeds". A non-2xx is never
a feed.

### P4 — real browser works, and adds nothing

```
profile=EMPTY (cold, logged-out) headless=True
nav1: status=200 bytes=535824 challenge=- next_data=True jobs=41 logged_in=False
nav2: status=200 bytes=534319 challenge=- next_data=True jobs=42 logged_in=False
```

Same 83 jobs, same 11/12 fields, no challenge served even logged-out.

**Not tested:** the logged-in variant. This session was non-interactive so the one-time manual
login could not be performed. `python -m src.run --probe p4 --login` is implemented and ready.
Given a cold browser already recovers 100% of what P1 does, I'd expect login to add little for
*search* data — its plausible value is applicant-only fields (application status, saved jobs,
recruiter-activity signals), none of which are in your 12-field schema.

---

## 4. P5 — Licensed third parties (research only, not implemented)

### Apify actors

| Actor | Price / 1,000 | Status | Users | Fields | Notes |
|---|---|---|---|---|---|
| [crawlerbros/wellfound-scraper](https://apify.com/crawlerbros/wellfound-scraper/issues) | **$1.00** | Live, modified 4 mo ago | **1.7K** (285 MAU) | id, title, compensation, remote, location, company, logo — **no description** | HTTP-only, no login. Field set ≈ exactly the Apollo `JobListingSearchResult` minus description |
| [jobsapi/wellfound-jobs-search-scraper](https://apify.com/jobsapi/wellfound-jobs-search-scraper/api) | **$2.99** | Live, 100% run success | 8 (1 MAU) | id, url, title, company, date, HTML+text description, salary, equity, funding stage, remote, experience | Self-describes as "HTML/JSON-LD parsing, no browser automation, no login or proxies" — **i.e. P1+P2** |
| [scrapesage/wellfound-scraper](https://apify.com/scrapesage/wellfound-scraper/api) | **$3.00** | Live, 100% run success | 2 (1 MAU) | 20+ incl. parsed salary min/max/currency and equity % range | Claims Wellfound "blocks a share of requests at random" (5–6 of 8) and that **residential proxy is required**. Also reports equity present in ~19% of postings, salary ~90% |
| [cryptosignals/wellfound-jobs-scraper](https://apify.com/cryptosignals/wellfound-jobs-scraper) | listed $0.05 | **404 — gone** | — | — | Delisted between indexing and this probe |
| arlusm/wellfound-scraper, jason_1bps/angellist-scraper | — | **DEPRECATED** | — | — | Explicitly marked deprecated |

### Commercial job-data APIs

| Provider | Price | Wellfound coverage | Notes |
|---|---|---|---|
| **Coresignal** | $49/mo entry; ~$800/mo for 10k jobs ≈ **$0.08/job** | **Named** — Wellfound listed alongside LinkedIn/Indeed/Glassdoor in a 399M+ record multi-source jobs dataset | Only provider that explicitly claims Wellfound |
| **TheirStack** | Free → $49/mo | Not named | 237M+ postings, 356k+ sources, 16k+ ATS platforms |
| **Bright Data** | $1.50–$2.50 / 1K records; datasets from $250, single-source can reach ~$23K | Not named | Source-specific datasets are LinkedIn/Indeed/Glassdoor |
| **Hirebase** | Free → $99/mo; $249/mo for 250k | Not named | 4.2M listings, 300k career pages, 80+ ATS |
| **Oxylabs** | $49/mo; datasets $1,000/mo | Not named | |
| **Proxycurl** | **Shut down July 2025** after legal action | — | Cautionary: single-vendor dependency is a real risk |

**Read:** six of seven mainstream job-data APIs don't name Wellfound at all. Paying $1–3 per
1,000 for an Apify actor buys you a wrapper around the same public `__NEXT_DATA__` you can read
for free — one of them says so outright. The one actor claiming residential proxies are
mandatory contradicts our 20/20 clean result, which is explained by volume: they scrape hard and
get mitigated; we don't and aren't.

**Recommendation: don't buy anything.** Revisit only if you need historical / multi-site
aggregation, where Coresignal is the only credible option.

---

## 5. Three silent-failure traps (these will bite the real tool)

These are the most operationally important findings, because all three return **HTTP 200 with
plausible data** while being wrong.

**1. Invalid role slug → silent unfiltered fallback.** `/role/l/{role}/{location}` does not 404
on a bad slug. It serves a location-wide search.

```
artificial-intelligence           -> page=/seoLanding/locationSearch
                                     args={"location":"bangalore","page":1}        1150 jobs  ← role IGNORED
artificial-intelligence-engineer  -> page=/seoLanding/roleLocationSearch
                                     args={...,"role":"artificial-intelligence-engineer"}  596 jobs  ← correct
```

I hit this on the first run and nearly reported "1,150 AI jobs in Bangalore". Detection: read the
GraphQL cache key — the server's own record of what it filtered on — and assert `"role"` is in
it. `SilentRoleFallback` is raised otherwise.

**2. Pagination silently wraps to page 1.** Past the last real page, the server returns page 1
rather than an empty result or a 404:

```
page 13 -> page_returned=13, 26 jobs
page 14 -> page_returned=14, 21 jobs
page 15 -> page_returned=15,  2 jobs   <- genuine end of results
page 16 -> page_returned=1,  41 jobs   <- SILENT WRAP
page 17..20 -> page_returned=1, 41 jobs, byte-identical
```

A naive `for page in range(1, 27)` loop — and `pageCount` *claims* 26 — would re-ingest page 1
five-plus times and inflate your corpus with duplicates. Always assert `page_returned == requested`.

**3. `pageCount` overstates reachable results.** The API reports `pageCount: 26` /
`totalJobCount: 596`, but results actually exhaust at page 15 (~465 jobs, **~78%**). The search
pages surface at most 3 `highlightedJobListings` per company; the remaining ~22% of matching jobs
are only reachable via per-company pages.

---

## 6. Recommendation

### Primary: **P1 (static HTTP + `__NEXT_DATA__`), enriched by P2 on demand**

Build the real tool on a plain `httpx` GET of `/role/l/{role}/{location}?page=N`, parsing
`props.pageProps.apolloState.data`.

- **11/12 fields at 100% coverage of what the site displays**, for 1 request per 20 companies.
- **$0**, no key, no login, no browser, no proxy, no evasion — nothing to ban.
- 2 pages of results = **2 HTTP requests, ~8 seconds**. Your whole probe target costs less than
  a page refresh.
- Then fetch job **detail** pages only for the shortlist your résumé scorer ranks highly. That is
  where `equity_raw` and JSON-LD live. Enriching the top 20 of 600 costs 20 requests, not 600 —
  and it's the step that takes you to **12/12**.

Why not P6 as primary, despite the brief's expectation: **31.6%** high-confidence resolution and
**18.4%** with live postings. You would discard two-thirds of the market and systematically bias
toward large companies. It is an enrichment layer, not a backbone.

Why not P4: it recovers byte-for-byte the same `__NEXT_DATA__` as P1 while adding a browser
runtime, a persistent profile, a manual login, and your account as a bannable asset. Strictly
worse. Keep the module for diagnostics only.

### Fallback ladder, in order of what breaks

1. **Wellfound changes the embedded shape** (Pages Router → App Router, or Apollo → RSC flight
   chunks — `self.__next_f.push`). *Cost: hours.* The data is still in the HTML; only the parse
   path changes. Watch for `__NEXT_DATA__` disappearing while `next_flight_chunks > 0` — P1
   already counts those.

2. **Wellfound starts challenging plain HTTP.** Fall back to **P2**: JSON-LD on detail pages is
   published for Google for Jobs and is the most durable contract on the site — breaking it costs
   them search traffic. It cannot enumerate, so pair it with the sitemap-free discovery you still
   have (role pages) or with P6 for the companies you already know.

3. **Wellfound becomes unreadable entirely.** Fall back to **P6** for the ~32% of companies with
   real ATS boards. These endpoints are keyless, documented, and stable; fragility 1 of 5. You
   lose small-startup coverage and equity, and you keep salary only at 5.6%.

4. **Last resort: Coresignal** (~$0.08/job), the only vendor explicitly claiming Wellfound
   coverage. Not worth paying today.

**Architecturally:** keep `Job` as the single normalized record (it already is), and make the
source pluggable so 1 → 2 → 3 is a config change, not a rewrite. Cache aggressively; you're
re-scoring against a résumé, and yesterday's listings are fine.

**One durable warning:** P1's fragility is 3, not 1, because `__NEXT_DATA__` is an internal
implementation detail with no stability contract. It will break eventually — probably without
warning, and possibly silently. Assert on `role_applied`, `page_returned`, and a floor for
`n_jobs`, and fail loudly. Silent wrongness is the failure mode here, not downtime.

---

## 7. What I'd test next

1. **Company pages for full enumeration.** `/company/{slug}/jobs` should expose the ~22% of jobs
   the search pages truncate. Verify it's the same Apollo shape and whether it carries equity.
2. **The GraphQL endpoint directly.** The page ships a `buildId`
   (`ch-4a221b6fe40b987f1a7b4de05ded67d6`) and literal query args. If Wellfound exposes a POST
   GraphQL route, one request could replace pagination entirely — but check robots and terms
   first; `/_jobs/` is disallowed and an undocumented API is a different conduct question.
3. **The logged-in P4 variant** — the one deliverable I could not execute. Worth 20 minutes to
   confirm login adds nothing to search data before deleting the module.
4. **Role-slug enumeration.** There is no published list. `deep-learning-engineer` silently
   fell back; `ai-engineer`, `machine-learning-engineer`, `data-scientist` all work. Build a
   validated slug catalogue — it directly determines recall, and it's the trap most likely to
   quietly halve your results.
5. **Freshness/churn.** Re-run daily for a week and diff `source_job_id` sets to measure how fast
   listings turn over. That sets your polling interval and tells you whether caching for a day
   is safe.
6. **Longer-horizon durability.** 20 requests proved nothing was mitigated; 500 over a week is
   the real test of where the polite-traffic ceiling sits.
7. **`resolve_ats` recall.** 31.6% is a floor, not a ceiling — it's limited by slug guessing.
   Resolving via each company's careers page (follow `wellfound.com/company/{slug}` → website →
   `/careers` → detect board URL) would raise it, at ~2 extra requests per company.

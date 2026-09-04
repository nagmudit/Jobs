# ADR-011: Listings expire from the cache; mitigations never do

**Status:** accepted · **Date:** 2026-09-05

## Context

`Fetcher` caches every response to `cache/` keyed by a URL hash. That rule came from the
Phase 1 probe brief, where it was exactly right: *"Cache every raw response to `cache/`
keyed by URL hash. Re-runs must hit the cache, not the network."* A probe re-runs the
same URLs dozens of times while the analysis changes, and paying a third party for each
re-run would have been indefensible.

The live tool inherited the rule unchanged, and in a live tool it is a bug.

`_raw_get` returned any cached entry unconditionally, and nothing in the crawl or ingest
path ever passed `refresh=True` — only `enrich.py` did. Cache inspection on 2026-09-05:

    wellfound.com    209 files   oldest 2026-09-02   newest 2026-09-04
    himalayas.app     43 files   oldest 2026-09-04   newest 2026-09-04

So `fetch --roles software-engineer` on 2026-09-05 would have made **zero network
requests and found zero new jobs**. The tool could not discover a job posted after the
first crawl of a given URL without someone deleting `cache/` by hand. Every corpus count
looked frozen, which is what prompted the investigation.

This also meant raising any page budget was pointless: a wider Himalayas window would
have re-read the same 500 cached jobs.

## Decision

**Listing responses carry a freshness window. Detail responses do not. A cached
mitigation never expires at all.**

`Fetcher.get(url, max_age=...)` re-fetches a cache entry older than `max_age` seconds.
`cache_ttl_hours: 6` in `targets.yaml` becomes `Fetcher.listing_ttl`, and the callers
that fetch listings pass it explicitly:

| Caller | Fetches | `max_age` |
|---|---|---|
| `crawl.py` | Wellfound search pages | `listing_ttl` |
| `sources/{remoteok,himalayas}.py` | API feeds | `listing_ttl` |
| `sources/{greenhouse,ashby,workable}.py` | ATS boards | `listing_ttl` |
| `ats.resolve` | board-token probes | `listing_ttl` |
| `enrich.py` | Wellfound job detail pages | none — permanent |
| `sources/ashby.py` detail | per-posting detail | none — permanent |
| `_load_robots` | `robots.txt` | `ROBOTS_TTL`, 24 h |

Passing it at the call site rather than inferring it inside `Fetcher` is deliberate:
`Fetcher` has no way to tell a listing from a detail page, and which requests are
freshness-sensitive should be greppable.

Age is taken from the recorded `fetched_at`, not the file mtime — it is the real fetch
time and it survives copying the cache directory. An entry whose timestamp cannot be
parsed counts as infinitely old: a cache entry we cannot date is one we cannot vouch for.

### The carve-out, which is a conduct rule and not a performance one

**A cached `403`, `429`, `503`, or any response carrying `cf-mitigated` is sticky
forever.** `_load` skips the age check entirely for those, so replaying one keeps raising
`MitigationDetected` however old it is.

Without that carve-out the TTL would quietly become a backoff-and-continue path — the
tool would halt on a mitigation, then retry the same URL six hours later, forever, on a
timer. AGENTS.md forbids exactly this: *"Never retry through it, never add a
backoff-and-continue path."* Clearing a mitigation stays a deliberate human act: delete
the cache entry.

`_is_mitigation` is kept in step with `assertions.check_mitigation`, which is the only
other place that decides what counts as Cloudflare acting on us. An ordinary failure
(404, 5xx) is *not* a mitigation and does age out — a board that 404'd last week may
exist today.

`robots.txt` gets its own, longer window for a related reason: it was cached
permanently, so a site could tighten its rules and we would never see it. Twenty-four
hours re-checks daily while still working offline in between.

## Consequences

**What this costs.** A cold role fetch across all six sources now runs ~1,100 requests
and 75–95 minutes at the mandated 3–5 s spacing. Re-runs within the six-hour window stay
nearly free, which is what makes iterating on filters bearable. Long runs belong in a
background task.

**What it does not change.** A re-fetch still passes through `assert_allowed`,
`_throttle` and `check_mitigation`. The TTL adds requests; it does not bypass a single
rule. Rate limit, robots, concurrency and the honest User-Agent are untouched.

**The probe's rule still stands where it was written.** `wellfound-probe/` is frozen and
keeps its permanent cache. This ADR changes the live tool only.

**Reversible.** `cache_ttl_hours: 0` restores the old permanent behaviour exactly.

## Alternatives rejected

**A `--refresh` flag on `fetch`.** Already exists for enrichment and does not fix the
default. The failure was silent — no error, no empty result, just a stale corpus that
looked complete — so it had to be fixed in the default path.

**Expiring everything, including detail pages.** A job description does not change after
posting. Re-fetching thousands of detail pages on a timer is pure cost against a third
party for no new information.

**Deleting `cache/` between runs.** Loses the raw evidence that ADR-002 exists to keep,
and would re-fetch detail pages too.

## Verification

`tests/test_fetch_cache.py`. The two that matter: a 7-hour-old listing under a 6-hour TTL
must go back to the network, and a 30-day-old cached 403 must **not**. Both were watched
failing before the fix, and both were re-confirmed by mutation — disabling the age
comparison reddens the first three, disabling the mitigation check reddens exactly the
two sticky-cache tests.

# ADR-013: An out-of-band robots permission is declared, not assumed

**Status:** accepted · **Date:** 2026-09-08

## Context

vickybytes.com lists jobs. Its listings are served to the browser from
`GET https://vickybytes.com/api/opportunities` — one unauthenticated request
returning the whole feed, which would make it the cheapest source in the repo.

Its `robots.txt`, fetched 2026-09-08, disallows it:

```
User-agent: *
Allow: /
Disallow: /admin/
Disallow: /auth/
Disallow: /forgot-password
Disallow: /reset-password
Disallow: /about
Disallow: /search
Disallow: /profile
Disallow: /avatar
Disallow: /payout
Disallow: /newpost-test
Disallow: /api

Sitemap: https://vickybytes.com/sitemap.xml
```

`Fetcher.allowed`, run against those exact rules, refuses the endpoint
(`matched Disallow: /api`), and `assert_allowed` raises `RobotsDisallowed`.

**The permitted alternative was investigated and does not exist.** The sitemap it
advertises is a `urlset` of ~67 URLs — blog posts, `/leaderboard`,
`/system-design`, `/login` — and contains **zero** job listings. So there is no
robots-permitted way to discover a listing URL. The only remaining route would be
enumerating id ranges against pages that may not exist: thousands of requests to
reconstruct what one call returns, at a site that has said `Disallow: /api`. That
is worse conduct than the thing being avoided.

The repo owner then **asked the site owner, who agreed** to a personal daily
fetch of that one endpoint.

## Decision

A permission that exists outside `robots.txt` is honoured **only when it is
declared in `targets.yaml` with its evidence**, and it flips exactly one path
prefix on one origin.

```yaml
robots_overrides:
  - origin: https://vickybytes.com
    prefix: /api/opportunities
    granted_by: "..."
    granted_on: 2026-09-08
    note: "..."
```

`Config.load` refuses a grant missing any of the five fields, the same way it
refuses a `delay_range` floor under 3 s. `Fetcher.allowed` applies a grant
**only after** the normal robots match, and **only to a refusal**.

## Why this is not a weakened guard

The distinction is load-bearing, and the tests exist to keep it true:

- `robots.txt` is still fetched and still parsed for that host. A host whose
  robots.txt cannot be read **still raises**, override or not — the naukri.com
  protection in ADR-007 is untouched. A grant can never be a way to crawl blind.
- The grant flips a **disallow to allow**, never the reverse, and only for the
  declared prefix. `/admin/`, `/auth/`, `/profile` and `/payout` on that host
  stay refused.
- Grants are **per-origin**. The same prefix on another host is refused;
  permission from one site's owner is not transferable.
- With no grants configured, behaviour is byte-identical to before.
- Using a grant **prints a line** naming it, so a run leaning on one says so
  rather than looking like ordinary permitted traffic.

## Consequences

- **The grant is informal.** A plain yes, no written terms, no stated rate limit.
  Recorded as such rather than dressed up. Our own 3–5 s pacing and concurrency 1
  still apply — those are this repo's floor, not the site's to waive.
- **Revoking is deleting the entry.** No code change, no redeploy: the fetcher
  refuses the path again immediately. A test covers that path.
- The mechanism generalises, which is a risk. It is bounded by making the
  evidence mandatory at load time: a grant nobody can justify will not load.
- AGENTS.md's robots rule now reads "enforced in code, with declared exceptions",
  not "absolute". That is a real weakening of a previously flat rule, and the
  reason it is acceptable is that the exception is auditable, narrow, loud, and
  revocable in one line.

## Related

`jobsearch/src/fetch.py::Fetcher.allowed` · `jobsearch/src/config.py::_validated_overrides` ·
`jobsearch/targets.yaml` · `jobsearch/tests/test_robots_multiorigin.py` ·
`jobsearch/tests/test_conduct_guards.py` · ADR-007

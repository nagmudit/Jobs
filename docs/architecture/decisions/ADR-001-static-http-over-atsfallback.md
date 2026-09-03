# ADR-001: Read Wellfound via static HTTP + `__NEXT_DATA__`, not an ATS fallback

**Status:** accepted · **Date:** 2026-09-02

## Context

Wellfound has no public API, sits behind Cloudflare, and several commercial scrapers
built on it are broken. Before writing a tool we ran a six-path feasibility probe
(`wellfound-probe/`, evidence in `REPORT.md`). The expected winner going in was P6 —
resolving each company to its public ATS board (Greenhouse/Lever/Ashby/Workable) and
reading keyless JSON.

## Decision

Build on **P1: a plain unauthenticated `httpx` GET of the SEO landing pages**, parsing
the Apollo cache embedded in the `__NEXT_DATA__` script tag. Use job detail pages (P2)
as on-demand enrichment. Do not build on the ATS fallback.

## Rationale

Measured, not assumed:

- P1 returns HTTP 200 with complete listing data. 20 consecutive requests at 3–5 s
  spacing produced 20× 200 and `cf-mitigated: None` every time.
- P1 recovers 11 of 12 target fields and **100% of what the site itself displays**
  (83/83 jobs, exact match against rendered HTML).
- The ATS fallback resolved only **31.6%** of companies with high confidence, and only
  **18.4%** to a board with live postings.
- Worse, the companies that *do* resolve skew large and well-funded. An ATS-first
  pipeline would have silently biased the corpus toward big employers and dropped the
  early-stage startups that are the reason to use Wellfound at all.

## Alternatives

- **P6 ATS fallback** — rejected as primary on the hit rate above. Retained as a
  fallback tier and as an annotation (`ats_source`).
- **P3 feeds** — no RSS/Atom/JSON feed exists. Sitemaps carry zero job URLs.
- **P4 Playwright with a real session** — works, and recovers byte-identical data to
  plain HTTP while adding a browser runtime, a persistent profile, a manual login, and
  the user's account as a bannable asset. Strictly worse.
- **P5 paid APIs** — one Apify actor self-describes as "HTML/JSON-LD parsing, no
  browser automation, no login or proxies", i.e. selling P1+P2 back at $2.99/1000. Six
  of seven mainstream job-data APIs do not cover Wellfound at all.

## Consequences

- Zero cost, no key, no login, no browser, no proxy — nothing to ban.
- **`__NEXT_DATA__` is an internal implementation detail with no stability contract.**
  It will break eventually, possibly silently. That risk is accepted and mitigated by
  the assertions in ADR-003, not eliminated.
- Equity is unavailable from search pages and from every ATS; it exists only on job
  detail pages. That forced the two-tier breadth/depth split (ADR-004).
- If P1 breaks, the documented fallback ladder is P2 (JSON-LD, published for Google
  for Jobs and therefore more durable) → P6 (ATS, ~32% coverage) → Coresignal (paid).

## Related

`wellfound-probe/REPORT.md` §1, §2, §6 · `jobsearch/src/parse.py` ·
`jobsearch/src/enrich.py`

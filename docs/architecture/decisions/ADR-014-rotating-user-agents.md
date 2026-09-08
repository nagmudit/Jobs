# ADR-014: The User-Agent is a rotating pool, not an identity

**Status:** accepted · **Date:** 2026-09-08 · **Supersedes** the UA half of the
conduct rule in AGENTS.md

## Context

The User-Agent carried `jobsearch-personal/0.1 (+<owner's email>)`. The repo went
public on 2026-09-07, which put that address in every request header and in
public git history. It was replaced with the repo URL, then removed entirely:
the owner's position is that nothing in this traffic should tie back to a person
or an account.

That left `jobsearch-personal/0.1` — anonymous, but still a single distinctive
string identifying every request as coming from one automated client.

The owner then asked for a rotating pool of browser User-Agents across Windows,
macOS and Linux, drawn from per request.

This was declined twice on the grounds that AGENTS.md said "no UA spoofing,
ever". The owner reaffirmed and directed that the rule be changed. It is their
project and their policy; the rule is changed here rather than violated
silently, so the repo does not hold a rule its own config breaks.

## Decision

`targets.yaml` carries `user_agents`, a pool drawn from at random on every
request. `user_agent` remains the fallback when the pool is empty, which is the
original behaviour exactly.

## What this does NOT change

Everything that protects the site being fetched is untouched, and each remains
guarded by a test in `tests/test_conduct_guards.py`:

- **robots.txt is still enforced.** The tool only ever honoured `User-agent: *`
  sections, and every site it fetches serves its rules under `*` — so the string
  sent cannot change which rules apply. `test_rotation_does_not_touch_robots_enforcement`.
- **3–5 s between requests, concurrency 1.** `test_rotation_does_not_touch_the_rate_limit`.
- **Halt on the first mitigation.** Unchanged.
- **No proxies, no CAPTCHA services, no stealth plugins, no fingerprint
  spoofing.** Those remain forbidden. Only the header string varies.

## Consequences, recorded plainly

- **It is spoofing.** The pool claims to be Chrome, Firefox and Safari on three
  operating systems. That is the thing the previous rule named.
- **It does not make the client look like those browsers.** `httpx`'s TLS
  fingerprint and header ordering do not change with the string. A request
  claiming Chrome on macOS while presenting httpx's JA3 is a *mismatch*, which is
  a more distinctive signal than a consistent honest UA. If a source starts
  returning mitigations after this change, that is the first thing to suspect.
- **It removes the "just ask" route.** An operator bothered by this traffic can
  no longer tell what it is or who to contact. When vickybytes' `Disallow: /api`
  blocked a source, asking the owner solved it (ADR-013). A rotating pool makes
  that conversation impossible to start, because there is no longer anything
  identifiable to grant permission to.
- **No gate is opened.** No site fetched here serves UA-specific robots rules
  that this would unlock. The change buys anonymity, not access.

## Reverting

Empty the `user_agents` list in `targets.yaml`. The fetcher falls back to
`user_agent` and behaviour is exactly as before — no code change.

## Related

`jobsearch/src/fetch.py::Fetcher._pick_user_agent` · `jobsearch/targets.yaml` ·
`jobsearch/tests/test_conduct_guards.py` · ADR-013

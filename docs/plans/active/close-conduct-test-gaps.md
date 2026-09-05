---
status: active
created: 2026-09-03
last_updated: 2026-09-03
areas: [jobsearch/src/fetch.py, jobsearch/src/config.py, jobsearch/src/crawl.py, jobsearch/tests]
---

# Close the conduct-layer test gaps

## Objective

Every conduct control — robots enforcement, rate limiting, mitigation halting, the
delay floor — is verified by a test that would fail if the control were removed.
Maturity moves from level 1 (tests exist, nothing enforces) to level 2 (CI blocks).

Done means: `python quality/validate_manifest.py` reports zero open P0 gaps, and a
push with a broken conduct guard fails CI.

## Current behavior

Verified 2026-09-03:

- `python -m pytest tests -q` → **38 passed in 0.60s**. No skips, no `.only`.
- `src/fetch.py`, `src/config.py`, `src/crawl.py` are referenced by **zero** test
  files. `src/fetch.py` is the module holding robots enforcement (`assert_allowed`),
  the rate limiter (`_throttle`), the disk cache, and the `check_mitigation` call.
- `assertions.check_mitigation` is well tested in isolation, but nothing verifies
  `Fetcher.get` still calls it — the call could be deleted and the suite stays green.
- No CI. Nothing blocks anything.

## Desired behavior

`tests/test_conduct_guards.py`, `tests/test_fetch_cache.py`, `tests/test_crawl.py` exist and cover
the P0 conduct journeys CJ-001 through CJ-004, CJ-006, CJ-011, CJ-016. A GitHub
Actions workflow runs the suite plus `validate_manifest.py` on push and PR and blocks
on failure.

## Scope

**In:** tests for `fetch`, `config`, `crawl`; one PR-stage CI workflow; an egress
boundary check.

**Out:** tests for `cli.py` (P2); `wellfound-probe/` (frozen); coverage tooling
(explicitly not chased); any test that touches the network.

## Approach

Inject a fake transport rather than mocking our own code. `Fetcher` currently
constructs `httpx.Client` inline in `_raw_get`; the smallest honest change is to let
`Fetcher` accept an optional `transport` (httpx supports `MockTransport`) so the real
`Fetcher.get` code path — robots check, throttle, cache, `check_mitigation`, logging —
runs end to end against canned responses.

That matters: mocking `Fetcher.get` itself would assert nothing. The point is to
exercise the wiring, since GAP-002 is precisely "the guard exists but might not be
wired in".

For the rate limiter, monkeypatch `time.sleep` to record durations rather than
actually sleeping — assert the *requested* spacing, keep the suite fast.

## Milestones

- [~] `Fetcher(..., transport=None)` seam — NOT built. Monkeypatching `httpx.Client`
      at the module boundary proved sufficient and adds no production surface, so the
      seam was not worth its cost. Revisit only if a test needs something that cannot
      reach.
- [x] `tests/test_conduct_guards.py` + `tests/test_fetch_cache.py` — CONDUCT-001/002/003/
      009/012 → closed GAP-001, 002, 006, 007, 011 (2026-09-06)
- [x] CONFIG-001, in `tests/test_conduct_guards.py` → closed GAP-003 (2026-09-06)
- [ ] `tests/test_crawl.py` — CRAWL-002/003 → closes GAP-004, 005
- [x] `.github/workflows/ci.yml` — pytest on 3.11+3.14, both validators, and the suite
      re-run with outbound traffic blocked → closed GAP-010 (2026-09-06)
- [x] Egress boundary check, as a static AST walk → closed GAP-007 (2026-09-06)
- [x] Manifest updated: CONDUCT-001/002/003 and CONFIG-001 proposed → implemented;
      10 gaps closed, 7 remain (2026-09-06)

## Log

### 2026-09-03
- Did: ran the test-governance audit; wrote `quality/test-manifest.yaml`,
  `quality/validate_manifest.py`, `docs/testing/report.md`.
- Ran: `python -m pytest tests -q` → 38 passed in 0.60s.
- Ran: `python quality/validate_manifest.py` → valid (17 journeys, 17 tests, 11 open
  gaps).
- Found: the validator caught a genuine hole in the manifest on first run — CJ-003
  (rate limiting) had a *proposed* test and no gap, so a P0 journey was silently
  unprotected. Added GAP-011. The rule earned its place immediately.
- Found: the coverage inversion is the real story — the lowest-blast-radius code
  (parsing) is well tested, the highest (conduct) is not tested at all.
- Blocked: nothing.

## Decisions

**Audit mode only; no test code written yet.** The skill separates `audit` from
`apply` deliberately, and writing tests in the same pass as ranking them tends to
produce tests for whatever was easiest to reach rather than what ranked highest.

**Fake transport at the network boundary, not mocked internals.** Mocking
`Fetcher.get` would test the mock. The gap being closed is specifically "is the guard
still wired into the call path", which only a real code path can answer.

**No coverage gate.** Per the skill's guardrails and ADR reasoning: coverage rewards
testing cheap paths, which is exactly the inversion this audit found.

## Open questions

- Should the egress check be a test (`test_boundaries.py`) or a lint step? A test
  keeps it in one runner and needs no new tooling — leaning that way, but it is a
  slightly unusual thing to assert in pytest.
- CI on GitHub Actions assumes this repo gets a remote. It currently has one commit
  and no remote configured. If it stays local, GAP-010 should be re-scoped to a
  pre-commit hook instead. **Needs the user's call.**

## Remaining

Next agent starts at milestone 1: add the `transport` seam to `Fetcher.__init__` and
thread it into `_raw_get`, then write `tests/test_conduct_guards.py`. Do not change any conduct
default while doing it.

## Done when

- `python quality/validate_manifest.py` → zero open P0 gaps.
- `python -m pytest tests -q` → green, with the new files included.
- Each new test verified to fail when its guard is removed, and restored.
- Manifest entries flipped to `implemented` with real `location` paths.

### 2026-09-06 — conduct cluster closed, plus three user-facing fixes

Ordered by the user: salary fix, corpus cleanup, background crawl, conduct tests, CI.

- **Found the suite RED before starting.** `test_robots_multiorigin.py` seeded its fake
  robots.txt with a hardcoded `2026-09-04`; the 24 h `ROBOTS_TTL` added on 2026-09-05
  turned that into a time bomb, green at 19 h old and red at 43 h. Six tests broke
  overnight with no commit to blame. Fixture is now relative to now, and the CI schedule
  below exists specifically so time alone can turn the build red.
- **GAP-014 (salary).** `derive._NUM` was already capturing the currency symbol and
  discarding it, so 2,331 rows had a numeric salary and no currency; ₹1.2cr outranked
  $520k on the UI's primary sort. Added `salary_currency` + `salary_usd_*`, sort and
  threshold filter now use them, display stays native. See ADR-012.
- **GAP-016 (corpus).** `cli reconcile` re-applies the role filter to rows that predate
  it. **Two near-misses caught while building it:**
  1. A first pass matched RemoteOK rows against their `badges` — which ARE RemoteOK's
     own tags — and concluded JANITOR was a `software-engineer`. Exactly the trap
     AGENTS.md documents. The backfill must use each source's live rule: title-only for
     RemoteOK.
  2. Scoping by "has no configured role slug" pulled in 2,478 **Wellfound** rows under
     the retired `ai-engineer`/`data-engineer` slugs. Those were server-side filtered at
     crawl time and are legitimate; the command would have deleted 598 of them by
     re-judging with a cruder keyword list. Scope is now sources that cannot filter
     server-side. Both are regression-tested.
  Applied after a WAL-checkpointed backup: re-tagged 149, removed 512. GAP-017 fell out
  as a side effect — both affected rows were part of the junk.
- **Conduct cluster (GAP-002/003/007/011).** `tests/test_conduct_guards.py`. Every guard
  mutation-verified: neutering the rate limiter, removing `check_mitigation` from the
  request path, and deleting the `delay_range` floor each redden exactly the right tests.
  The egress check is a static AST walk (a new call site is the risk, and no runtime test
  would see one until it shipped); `urllib.parse` is deliberately allowed, and the check
  is itself proven able to detect a real offender.
- **CI (GAP-010).** Three jobs: matrix test on 3.11/3.14, governance validators, and the
  suite re-run with `iptables -P OUTPUT DROP`. **Verified locally first** by patching
  `socket.connect`/`create_connection`/`getaddrinfo` to raise — 215 passed with every
  socket blocked, so the offline claim is measured rather than asserted.
- **AGENTS.md hit the 200-line budget twice**, so the Conventions section moved to
  `docs/engineering/conventions.md` with the six load-bearing rules kept inline.
- Ran: `python -m pytest tests -q` → **215 passed**. Both validators clean.
  Gaps 17 → **7**.

## Remaining

GAP-004 (crawl slice loop) and GAP-012 (web endpoint guards) are the last two P0s.
GAP-005, 008, 009, 013, 015 are P1/P2.

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

`tests/test_fetch.py`, `tests/test_config.py`, `tests/test_crawl.py` exist and cover
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

- [ ] `Fetcher(..., transport=None)` seam, defaulting to real behaviour
- [ ] `tests/test_fetch.py` — CONDUCT-001/002/003/004 → closes GAP-001, 002, 006, 011
- [ ] `tests/test_config.py` — CONFIG-001 → closes GAP-003
- [ ] `tests/test_crawl.py` — CRAWL-002/003 → closes GAP-004, 005
- [ ] `.github/workflows/ci.yml` — pytest + manifest validation, blocking → closes GAP-010
- [ ] Egress boundary check: `httpx` imported only by `fetch.py` → closes GAP-007
- [ ] Manifest updated: proposed → implemented, gaps closed

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
thread it into `_raw_get`, then write `tests/test_fetch.py`. Do not change any conduct
default while doing it.

## Done when

- `python quality/validate_manifest.py` → zero open P0 gaps.
- `python -m pytest tests -q` → green, with the new files included.
- Each new test verified to fail when its guard is removed, and restored.
- Manifest entries flipped to `implemented` with real `location` paths.

---
status: current
last_verified: 2026-09-03
applies_to: [jobsearch]
---

# Test Audit — 2026-09-03

Mode: `audit`. Revision `0099cae`. Detail lives in
[`quality/test-manifest.yaml`](../../quality/test-manifest.yaml); this page is the
Monday-morning version.

## Verdict

**Maturity level 1** — tests exist, they pass, and nothing enforces them.

The suite is genuinely good at what it covers and covers the wrong half. Every test
protects **data shaping**: parsing, deriving, storing, filtering. Nothing protects
**conduct** — robots enforcement, the rate limiter, the cache, and the mitigation
halt are all untested.

That inversion matters more here than in a normal repo. A data-shaping bug produces a
wrong salary in a local SQLite file that the user can see and re-crawl. A conduct bug
produces unthrottled traffic against a live third-party site with Cloudflare in front,
which is silent, harms someone else, and can revoke access permanently. **The lowest
blast radius is well covered and the highest is not covered at all.**

Single change that moves us to level 2: run the existing suite in CI on every push and
block on failure. It is already fast (0.6 s) and fully offline.

## Suite status — actually run

```
$ python -m pytest tests -q
......................................    [100%]
38 passed in 0.60s
```

No skipped tests, no `xfail`, no `.only`. That is worth stating: a permanently-skipped
test reads as coverage in every count and protects nothing, and there are none here.

## What is already solid

- **All four corpus-integrity guards** are tested against synthetic `__NEXT_DATA__`
  fixtures reproducing each real failure shape — and these are the traps that actually
  bit during development, so the tests are shaped by real failures rather than guessed
  risk.
- **`user_state` survives a `job_raw` rewrite** (CJ-010). This protects the only
  non-regenerable data in the database.
- **Dedupe and provenance** (CJ-009) — one row, N provenance rows.
- **The `''`-not-`null` normalisation** (CJ-012), which had already shipped as a real
  bug making the salary fill rate read 100% when the truth was 52%.
- **Filter SQL is asserted parameterised** — injection-safe, hidden excluded by default.
- Tests are offline, deterministic, and use no network, no clock, and no random data.

## Top exposures

Ranked by cost of failure, not by ease of testing.

| # | Gap | P | Why it ranks here |
|---|---|---|---|
| 1 | **`src/fetch.py` has zero tests** (GAP-001) | P0 | robots, rate limit, cache, and cf-logging all unverified. The one module whose failure harms a third party. |
| 2 | **Rate limiter unverified** (GAP-011) | P0 | `_throttle` has no test. A regression dropping spacing to zero ships silently and looks like an attack. |
| 3 | **Mitigation halt not tested in the call path** (GAP-002) | P0 | `check_mitigation` is tested; nothing proves `Fetcher.get` *calls* it. Delete the call and the suite stays green. |
| 4 | **`delay_range` floor guard untested** (GAP-003) | P0 | The only *enforced* conduct control, never exercised. |
| 5 | **`src/crawl.py` untested** (GAP-004) | P0 | `PageWrap` is verified as an assertion but not as loop behaviour — nothing proves `crawl_slice` catches it, stops, and records `ended_reason`. |
| 6 | **Resume path untested** (GAP-005) | P1 | A resume telemetry bug already shipped once and was caught by eye, not by a test. That is precisely the argument for the test. |
| 7 | **Egress boundary unenforced** (GAP-007) | P1 | "All network access goes through `Fetcher.get`" is convention only. A second `httpx` call anywhere bypasses every conduct rule. |
| 8 | **No CI** (GAP-010) | P1 | Nothing blocks. This is what pins maturity at level 1. |

Two of these — 3 and 7 — are the same shape of problem and the most insidious: the
guard exists, is tested in isolation, and nothing verifies it is still wired in.

## Coverage by module

| Module | Referenced by tests | Grade |
|---|---|---|
| `parse.py` | 4 files | covered |
| `derive.py` | 2 | covered |
| `assertions.py` | 1 | covered (in isolation) |
| `store.py` | 1 | covered |
| `web/app.py` | 2 | weak — `_where` only; no route tested |
| `enrich.py` | 2 | weak — `parse_detail` only; orchestration untested |
| `fetch.py` | **0** | **uncovered** |
| `crawl.py` | **0** | **uncovered** |
| `config.py` | **0** | **uncovered** |
| `cli.py` | **0** | uncovered (P2 — acceptable for now) |

"Weak" is used precisely: a test exists but asserts too little to fail on the
regression that matters.

## Recommended sequence

Per `references/gates.md`, each step leaves the repo safer than the last. The plan is
in [`docs/plans/active/close-conduct-test-gaps.md`](../plans/active/close-conduct-test-gaps.md).

1. **`tests/test_conduct_guards.py`** (done 2026-09-06) — mitigation on `cf-mitigated` and on
   403/429, throttle spacing, cache round-trip. Closes GAP-001/002/006/011. Highest
   value in the repo; all four are cheap and fully offline against a stub transport.
2. **`tests/test_conduct_guards.py`** (done 2026-09-06) — the delay floor, the rate
   limiter, and a static egress check. Closes GAP-002, 003, 007, 011.
3. **`tests/test_crawl.py`** — `page_wrap` termination and resume telemetry against a
   fake fetcher. Closes GAP-004/005.
4. **CI** — one GitHub Actions workflow running `pytest` + `validate_manifest.py` on
   push and PR, blocking on failure. Closes GAP-010, moves maturity to level 2.
5. **Egress boundary check** — grep or AST assertion that `httpx` is imported only by
   `fetch.py`. Closes GAP-007. Promotes a prose rule into an enforced one.

Steps 1–3 need no new dependencies, no containers, and no network.

## Layers — chosen, and deliberately not chosen

| Layer | Verdict |
|---|---|
| Unit | Yes — real parsing/derivation logic |
| Integration (SQLite) | Yes — a real DB exists; `tmp_path` is fast and real |
| Static / typecheck / lint | **Not configured.** Worth adding; not claimed anywhere |
| Secret scanning | Deferred — no secrets in repo; UA email is deliberate and public |
| E2E (browser) | **No.** Single-user local UI; the API-level tests are the affordable equivalent |
| Contract | **No.** Wellfound has no contract with us — that is the entire premise |
| Perf / load | **No.** One user, ~1,800 rows |
| Permission / tenancy | **No.** Single-user tool |

Contract tests deserve a note: normally you contract-test every third party you fake.
Here the "provider" is an undocumented internal JSON blob with no stability guarantee,
so `SchemaDrift` **is** our contract test — it fails loudly when the shape moves.

## Limitations

- Coverage percentages were not measured; no coverage tool is configured, and per the
  skill's guardrails a number was not chased.
- CJ-017 (apply URL resolves to a live posting) was verified **manually** on
  2026-09-03 against `jobs/4235671-ai-engineer` and cannot be automated without
  network. The affordable half — pinning the URL template against a known raw node —
  is filed as GAP-009.
- `wellfound-probe/` is out of scope. It is frozen research with no tests, and adding
  them would be maintenance on an artifact nobody will change.
- Module coverage above is by import reference, not by executed lines. It is a
  reliable signal for "zero tests" and a weak one for "well tested".

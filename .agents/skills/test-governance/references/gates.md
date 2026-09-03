# Pipelines, Gates, and the Agent Testing Contract

## Build pipelines in this order

Each stage only earns its place once the previous one is green and trusted.

### 1. Pull request — build this first, and often only this

Fast feedback or developers route around it. Target under 10 minutes.

```
format + lint + typecheck        parallel, fail fast
secret scan
unit tests
integration tests (containers)
P0 journey / smoke tests
manifest validation
build
```

Everything here blocks merge. A non-blocking check is a check people learn to ignore, and its presence is worse than its absence because it looks like protection.

### 2. Main branch

Full suite including the slow parts excluded from PR. Coverage trend recorded for information, not enforced.

### 3. Nightly

Full regression · cross-browser · perf baseline · flaky detection (repeat the suite, flag nondeterminism) · long-running data tests · dependency drift.

Flaky detection is the highest-value nightly job and the most commonly skipped.

### 4. Release

P0 + P1 regression · migration forward **and rollback** · security scan · release evidence recorded.

### 5. Post-deploy

Smoke against the deployed environment · synthetic P0 journeys · health checks · defined rollback trigger.

**Stop at stage 1 for most repos.** Five pipelines on a repo whose suite isn't green yet is theater.

## Design rules

- Cheap deterministic checks first. Never spend eight minutes to discover a lint error.
- Parallelize independents; fail fast on the ones that gate everything.
- **Retries are a flakiness signal, not a fix.** Auto-retrying to green trains everyone to ignore red. Retry, but log and count it.
- Failures must reproduce locally with a single command printed in the output.
- Least-privilege test credentials, never production ones. Never echo secrets.
- Path-based selection is a trap for cross-cutting changes — always provide a forced full-run escape hatch.
- Distinguish infrastructure failure from product failure where you can, so people don't stop reading red.

## Quality gates

| Gate | Merge | Release |
|---|---|---|
| Lint / typecheck | block | block |
| Unit + integration | block | block |
| P0 journey tests | block | block |
| Secret scan | block | block |
| New P0 gap without owner | block | block |
| Migration rollback tested | — | block |
| Expired risk acceptance | warn | block |
| Coverage drop | report | report |
| Perf regression | warn | block |

**Coverage never blocks.** It measures which lines executed, not whether behavior is correct, and gating on it produces assertion-free tests written to satisfy the number. Track the trend, ask about sharp drops, don't automate a verdict.

### Flaky quarantine

Quarantine is allowed, and it must be governed:

- Owner named, expiry set (14 days is generous).
- Quarantined P0 tests are release-blocking regardless.
- Expiry passes → the *pipeline* fails, not the test.
- Report the quarantine list every week.

Ungoverned quarantine is how a suite quietly stops testing anything, one test at a time.

## The agent testing contract

Put this in `AGENTS.md` — see `context`. Rules that only live in an audit report change nothing after the audit.

```md
## Testing

Test manifest: `quality/test-manifest.yaml`. Suite: `<command>`.

### Every change makes a testing decision

State which, in the PR or the plan:
- Tests added — what and why
- Existing tests cover it — name them
- No test needed — justify (docs, formatting, generated code)

"No test needed" is a valid answer. An unstated decision is not.

### Bug fixes

Write the failing test first. Watch it fail. Then fix.
A bug fix without a regression test means the same bug ships twice.

### New P0/P1 behavior

Add a manifest entry in the same PR.

### Never

- Weaken or delete a test to make a change pass. Fix the code, or change the
  test deliberately and say so in the PR.
- Skip or `.only` a test on a shared branch.
- Claim a suite passed without running it — paste the output.
- Assert on mocks of our own code.
- Add sleeps. Wait on conditions.
```

The bug-fix rule is the one that compounds. A team that writes a regression test for every incident ends up with a suite shaped by real failures instead of guessed risk, and that suite gets better every time something goes wrong.

## Maturity

| Level | State |
|---|---|
| 0 | No tests, or a suite that doesn't pass |
| 1 | Tests exist, run locally, nothing enforces them |
| 2 | CI runs them and blocks merge on failure |
| 3 | P0 journeys covered, gaps tracked and owned, releases gated |
| 4 | Flakiness managed, incidents become regression tests, post-deploy verification |

Name the current level and the single change that moves it up one. Most repos sit at 1 and think the problem is coverage; the problem is that nothing blocks.

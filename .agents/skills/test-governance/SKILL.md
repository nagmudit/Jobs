---
name: test-governance
description: Work out what a repository actually needs to test, then build it — risk-ranked user and system journeys, a machine-readable test manifest, the missing suites, CI pipelines, and merge/release gates. Use when the user says "we have no tests", "what should I test", "set up a test strategy", "our tests keep breaking", "flaky tests", "improve coverage", "add CI quality gates", "we ship bugs to production", or is about to take an untested project live. Three modes — audit (what's covered, what's exposed), plan (journeys, manifest, pipeline design), apply (write the tests and workflows).
---

# Test Governance

Most repos with a testing problem don't need more tests. They need the *right dozen* tests and something that stops the untested paths from shipping.

This skill works out which paths would actually hurt if they broke, checks what already protects them, and then builds the gap — smallest suite first.

**Not a code reviewer.** Finding bugs in a diff is `code-review`. This designs and builds the test system around the repo.

## Modes

| Mode | Trigger | Output |
|---|---|---|
| `audit` | "do we have enough tests", "what's exposed" | `quality/test-manifest.yaml` (proposed) + short report |
| `plan` | "what should we build first" | journeys, catalogue, pipeline design, sequenced |
| `apply` | "write the tests", "set up CI" | working tests + workflows, one slice at a time |

Default `audit`. Say which mode in one line, then start.

## The governing rule

**Rank by what failure costs, never by what's easy to test.**

A payment path with no test and a util file at 100% is the normal state of a repo that measured coverage. Coverage percentage is a diagnostic, never a goal and never a gate on its own — it rewards testing the cheap paths.

The first ten tests in an untested repo should be ten P0 journey tests, not two hundred unit tests.

---

## Mode: audit

### Step 1 — Understand the product, then the tests

Read the context OS if it exists (`AGENTS.md`, `docs/index.md`, `docs/architecture/`) — it answers most of this for free. Otherwise inspect entry points, routes, schemas, jobs, and integrations directly.

You cannot rank test priority without knowing what the product does and who is hurt when it breaks. Do not skip to counting test files.

### Step 2 — Inventory what exists

Frameworks and runners · test locations and count by layer · coverage config and current numbers, if cheap to obtain · CI workflows and what actually blocks a merge · skipped, quarantined, and `.only` tests · fixtures, factories, seeds · what the tests use for external services.

**Run the existing suite** if it's safe and fast. A suite that doesn't pass on a clean checkout is the finding that outranks everything else — record the actual output, never assume it's green.

Note skipped tests specifically. A permanently-skipped test is worse than a deleted one: it reads as coverage in every count and protects nothing.

### Step 3 — Map capabilities and journeys

Build the risk map from `references/risk-map.md`. Assign stable IDs (`CAP-AUTH`, `CJ-001`).

Cover **system-facing journeys** — webhooks, retries, cron, migrations, provider fallback, token refresh, reconciliation. These are where production actually breaks and where tests almost never exist, because nobody clicks through them.

### Step 4 — Grade coverage honestly

For each P0/P1 journey: what protects it today, and would that test *fail* if the behavior broke?

A test that asserts a mock returned what the mock was told to return protects nothing. Count it as uncovered and say why.

### Step 5 — Write the manifest

Produce `quality/test-manifest.yaml` per `references/manifest.md`, every proposed entry `status: proposed`. Existing tests get mapped in as `implemented`.

The manifest is this skill's durable artifact and its findings ledger — `known_gaps` carries stable IDs and survives re-runs. Re-audits diff against it: keep IDs, mark `closed` / `regressed`, report the delta. Never renumber.

Plus a report under ~250 lines: verdict, top exposures, what's already solid, limitations. The manifest holds the detail; the report is for a human on a Monday morning.

**Do not write test code in this mode.**

---

## Mode: plan

Sequence the gaps so each step leaves the repo safer than the last:

1. **Make the suite trustworthy** — fix or delete failing and skipped tests. Nothing else matters while the suite is unreliable, because nobody will believe the next result either.
2. **Cover P0 journeys end to end** — thin, slow, few. Ten of these beat a thousand unit tests for shipping confidence.
3. **Push detail down a layer** — the edge cases those journeys revealed, tested where they're cheap and fast.
4. **Gate it** — CI runs it, failures block merge.
5. **Widen** — P1 journeys, contract tests, the domain-specific layers from `references/layers.md`.

Pick layers with the decision table in `references/layers.md`. Recommending every test type is how a plan becomes shelfware — most repos need three or four layers, not fifteen.

---

## Mode: apply

One slice per invocation: a journey, a capability, or a pipeline stage.

1. Match the repo's existing framework, structure, and naming. A second test framework alongside the first is a new problem, not a fix.
2. **Verify the test can fail.** Break the behavior, watch it go red, restore it. A test that has never failed is decoration.
3. Real dependencies where affordable, fakes at the boundary where not. Mocking your own code mostly tests your mocks.
4. Deterministic: no real clocks, no random data without a pinned seed, no cross-test order dependence, no sleeps.
5. Run the suite. Report actual output.
6. Update the manifest — `status: implemented`, `actual_location`, close the gap ID.

CI workflows: start with the pull-request pipeline from `references/gates.md` and nothing else. A five-pipeline setup on a repo with no green suite is theater.

Write the testing rules into `AGENTS.md` (see `references/gates.md`) so future agents inherit the habit. That writeback is what makes this stick after the skill stops running.

## Guardrails

- Don't chase a coverage number. Don't add a coverage gate without an explicit ask.
- Don't add a test framework, service container, or CI provider the repo doesn't already use unless there's no alternative — say why.
- Don't write tests that assert current behavior you believe is a bug. Flag it.
- Don't quarantine a flaky test without an owner and an expiry.
- Never claim a suite passes without running it.

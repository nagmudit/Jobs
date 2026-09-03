# Test Manifest

`quality/test-manifest.yaml` — the map from *what matters* to *what protects it*.

Test files tell you what is tested. Only a manifest tells you what is **not**, and that's the information anyone deciding whether to ship actually needs.

Three consumers: a human deciding whether to release, CI enforcing that P0 gaps don't grow, and an agent working out whether its change needs a test.

## Field discipline

The temptation is thirty fields per test. Nobody maintains thirty fields, so the file rots and everyone stops trusting it — which is worse than not having one.

**Ten fields that stay true beat thirty that go stale.** Add a field only when something reads it.

## Schema

```yaml
schema_version: "1.0"

product:
  name: acme-billing
  owners: [team-payments]

capabilities:
  - id: CAP-PAY
    name: Payments
    priority: P0
    description: Charge, refund, subscription lifecycle

journeys:
  - id: CJ-003
    name: Stripe webhook marks invoice paid
    capability: CAP-PAY
    priority: P0
    facing: system            # user | system
    blast_radius: Double-charge or silent non-payment
    release_blocking: true

tests:
  - id: PAY-INT-001
    name: Duplicate webhook does not double-charge
    journey: CJ-003
    priority: P0
    level: integration        # static|unit|integration|contract|e2e|security|perf
    status: proposed          # proposed | implemented | deprecated
    location: tests/integration/payments/webhook.test.ts
    command: pnpm test:integration payments
    triggers: [pull_request]
    merge_blocking: true
    owner: team-payments

known_gaps:
  - id: GAP-004
    journey: CJ-003
    priority: P0
    description: Out-of-order webhook delivery untested; ordering assumed
    status: open              # open | closed | regressed | accepted
    first_seen: 2026-08-10
    owner: team-payments

risk_acceptances:
  - gap: GAP-011
    reason: Load testing deferred until GA
    approved_by: cto
    expires: 2026-11-01       # required — an acceptance without expiry is an excuse

environments:
  - id: ci
    services: [postgres-16, stripe-mock]

last_audit:
  date: 2026-08-10
  revision: a3f9c21
  report: docs/testing/report.md
```

Omit sections the repo has no use for. An empty `environments:` is noise.

## `known_gaps` is the findings ledger

This skill's re-runnable state lives here, so it stays next to the coverage it describes rather than in a parallel file.

On re-audit: match by journey + description, **keep existing IDs**, set `closed` when a test now covers it, `regressed` when it was closed and broke again. Report the delta — `2 closed · 1 new · 1 regressed` — never a fresh essay.

`accepted` requires a matching `risk_acceptances` entry with an approver and an expiry. Expiry is not optional: a permanent acceptance is a decision to never fix it, and should be written down as such rather than disguised as a deferral.

## Validation rules

Cheap to script, worth wiring into CI once the manifest is real:

1. IDs unique; every `journey` and `capability` reference resolves.
2. Every P0 journey has at least one `implemented` test, or an open gap, or an unexpired acceptance. **Silence is not allowed.**
3. Every `implemented` test's `location` exists on disk.
4. Every `merge_blocking: true` test has a `command`.
5. Every open P0/P1 gap has an owner.
6. No expired `risk_acceptances`.
7. Test files on disk that map to no manifest entry → warn.
8. Skipped or `.only` tests in the codebase → fail.

Rules 2, 5, and 6 are the ones that matter. The rest is hygiene.

## Adopting it in an existing repo

Don't backfill everything — a week of cataloguing produces a file nobody trusts anyway.

1. Enumerate capabilities and P0/P1 journeys only.
2. Map **existing** tests that cover those journeys. Ignore the rest of the suite for now.
3. Record every uncovered P0/P1 as a gap.
4. Turn on validation rules 2, 5, 6.
5. Grow it as tests get written. New test → new entry, enforced at review.

P2/P3 tests can live outside the manifest indefinitely. The manifest exists to protect what matters, not to index everything.

## Keeping it alive

The manifest dies the moment it stops matching reality. Three habits keep it honest:

- New P0/P1 test → entry in the same PR. Enforceable via rule 7.
- Deleted test → `deprecated` with a reason, or removed with its gap reopened. Silent deletion is how coverage disappears without anyone deciding to drop it.
- Every production incident → a gap entry, then a regression test closing it. This is the highest-value habit in the entire skill: it makes the manifest a record of what actually hurt, and the suite grows along the axis of real failure instead of guessed risk.

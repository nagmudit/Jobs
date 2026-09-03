# Capability and Journey Risk Map

Ranking is the whole job. Get this wrong and the rest is busywork on the wrong code.

## Capabilities

A capability is something the product does for someone. Not a module, not a file.

Keep it to 8–20 for most repos. Beyond that you're listing functions.

```
CAP-AUTH   Sign in, session, password reset
CAP-PAY    Charge, refund, subscription lifecycle
CAP-DOC    Upload, parse, index documents
```

For each: what it does · who depends on it · what breaks downstream if it's wrong · whether it touches money, credentials, or personal data · current test coverage · confidence in that coverage.

## Priority

Assign by **cost of failure**, never by implementation complexity or ease of testing.

| | Failure means |
|---|---|
| **P0** | Data loss or corruption · wrong money movement · security or tenancy breach · core workflow down · unrecoverable state · regulatory breach |
| **P1** | Important workflow degraded, real users affected, but a workaround exists and blast radius is bounded |
| **P2** | Secondary behavior, usability, maintainability |
| **P3** | Cosmetic, rare combinations, or expensive to test for the value returned |

Two sharpeners:

**Is it silent?** A crash gets noticed in minutes. Wrong data written quietly for three weeks is discovered by a customer. Silent failures rank a level up.

**Is it reversible?** A bad read is a bug. A bad irreversible write — money sent, record deleted, email out — is an incident. Irreversible ranks up.

## Journeys

End-to-end paths with stable IDs (`CJ-001`). These, not modules, are what a test suite should protect first.

For each: actor · trigger · preconditions · success path · the alternate paths that matter · failure paths and recovery · external dependencies · data needed · what's covered today · release-blocking or not.

Keep it lean — six fields you'll maintain beat twenty you won't.

### User-facing

The obvious ones. Sign-up, checkout, the core loop, the admin action that touches everything.

### System-facing — where production actually breaks

Nobody clicks these, so nobody tests them, so they fail in the dark. Enumerate them explicitly:

- Webhook received: valid · duplicate · out of order · delayed past timeout · replayed after days · signature invalid
- Scheduled job: normal · overlapping run · run after downtime · partial failure mid-batch
- Retry and backoff: transient failure recovers · permanent failure gives up · dead-letter path
- Migration: forward · rollback · run twice · run against partially-migrated data
- Provider outage: timeout · rate limit · malformed response · fallback · recovery
- Token refresh: expiry mid-request · refresh race · revoked credential
- Reconciliation: our state vs. theirs disagreeing
- Backup restore — the one nobody has ever actually run
- Queue drain after downtime · redelivery · poison message

Any of these present in the repo and absent from the test suite is at minimum P1. Idempotency failures on webhooks and retries are the most common P0 in this whole list, and the cheapest to test.

### Cross-cutting scenario sets

Apply to any journey touching these. Reusable checklists, not new journeys:

**Permissions** — allowed actor · disallowed actor · unauthenticated · expired credential · another user's record · another tenant's record · privilege escalation · admin override logged

**State machines** — every valid transition · invalid transitions rejected · duplicate action · concurrent action on one entity · cancellation mid-flight · timeout · partial failure leaving consistent state

**Integrations** — success · validation error · provider 5xx · timeout · rate limit · malformed body · duplicate callback · out-of-order callback · auth failure · retry · fallback

## Coverage grading

For each P0/P1 journey ask one question: **would this test fail if the behavior broke?**

| Grade | Meaning |
|---|---|
| Covered | A test exercises it and would fail on regression |
| Weak | A test exists but asserts too little, or mocks the thing under test |
| Uncovered | Nothing, or only a smoke test that checks a 200 |

Weak coverage is more dangerous than none — it produces a green check that means nothing, and the team stops looking. Grade it weak and say exactly what's wrong.

## Output

| ID | Journey | Cap | P | Blast radius | Covered | Gap |
|---|---|---|---|---|---|---|
| CJ-003 | Stripe webhook → mark paid | CAP-PAY | P0 | double-charge, silent | weak — asserts 200 only | duplicate + out-of-order delivery |

Sort by priority. In a large monorepo, complete the P0/P1 rows fully, sample P2/P3, and state the sampling. Never summarize a critical gap out of view.

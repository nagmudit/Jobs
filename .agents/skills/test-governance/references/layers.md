# Choosing Test Layers

There are roughly sixty test types. Most repos need four. Recommending all of them produces a plan nobody starts.

## Decision table

Earn each layer with a signal from the repo.

| Layer | Add it when | Skip it when |
|---|---|---|
| Static / typecheck / lint | Always. Cheapest defect detection there is | — |
| Secret scanning | Always | — |
| Dependency CVE scan | Ships to users | Throwaway |
| Unit | Real logic: calculations, rules, parsing, state machines | Code that only wires libraries together |
| Integration (real DB) | A database or queue exists | No persistence |
| API / contract | Something else calls you, or you call something else | Single closed app |
| E2E journey | A UI or multi-step flow users depend on | Library or CLI |
| Permission / tenancy | Multi-user or multi-tenant | Single-user tool |
| Migration + rollback | Migrations exist | No schema |
| Idempotency / retry | Webhooks, queues, retries, payments | Pure request/response |
| Load / perf | Known traffic target or an SLO | No users yet |
| Accessibility | Public-facing UI, or any obligation | Internal tool, early |
| Visual regression | Design system, or brand-critical UI | Rapidly changing UI — churns constantly |
| Failure injection | Distributed, multiple providers | Monolith, one DB |
| AI evals | LLM output quality matters | — hand to `agent-harness` |

**The shape follows the risk, not a pyramid.** An API gateway is mostly integration and contract tests with few unit tests, and that's correct. A rules engine is the inverse. Don't impose a ratio.

## Per layer, decide four things

Everything else is detail you can settle while writing:

1. **What belongs here** — and explicitly what doesn't, or layers blur and everything migrates to slow E2E.
2. **Real or faked dependencies** — see below.
3. **When it runs** — every PR, main, nightly, release.
4. **Does it block a merge** — if not, be honest that it's advisory and will eventually be ignored.

## Dependency strategy

Default order, most preferred first:

1. **Real, in a container** — Postgres, Redis, the actual thing. Testcontainers or compose. Highest fidelity, and worth the seconds.
2. **Official emulator** — Stripe CLI, Firebase emulator, LocalStack.
3. **Recorded fixtures** — real captured responses replayed. Good until the provider changes and nothing tells you.
4. **Hand-written mocks** — last resort, at the process boundary only.

**Never mock your own code to test your own code.** That asserts the mock returns what the mock was configured to return. It's the single most common form of coverage that protects nothing.

Contract-test every third party you fake, so drift gets caught somewhere.

## Domain notes

Apply only what's present.

**Web UI** — test the journey, not the component tree. Semantic selectors, never CSS classes. Auto-waiting, never sleeps. Loading, empty, and error states are where the bugs are.

**API** — status codes, error body shape, auth on every route, pagination limits, input validation at the edge. Contract tests if anyone else consumes it.

**Database** — migrations forward and back, constraints actually enforced, transaction rollback on failure, N+1 on hot paths, unique constraints under concurrency.

**Payments** — idempotency keys, duplicate webhooks, refund and partial refund, currency and rounding in minor units, failed-charge recovery, reconciliation. Every path here is P0.

**Multi-tenant** — every query scoped, cross-tenant read attempts explicitly asserted to fail, tenant-scoped uniqueness. This is one test template repeated across resources, and it's the highest-value template in the repo.

**Background jobs** — idempotent reruns, partial batch failure, poison messages, concurrent workers on one item, restart mid-job.

**Data pipelines** — schema drift, nulls and outliers, duplicates, late data, backfill correctness, row-count and distribution assertions.

**Mobile** — permission denied paths, offline, backgrounding mid-flow, upgrade over existing data.

**Infrastructure** — plan-diff review, policy-as-code, secret absence, deploy then rollback.

**Real-time / voice** — reconnection, interruption, silence and timeout, partial input, concurrent sessions, latency budget.

## Anti-patterns

- Coverage percentage as a gate. Rewards testing trivial code and punishes deleting dead code.
- E2E for logic that a unit test covers in milliseconds — slow, flaky, poor failure messages.
- Snapshot tests over large structures. Everyone regenerates them without reading.
- Shared mutable fixtures across tests. Produces order dependence and the flakiness that kills trust in the suite.
- Asserting exact strings from anything nondeterministic.
- One test asserting fifteen things. First failure hides the rest.
- A `tests/` tree so slow nobody runs it locally. A suite people skip is a suite you don't have.

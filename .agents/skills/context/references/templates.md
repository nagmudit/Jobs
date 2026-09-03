# Document Templates

Shapes, not forms to fill in. Delete sections the repo can't fill with evidence.

## Frontmatter

On anything above Tier 1:

```yaml
---
status: current       # draft | current | proposed | generated | deprecated | historical
last_verified: 2026-08-10
applies_to: [src/api, src/domain]
---
```

`current` means checked against code on that date. `proposed` means it does not exist yet — the most important distinction in the whole system, because it's what stops an agent from building on fiction.

---

## `docs/index.md` — the routing map

Its job is letting an agent find the two relevant documents without reading twelve.

```md
# Documentation Index

Repository is the source of truth. Start here, read only what your task touches.

## Start
| Doc | Read it when | Status |
|-----|--------------|--------|
| [`../AGENTS.md`](../AGENTS.md) | Always, first | current |
| [architecture/repository-map.md](architecture/repository-map.md) | Finding where code lives | current |
| [engineering/commands.md](engineering/commands.md) | Running, testing, deploying | current |

## Working on...
| Area | Read |
|------|------|
| API endpoints | architecture/overview.md, engineering/conventions.md |
| Database changes | architecture/data-flow.md, generated/database-schema.md |
| Auth | architecture/security-model.md |

## In flight
[plans/active/](plans/active/) — read before touching an area with an open plan.

## Generated — do not edit
[generated/](generated/) — refresh with `<command>`.
```

---

## `docs/architecture/repository-map.md`

Explains *why* the structure is what it is. A file listing is `ls`; this is the part `ls` can't tell you.

```md
# Repository Map

## Shape
<One paragraph: monolith / monorepo / service split, and the organizing principle.>

## Directories
### `src/domain`
**Holds:** business rules and entities.
**Depends on:** nothing internal — deliberately.
**Rule:** no framework or DB imports. Enforced by <test/lint> or unenforced (risk).
**Entry:** `src/domain/index.ts`

## Where things live
| Looking for | Go to |
|-------------|-------|
| HTTP routes | `src/api/routes/` |
| Business rules | `src/domain/` |
| DB access | `src/repo/` |
| Migrations | `migrations/` |

## Boundaries
<Which module may depend on which. Note whether enforced or convention-only —
an unenforced rule is a debt entry.>

## Surprises
<Things that would mislead a newcomer: a legacy directory still in the build, a
misleading name, generated code checked in.>
```

That last section is usually the highest-value part of the file.

---

## `docs/engineering/commands.md`

Verified commands only. One unverified command teaches an agent to distrust the file.

```md
# Commands

| Task | Command | Verified |
|------|---------|----------|
| Install | `pnpm install` | ✅ package.json |
| Dev | `pnpm dev` | ✅ ran |
| Test | `pnpm test` | ✅ ran, 42 pass |
| E2E | `pnpm test:e2e` | ⚠️ needs Docker, not run |
| Deploy | `gh workflow run deploy` | ⚠️ from CI only |

## Prerequisites
<Runtime versions, services, required env vars — names only, never values.>

## Gotchas
<Migrations before first run; port conflicts; slow first build.>
```

---

## `docs/plans/active/<task>.md`

The handoff artifact. Another agent resumes from this alone.

```md
---
status: active
created: 2026-08-10
last_updated: 2026-08-10
areas: [src/billing]
---

# <Task>

## Objective
Outcome, not activity. What is true when this is done?

## Current behavior
Verified, with paths.

## Desired behavior

## Scope / out of scope

## Approach
Current plan. Update it when it changes — don't leave the original standing.

## Milestones
- [x] Schema
- [ ] Handler
- [ ] Tests

## Log
### 2026-08-10
- Did: <what>
- Found: <surprises — this section is why the file is worth keeping>
- Ran: `pnpm test` → 42 pass
- Blocked: <what and on whom>

## Decisions
**<Choice>** — because <reason>. Rejected <alternative> because <reason>.

## Open questions
Things that would change the implementation, with who can answer.

## Remaining
Exactly what the next agent does first.

## Done when
Observable conditions. Not "code written."
```

Update it **during** the work. A plan reconstructed at the end is a report, and reports don't survive a session ending mid-task — which is the entire scenario this file exists for.

Move to `completed/` only when every completion condition holds. Add the final validation output and any remaining limitations first.

---

## ADR — `docs/architecture/decisions/ADR-001-title.md`

Only for decisions that constrain future work. Not for every library choice.

```md
# ADR-001: <Decision>

**Status:** accepted · **Date:** 2026-08-10

## Context
The situation that forced a choice. Constraints that were real at the time.

## Decision

## Rationale

## Alternatives
What else was considered, and why not.

## Consequences
Both directions. What this makes harder is the part future readers need.

## Related
Code paths, other ADRs.
```

For decisions found in code with no recorded reasoning: document the observed decision, and state plainly that the original rationale could not be verified. Inventing a plausible rationale is worse than admitting the gap — it gets quoted later as fact.

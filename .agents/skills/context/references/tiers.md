# Tier Selection

The original framework specified ~40 files. Most repos that need context need four. This decides which.

## Signals

Gather these before choosing. They take about two minutes.

| Signal | How |
|---|---|
| Deployable units | Count things with their own entry point + deploy config |
| Source files | Excluding vendor, generated, lockfiles, build output |
| Contributors | `git shortlog -sn --since="12 months ago"` |
| Production evidence | CI workflows, deploy config, migrations, monitoring, error tracking |
| External integrations | Third-party APIs, payment, auth providers, queues |
| Sensitive data | PII, payments, health, anything regulated |

## Tiers

### Tier 0 — Single file

1 unit · < ~25 source files · no CI or deploy · solo · no external integrations

**Build:** `AGENTS.md` only.

Everything an agent needs fits on one page: what this is, how to run it, the two conventions that matter, what's unfinished. No `docs/`, no plans directory, no ledger.

Most prototypes, scripts, and weekend projects land here. Building Tier 2 on top of a Tier 0 repo is the single most common way this skill fails.

### Tier 1 — Core (default)

1–2 units · has CI **or** deploy config **or** more than one contributor

**Build:**

```
AGENTS.md                              contract + routing
CLAUDE.md                              @AGENTS.md pointer
docs/index.md                          what to read, when
docs/architecture/repository-map.md    where things live and why
docs/engineering/commands.md           verified run/test/build/deploy
docs/plans/active/                     what's in flight
```

Six paths. Answers the four questions that block a cold agent: *what is this, where is the code, how do I run it, what's already underway.* Stop here unless evidence says otherwise.

### Tier 2 — Full

3+ contributors **or** real users **or** 3+ external integrations **or** sensitive data **or** 2+ deployable units

**Adds:**

```
docs/product/vision.md, requirements.md, user-journeys.md
docs/architecture/overview.md, data-flow.md, integrations.md, security-model.md
docs/architecture/decisions/            ADRs, from here on
docs/engineering/conventions.md, testing.md
docs/plans/backlog.md, technical-debt.md
docs/runbooks/                          only for procedures actually performed
.github/copilot-instructions.md         if the team uses Copilot
```

Add `product/business-rules.md` and `glossary.md` only where domain vocabulary is genuinely ambiguous — insurance, healthcare, logistics, finance. Skip for a CRUD app.

### Tier 3 — Monorepo / scale

4+ deployable units **or** 10+ contributors **or** independently released packages

**Adds:**

```
apps/*/AGENTS.md, services/*/AGENTS.md   local scope only, never repeats root
docs/generated/                          derived: routes, schema, env vars
scripts/context/validate-context         links, dead paths, plan hygiene
scripts/context/refresh-generated-context
docs/engineering/dependency-rules.md     ideally an automated boundary test
```

At this size, generate rather than write anything derivable, and enforce rather than document anything critical. Wire `validate-context` into CI — a drift check nobody runs is decoration.

## Rules

**Between tiers, go lower.** Adding a document later costs minutes. A directory of confidently-wrong documents costs an agent's trust in all of them, including the accurate ones.

**Tiers describe ceilings, not quotas.** A Tier 2 repo with no external integrations does not get an `integrations.md`. Never create a file to complete a set.

**Nested `AGENTS.md` earns its place or doesn't exist.** One per deployable unit, at most, and only where local rules genuinely differ from root.

**Runbooks document procedures someone actually performs.** An incident-response runbook for a project with no users is fiction.

**Say the tier out loud.** Name it and the two or three signals behind it. If the user wants a higher tier than the evidence supports, build it and note the maintenance cost in one sentence — it's their repo.

## Growing later

Re-running `build` on an existing system upgrades it in place: keep accurate files, add the tier's missing ones, never regenerate what's still correct. Tier is a floor that rises, never a reset.

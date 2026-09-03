# AGENTS.md and Agent Adapters

`AGENTS.md` is the canonical entry point. Every other agent file points at it.

It is a **contract and a routing table**, not an encyclopedia. Target 60–120 lines. Past ~200 it stops being read carefully, which defeats its purpose. Detail belongs in `docs/`; this file says where.

## Template

Cut every section the repo doesn't need. A Tier 0 repo keeps roughly the first half.

```md
# <Project> — Agent Instructions

<Two or three sentences: what this product does, for whom.>

## Source of truth

This repository is authoritative. Conversation history is not. If you learned
something in chat that isn't written here, it does not survive this session —
write it down or lose it.

When sources disagree, trust in this order:

1. Code, tests, schemas, config — what actually runs
2. Accepted ADRs in `docs/architecture/decisions/`
3. Product specs and business rules
4. Active plans in `docs/plans/active/`
5. Engineering and operational docs
6. Everything else, including this file

Conflicts get investigated and recorded, never silently resolved in favor of
whichever is easiest.

## Before you start

1. Read `docs/index.md` and follow it to what's relevant — not everything.
2. Check `docs/plans/active/` for work already underway on this area.
3. Read the nearest nested `AGENTS.md` for directories you'll touch.
4. Read the actual implementation and its tests.
5. Run `git status`.
6. For anything substantial, write or update an execution plan first.

Never start significant work from the task description alone.

## Stack

<Language, framework, database, hosting, notable services. Facts, not history.>

## Layout

| Path | Holds |
|------|-------|
| `src/api` | HTTP handlers; no business logic |
| `src/domain` | Business rules; no framework imports |

<Only load-bearing directories. Full detail in docs/architecture/repository-map.md.>

## Commands

| Task | Command |
|------|---------|
| Install | `...` |
| Dev | `...` |
| Test | `...` |
| Lint / typecheck | `...` |
| Build | `...` |

Every command here has been run or found in CI. Mark unverified ones `# unverified`.

## Conventions

<Only rules a competent engineer would otherwise get wrong here. Observed from
the codebase, not imported from a style guide. Five to ten lines.>

## Before you finish

- Tests and lint pass — actually run, not assumed.
- Docs updated for anything that changed behavior, schema, config, or deps.
- Execution plan updated with what happened, what was decided, what's left.
- Report validation you did *not* run, and why.

## Never

- Commit secrets, `.env` values, or real customer data.
- Claim tests passed without running them.
- Mark work complete with stale docs behind it.
- Refactor beyond what the task requires.
```

## Rules for writing it

**Every line must change behavior.** "Write clean code," "follow best practices," "be careful" — cut. If removing a line would change nothing an agent does, it was noise, and it dilutes the lines that matter.

**Repo-specific over universal.** "Handlers never import from `src/db` — go through `src/repo`" is worth ten lines of generic architecture advice.

**Observed over aspirational.** Document what the codebase does. If the team wants a convention it doesn't yet follow, that's a technical-debt entry, not an instruction — instructions that contradict the code teach agents to ignore the file.

**Link, don't inline.** Anything longer than a few lines lives in `docs/` and gets a pointer.

## Adapters

One canonical file, thin pointers everywhere else. Competing sources of truth are worse than no documentation.

**`CLAUDE.md`** — imports the canonical file, adds only Claude-specific behavior:

```md
@AGENTS.md

# Claude Code

Follow `AGENTS.md`. Additionally:

- Use plan mode for cross-module or architectural changes.
- Prefer Grep/Glob over shell find/grep.
- Update the active execution plan before reporting completion.
```

**`.github/copilot-instructions.md`** — same shape. Copilot has no import syntax, so restate the three or four load-bearing rules and link to `AGENTS.md` for the rest.

**Cursor / Codex / others** — thin pointer, same rule.

Never copy the full contract into an adapter. When it drifts, and it will, nobody can tell which file is real.

## Nested AGENTS.md

For Tier 3, or any repo where local rules genuinely differ from root.

Include only: scope, local entry points, local conventions, local test/build commands, prohibited dependencies, domain-specific completion requirements.

Never repeat root content. Never create one per directory — one per deployable unit at most, and only where an agent would otherwise get something wrong.

```md
# services/billing — Agent Instructions

Extends root `AGENTS.md`.

**Scope:** invoicing, subscriptions, Stripe webhooks.

**Entry:** `src/server.ts` · webhooks in `src/webhooks/`

**Local rules**
- Money is integer minor units. Never float.
- Webhook handlers must be idempotent — Stripe retries.
- No imports from `services/crm`; go through the shared event bus.

**Validate:** `pnpm --filter billing test`
```

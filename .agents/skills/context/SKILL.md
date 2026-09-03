---
name: context
description: Build and maintain a vendor-neutral context system inside a repository so any coding agent — Claude Code, Cursor, Copilot, Codex — can pick up the work without being re-briefed. Use when the user says "set up AGENTS.md", "document this repo for AI agents", "I keep re-explaining my project", "I'm switching between coding agents", "context engineering", "my agent forgets everything", "our docs are out of date", or asks for architecture/decision/plan docs an agent can navigate. Three modes — audit (what context exists and what's stale), build (create it, sized to the repo), refresh (repair drift).
---

# Repository Context

Conversation history is not project memory. It dies with the session and it doesn't transfer between agents. The repository has to carry the context itself: what this product is, how it's built, what's in flight, and what was already decided.

This skill makes that true for a specific repo, at a size that repo can actually maintain.

**Not the same as `init`.** The built-in `init` skill writes a `CLAUDE.md` for Claude Code. This builds a vendor-neutral system — canonical `AGENTS.md`, navigable docs, execution plans, drift checks — that Cursor, Copilot, and Codex read equally. If the user just wants a quick `CLAUDE.md`, point them at `init` instead of building all this.

## Modes

| Mode | Trigger | Output |
|---|---|---|
| `audit` | context exists; "is our documentation accurate" | `docs/context/findings.jsonl` + verdict |
| `build` | little or no context; "set this up" | the context system, sized by tier |
| `refresh` | "update the docs", periodic maintenance | drift repaired, findings closed |

Default: `audit` if `AGENTS.md` or `docs/index.md` exists, otherwise `build`. Say which you chose in one line, then start.

## The governing rule

**Size the system to the repo, not to the template.**

The failure mode of every documentation framework is generating forty files for a project with six. Stale docs are worse than absent docs, because an agent trusts them. Pick a tier from `references/tiers.md` before writing anything, and when between two tiers pick the lower one.

---

## Mode: build

### Step 1 — Read the repo before writing about it

Never generate documentation from filenames. Inspect, in this order:

1. `README`, existing `AGENTS.md` / `CLAUDE.md` / `.cursorrules` / `.github/copilot-instructions.md`
2. Package manifests → stack, scripts, dependencies
3. Entry points → how it starts
4. Routes, schemas, migrations → what it does with data
5. Config and `.env.example` → what it connects to
6. Tests and CI → what "correct" means here
7. `git log --oneline -30` and `git status` → what's active

Cap at ~40 files. Sample by directory beyond that and record it under Limitations.

**Existing docs are evidence, not truth.** Check claims against code. A README describing a feature that no longer exists is a finding, not a source.

### Step 2 — Pick the tier

Score the repo against `references/tiers.md`. State the tier and the two or three signals that decided it. If the user asked for more than the evidence supports, build their tier and note the maintenance cost in one sentence.

### Step 3 — Write, evidence-first

Templates in `references/templates.md`; `AGENTS.md` rules and adapters in `references/agents-md.md`.

Non-negotiables:

- **Verify before asserting.** Never mark a requirement implemented without finding it in code. Never document a command without confirming it exists in package scripts, Makefile, or CI.
- **Label uncertainty inline.** "Unverified", "proposed", "unknown — check `services/billing`" are all acceptable content. Confident fabrication is not.
- **Preserve what's accurate.** Rewriting a correct doc to match a template destroys reviewed knowledge. Merge, don't replace.
- **Skip empty sections.** A heading with "TBD" underneath is noise. Omit it.
- **No secrets, no customer data.** Not in examples, not copied from `.env`.

Greenfield repos have no implementation to inspect. Document requirements and proposed architecture, mark every architectural claim `proposed`, and create the first execution plan — but never describe intended behavior in the present tense.

### Step 4 — Close the loop

For Tier 1 and above:

1. Create the execution plan for whatever the user is actually building (`docs/plans/active/`), or `docs/plans/completed/context-bootstrap.md` if this ran standalone.
2. Register every file created in `docs/index.md` with what it holds and when to read it.
3. Report: tier chosen, files created, files preserved, contradictions found, what stays unverified, and the exact next action.

---

## Mode: audit

Run the checks in `references/drift.md`. Record each failure as a finding in `docs/context/findings.jsonl`:

```json
{"id":"CTX-001","kind":"stale-command","severity":"P1","title":"docs/engineering/commands.md documents `npm run test:e2e`, not present in package.json","evidence":["docs/engineering/commands.md:31"],"fix":"Remove or correct; verify against package.json scripts","status":"open","first_seen":"2026-08-10"}
```

Re-runs diff against the existing ledger — match on `kind` + `title`, keep IDs, mark `fixed` / `regressed`. Report the delta, not a fresh essay.

Give a verdict a human can act on: **can a new agent enter this repo cold and start working correctly?** If no, name the one document blocking that.

Skip the ledger at Tier 0 — a single `AGENTS.md` doesn't need findings tracking.

---

## Mode: refresh

1. Read the ledger; re-verify open findings against current code before acting on them.
2. Fix mechanical drift directly: broken links, dead paths, unverified commands, duplicate ADR IDs, plans completed but still in `active/`.
3. Regenerate derived docs (routes, env vars, schema) from source.
4. **Escalate, don't guess.** Where docs and code disagree on intent — not on facts — record the contradiction and ask. Never rewrite documentation to match what might be a bug, and never change code to match stale docs.
5. Update `last_verified` only on what you actually checked.

---

## Composing with a real task

When invoked alongside a feature request, don't stop at documentation: build the tier-appropriate context, create the execution plan for the requested work, then do the work and write back into the docs before finishing. Context bootstrap is a prelude to the user's task, never a substitute for it.

## Guardrails

- Don't refactor code, rename modules, or install packages to fit the doc structure.
- Don't maintain competing sources of truth — `CLAUDE.md` and Copilot instructions are thin pointers to `AGENTS.md`, not copies.
- Don't claim a command passed unless it was run.
- Inspect `git status` first; never overwrite uncommitted work.

---
status: completed
created: 2026-09-04
last_updated: 2026-09-04
areas: [jobsearch/src/roles.py, jobsearch/src/relevance.py, jobsearch/src/sources, jobsearch/src/web]
---

# Role-driven fetch across all three sources

## Objective

One workflow: pick a role, live-fetch it from every enabled source. **Done.**

## What shipped

- `src/relevance.py` — local role matching on title + categories, word-boundary safe.
- `src/roles.py` — `fetch_role`, dispatching per source and reporting `filter_mode`.
- `role_map` in `targets.yaml` — per-role query tokens and keywords, tags validated live.
- `python -m src.cli fetch --roles a,b [--sources ...]`.
- `POST /api/fetch` + a unified role picker in the UI, each source labelled with how it
  filters.
- RemoteOK `parse` bug fixed; local filtering and `filtered_out` accounting in both API
  sources; provenance now records the role across all sources.

## Log

### 2026-09-04
- Probed all three sources before designing. **Himalayas ignores every filter parameter**
  (`search=engineer` → *Grants Coordinator*, *Account Executive*). **RemoteOK's `?tag=`
  is real but unusable**: low relevance (`mobile` 1%, `react` 5%) and its tag endpoints
  serve an archive (median age 112–144 days vs 5 days for the live feed).
- Found and fixed three bugs, all silent:
  1. `remoteok.parse` raised `SchemaDrift` on a legitimately empty filtered result —
     would have aborted healthy ingests. Regression test written first.
  2. Matching on RemoteOK's own tags put *Kitchen Technician* and *Plant Fitter* under
     `software-engineer` / `mobile-engineer`. Title-only for that source now.
  3. Selecting tags by result count picked the worst options — `?tag=ml` returns 98 jobs,
     **none** of them ML. Corrected to relevance, then abandoned tags entirely for role
     fetch once the archive behaviour showed up.
- Ran: `python -m pytest tests -q` → **108 passed**. Verified the new guards fail when
  broken (word-boundary matching, Himalayas predicate, no-tag rule), then restored.
- Ran live: `fetch --roles artificial-intelligence-engineer,machine-learning-engineer`
  → wellfound 868 / himalayas 42 of 500 / remoteok 4 of 100, **0 mitigations**.
- UI verified in a real browser: no JS errors, per-source filter labels correct.

## Decisions

**Role fetch sends no RemoteOK tag** — the live feed plus a local filter beats a stale
tag endpoint under the 30-day cutoff. Tags stay in `role_map`, annotated, for mining
older postings deliberately.

**`filter_mode` is surfaced everywhere** — CLI, API and UI. Only Wellfound is `server`.

## Remaining

- `GAP-016`: pre-ADR-009 RemoteOK rows were never role-filtered and still sit in the
  corpus with provenance `all`/`dev`/`engineer`. Needs a deliberate prune or re-fetch.
- `GAP-017`: HTML entities double-escaped in UI titles.
- Deep-paging Himalayas to a match target — deferred by decision.

## Done when

- [x] `fetch` works from CLI and UI across all three sources
- [x] Every result reports how it was filtered
- [x] Suite green, new guards verified failable
- [x] ADR-009, AGENTS.md, manifest updated

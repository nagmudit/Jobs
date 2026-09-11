---
status: active
created: 2026-09-09
last_updated: 2026-09-11
areas: [jobsearch/src/store.py, jobsearch/src/cli.py, jobsearch/src/web]
---

# Application analytics

## Objective

Know what was applied to and when, how many this week, and how those applications
turned out. **Small on purpose** — a handful of numbers, no charts.

## The problem it solves

`user_state` holds one **mutable** row per job. `set_status` overwrote `status`
and `updated_at` in place, so marking a job `rejected` on the 20th destroyed the
fact that it was applied to on the 1st. "What did I apply to last week" was
unanswerable, not merely unimplemented.

## What changed

**`status_event`** — an append-only log of every status change. `set_status`
remains the single writer of both tables: it upserts `user_state` exactly as
before, so every existing filter, facet and view is untouched, and appends an
event. Everything else here is a query over that.

**Three statuses added**: `interviewing`, `offer`, `rejected`, giving the funnel
`shortlisted → applied → interviewing → offer | rejected`. Rounds go in `note`;
there is no per-round tracking by design.

**`SCHEMA_VERSION` 2 → 3**, with a `migrate()` step seeding one event per
existing mark from its `updated_at`, guarded on absence so reconnects do not
duplicate.

## The four consequences that carried the real risk

1. **`export` clears the log.** The corpus is published to a public repo daily; a
   dated record of every job applied to must never leave the machine. Verified on
   the real corpus with plain `sqlite3`: 0 events in the published file.
2. **`sync` carries the log across.** Unlike a mark, an event cannot be recreated
   by clicking the button again.
3. **`INVESTED_STATUSES` gained `interviewing` and `offer`**, so `prune` and
   `carry_forward_marked` never drop a job under active interview.
4. **`import_user_state` replays with `record=False`.** Found by a failing test:
   replaying marks through `set_status` appended a *second* set of events dated
   today, so every sync silently re-dated the history it was meant to preserve.

## Verification

- `python -m pytest tests -q` → **333 passed**, three consecutive runs.
- `tests/test_application_analytics.py`, 19 tests. Five mutants each caught by the
  right test: history not recorded, export not stripping, sync not carrying,
  counting `user_state` instead of events, and `interviewing` dropped from
  `INVESTED_STATUSES`.
- End-to-end on a copy of the real 6,342-job corpus: applied date survived a later
  rejection, published copy clean, sync restored all 8 events.

**A flaky test caught before it shipped.** `median_days_to_reply` used
`CAST(... AS INTEGER)`, which truncates: a reply after 9.99999 days reported 9.
The test passed alone and failed in the full suite, because the gap depended on
how long the run took. The metric now rounds (a reply after 1.9 days is 2, not 1)
and the test pins both timestamps instead of one.

## Deliberately not built

Charts, per-round tracking, reminders or follow-up nudges, goals, CSV export,
company-level CRM. If a number needs a chart to be read, it is the wrong number
for this tool.

## 2026-09-11 — the capture problem, and the page

Marking status per row was never going to be used: it needs a trip back and a
hunt for the row. **The Apply click is now the trigger.** It records `opened`
(not `applied` — opening is not applying), and a tray at the top asks "did you
apply?" so the row never has to be found. Answering yes dates the application at
the **open** time, so confirming on Thursday something opened on Monday does not
move it into the wrong week. Applications with no outcome after
`followup_days` (10) get "any news?" in the same tray; "Not yet" snoozes.

`opened`, `skipped` and `nudged` are event-only — never in `user_state`, so every
filter, facet and the funnel are untouched. `/api/event` refuses funnel statuses
so an application can never be logged without also setting current state.

Row controls went from six to three (Apply · Shortlist · Hide); outcomes are
answered in the tray where the context already is.

**The page**, against the repo's `ui-ux-pro-max` skill:

- Emoji controls (☎ 🏆 ✖, added 2026-09-09) replaced with an inline SVG sprite —
  the skill's priority-4 anti-pattern. Zero emoji remain.
- First focus rings on the page; `prefers-reduced-motion`; `scroll-margin-top` so
  the sticky header cannot hide a focused row (WCAG 2.2 focus-not-obscured).
- Body 13px → 14px/1.5; `--dim` raised to clear 4.5:1 in both themes.
- **A real bug fixed:** `aside` and `th` hardcoded `top:41px`. Adding the stats
  row on 2026-09-09 made the header taller, so the sticky column heads had been
  sitting *under* it since. Now measured into `--hdr` on load and resize.
- Stat values use proportional figures, not `tabular-nums` — per the `dataviz`
  skill, equal-width digits make a standalone number look loose; tabular is for
  columns that align.

Not taken from the skill, deliberately: its `--design-system` pattern was
"Enterprise Gateway" (mega menu, client logos, Contact Sales) — a B2B landing
pattern with nothing to do with a single-user tool. Its Google-Fonts suggestion
was dropped too; this page is served locally and from a cold-start function.

## Remaining

- Numbers are whole-corpus; they do not respect the active filters. Deliberate for
  now — "how many did I apply to this week" should not change when a facet is
  clicked. Revisit if it turns out to be wanted.
- No per-source response *rate* (only application counts by source). Needs more
  data before it would mean anything.
- **Visual rendering and screen-reader behaviour are unverified.** The checklist
  was audited against the source, not against a browser or a real AT.
- The tray polls only on load and after an answer. If the page is left open for
  days, a follow-up that becomes due will not appear until a reload.

## Done when

- [x] Applied date survives an outcome recorded later
- [x] 7d / 30d / all-time counts, funnel, response rate, median time to reply
- [x] History never published
- [x] History survives a corpus refresh
- [x] Suite green, every guard verified failable
- [ ] Used for a week, then decide whether any number is missing or noise

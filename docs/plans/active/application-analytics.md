---
status: active
created: 2026-09-09
last_updated: 2026-09-09
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

## Remaining

- Numbers are whole-corpus; they do not respect the active filters. Deliberate for
  now — "how many did I apply to this week" should not change when a facet is
  clicked. Revisit if it turns out to be wanted.
- No per-source response *rate* (only application counts by source). Needs more
  data before it would mean anything.

## Done when

- [x] Applied date survives an outcome recorded later
- [x] 7d / 30d / all-time counts, funnel, response rate, median time to reply
- [x] History never published
- [x] History survives a corpus refresh
- [x] Suite green, every guard verified failable
- [ ] Used for a week, then decide whether any number is missing or noise

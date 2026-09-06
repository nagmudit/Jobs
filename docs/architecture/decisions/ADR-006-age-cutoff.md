# ADR-006: A 30-day age cutoff, applied at ingest — but never as an early stop

**Status:** accepted · **Date:** 2026-09-04

## Context

Wellfound serves very old listings as current search results. Measured on a
2,941-job corpus: 61% were over 30 days old, 25% over 90 days, and the oldest was
**3,186 days** (~8.7 years). The detail page confirms it in Wellfound's own words —
*"Posted: 8 years ago"* — and the listing still returns HTTP 200 and renders normally.

The tool's promise is working apply links. A corpus where most rows are probably dead
does not deliver that.

## Decision

A `max_age_days` cutoff, default **30**, configured in `targets.yaml` and overridable
with `--max-age-days` (0 disables it). Jobs older than the cutoff are **not ingested,
not enriched, and not shown**.

The cutoff is applied **per job at ingest**. It explicitly does **not** terminate a
slice early.

## Rationale — the part that constrains everything

**Wellfound does not order search results by date.** Measured 2026-09-04 across a
20-page slice:

| page | min age | max age |
|---|---|---|
| 3 | 2d | 1721d |
| 6 | 1d | 116d |
| 8 | 2d | 44d |
| 20 | 3d | 1689d |

Fresh and ancient jobs are mixed on every page. A live `mobile-engineer` crawl kept
only 5 of 35 jobs on page 6, then 12 of 32 on page 7 and 9 of 27 on page 10.

So an early stop on age — the obvious optimisation, and what "don't crawl old jobs"
sounds like it should mean — would **silently discard fresh jobs sitting deeper in the
results**. That is precisely the silent-wrongness class this crawler exists to prevent
(ADR-003), so it is prohibited, and there is a test named
`test_all_stale_page_does_not_end_the_slice` holding the line.

**The cutoff therefore saves no requests.** Every page is still fetched. What it buys
is corpus quality, and skipped detail fetches during enrichment (one request each, so
that saving is real).

## Alternatives

- **Early slice termination on a stale page** — rejected on the evidence above.
- **Filter only at display time** — simpler, but the corpus keeps growing with rows
  that will never be shown, and `enrich` would still spend requests on them.
- **Drop the cutoff, sort by date instead** — the default sort is already newest-first,
  but with 61% stale the user still pages through junk to find anything.

## Consequences

- **Ingest is now lossy relative to what was fetched.** This is a deliberate exception
  to ADR-002's "never discard what you'd have to re-crawl", and it is safe only because
  the raw pages remain in `cache/`: a wider cutoff is re-ingested with
  `crawl --no-resume --max-age-days 90` and **no network**.
- A job with no `liveStartAt` is **kept**. Unknown age is not old, and dropping it would
  silently lose data on a schema change.
- A company is only stored once one of its jobs survives the cutoff.
- `slice_stats` gained `stale_skipped`, and `recovery_ratio` is computed on
  `kept + stale` — the number that means "how much did the page cap let us reach",
  which is what the truncation panel is for. Conflating the cutoff with the cap made
  that number unreadable.
- Existing corpora are not touched automatically. `python -m src.cli prune` reports
  what it would remove; `--apply` deletes. `user_state` is never deleted, so a job you
  marked applied keeps that mark if it returns on a later crawl.
- Adding the column needed a real migration (`store.migrate`), since
  `CREATE TABLE IF NOT EXISTS` does not alter an existing table.

## Addendum — 2026-09-06: the published corpus IS pruned automatically

This amends, and does not replace, the consequence above that reads *"Existing corpora
are not touched automatically."*

**What changed.** The tool now publishes a corpus daily for a hosted read-only UI
(`docs/engineering/deployment.md`). Vercel's serverless bundle limit is 250 MB and the
corpus grows every day — 59.7 MB at the time of writing, with 1,883 of 6,266 jobs
already past the cutoff. The daily workflow therefore runs `prune --apply` before
publishing.

**What is genuinely weaker.** This decision's safety argument was that a prune is
recoverable with no network because the raw pages stay in `cache/`. **A CI runner has
no `cache/`.** A prune there is irreversible for those rows. That is acceptable only
because the published corpus is a *derived artifact* — the source of truth for a
listing is the platform, and a job 30 days dead is not worth a byte of a 250 MB budget.

**What is unchanged.**

- The user's local corpus is still never pruned automatically. `prune` remains dry-run
  by default and is invoked by hand.
- Unknown-age jobs are still kept. `days_old` is NULL without a `posted_ts`, and
  `NULL > 30` is NULL.
- `user_state` is still never deleted.

**What this required.** `prune_stale` gained `keep_marked=True`: a job marked `applied`
or `shortlisted` is never pruned, because its listing is the only record of what the
user applied to. `hidden` is deliberately not protected — it means "stop showing me
this", and the mark survives in `user_state` regardless.

That protects a *local* prune. The published corpus carries no marks at all (`cmd_export`
strips them), so its prune cannot know about them — and adopting it would otherwise
delete an applied job out from under the user. `cmd_sync` therefore calls
`store.carry_forward_marked`, which copies those rows back from the corpus being
replaced.

**Note.** `VACUUM` is what actually reclaims the space; SQLite does not return deleted
pages to the OS. Measured: deleting 1,883 stale jobs left the file at 59.7 MB, and
`VACUUM` took it to 46.0 MB. `cmd_export` vacuums.

## Related

`jobsearch/src/config.py::cutoff_ts` · `jobsearch/src/crawl.py::crawl_slice` ·
`jobsearch/src/store.py::prune_stale` · `jobsearch/src/store.py::carry_forward_marked` ·
`jobsearch/tests/test_age_cutoff.py` · `jobsearch/tests/test_hosted.py` ·
`docs/engineering/deployment.md` · ADR-003

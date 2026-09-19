---
status: active
created: 2026-09-18
last_updated: 2026-09-18
areas: [jobsearch/src/fetch.py, jobsearch/src/roles.py, jobsearch/src/cli.py, jobsearch/src/web/app.py, jobsearch/src/web/static/index.html, .github/workflows/daily-fetch.yml, jobsearch/tests]
---

# Scope a block to its host, not the whole run

## Objective

When one origin refuses us (a mitigation, or an unreadable robots.txt), that
origin gets no further request for the rest of the run and every other origin
carries on. The run still fails loudly. Design and rationale are in
[ADR-016](../../architecture/decisions/ADR-016-per-host-halt.md).

Done means: the 2026-09-18 failure replays in a test as "himalayas blocked, every
other source fetched, exit 3"; a blocked host is shown to receive zero requests
afterwards, across roles; the suite is green; every new guard has been seen to
fail.

## Current behavior

Verified 2026-09-18 by reading the code and the failing run's log:

- `Fetcher._load_robots` raises a bare `RuntimeError` for an unreadable robots.txt.
  `cmd_fetch` catches only `CorpusIntegrityError`, so this one escapes as a
  traceback, exit 1 (the daily run's failure).
- `MitigationDetected` is a `CorpusIntegrityError`, so `cmd_fetch` catches it
  and returns 2. Either way the remaining sources and roles never run.
- `fetch_role` has no per-source error handling.
- The robots.txt 403 is written to the disk cache, and `_is_mitigation` makes it
  sticky forever. Locally the host is refused on every later run until the entry
  is deleted. CI starts with no cache, so every daily run re-requests robots.txt
  once.
- `daily-fetch.yml` publishes the corpus on `always()` but triggers the Vercel
  deploy only when the fetch step succeeded.

## Changes

1. **`src/fetch.py`**
   - `class HostBlocked(RuntimeError)` carrying `origin` and `reason`.
   - `class RobotsUnreadable(RuntimeError)`, raised where the bare
     `RuntimeError` is today, with the same message, so the existing
     `match="refusing to crawl blind"` tests keep passing.
   - `Fetcher.blocked: dict[str, str]`, keyed by origin. `get()` checks it
     **first**, before robots, throttle, cache or transport, and raises
     `HostBlocked` with no request.
   - `get()` records the origin when `RobotsUnreadable` or `MitigationDetected`
     comes out of it, then re-raises the original exception unchanged.
     `on_response` still runs before that, so `request_log` keeps its evidence.
2. **`src/roles.py`**: `fetch_role` wraps each source in `try/except (HostBlocked,
   RobotsUnreadable, MitigationDetected)` and appends
   `_blocked(source, role, origin, reason)` (`filter_mode: "blocked"`).
   `describe` prints `blocked: <origin> (<reason>)`. Other
   `CorpusIntegrityError`s still propagate.
3. **`src/cli.py`**
   - `cmd_fetch` gathers `f.blocked` after all roles and prints a `BLOCKED` summary.
     It returns **3** when anything was blocked. Integrity halts stay at 2.
   - `cmd_ingest` gets the same per-source continue.
4. **`src/web/app.py`**: `_run_fetch` / `_run_ingest` expose `state["blocked"]`.
   `index.html` shows blocked hosts in the status box in the warning colour,
   separate from `halted:`.
5. **`.github/workflows/daily-fetch.yml`**
   - The fetch step records its exit code as an output. It fails at once on
     anything other than 0 or 3.
   - The deploy runs on 0 or 3.
   - A final step fails the job on 3, so GitHub still emails.
   - The step summary lists the blocked hosts.
6. **Docs**
   - ADR-016 → accepted. Cross-link it from ADR-003 and ADR-007.
   - Reword the `AGENTS.md` conduct bullet, "Halt on the first mitigation".
   - Add the new exit code to `docs/engineering/commands.md`.
   - Add ADR-016 to `docs/index.md`.
   - Update the manifest: CJ-002 is renamed to the host-scoped wording, and a new P0
     journey covers "a blocked host gets no further request".

## Tests (offline; fakes only at `httpx.Client`)

Write each one first and watch it fail.

- **Mitigation:** a 403 on origin A, then any URL on A → `HostBlocked`, and
  `Client.calls` does not grow (no robots.txt, no page).
- **Unreadable robots:** the same, when the refusal is A's robots.txt.
- **Other hosts:** origin B still fetches normally after A is blocked, and B's
  robots.txt is still read and enforced.
- **Evidence:** `on_response` still receives the refusing response before the
  raise.
- **Across roles:** `fetch_role` over two sources, one blocked → the other's rows
  are stored and the result says `blocked` for the first. A second role on the same
  `Fetcher` sends that host zero requests.
- **Integrity halts:** a `SchemaDrift` (or `SilentRoleFallback`) from a source
  still propagates out of `fetch_role`.
- **Exit codes:** `cmd_fetch` returns 3 with a blocked host and prints it; 2 on
  an integrity halt; 0 when clean.
- **Existing tests to re-read, not weaken:**
  - `test_get_halts_on_a_live_mitigation` is unchanged: it still raises.
  - `test_unreadable_robots_*` keep their `RuntimeError` match, since
    `RobotsUnreadable` subclasses it.

## Out of scope

- Transport failures (timeouts, DNS) on ordinary requests. They still end the run
  as today. Worth a separate decision, since a timeout is not a refusal.
- `crawl` and `enrich`: each is a single host, so a host block already halts them.
- Clearing a sticky cached refusal automatically. That stays a human act (ADR-011).
- The Himalayas block itself. With this in place it costs Himalayas' jobs only.
  Whether to disable the source is a separate call once it has persisted a few days.

## Risks

- **A caller that catches too broadly** could turn a block into a silent skip.
  The `HostBlocked` check in `get()` holds regardless: whatever the caller does,
  the host is not contacted again.
- **A shared origin across sources.** All Greenhouse boards sit on one API host,
  so a Greenhouse block skips every Greenhouse company for the run. That is
  intended: the unit of refusal is the host.
- **Exit code 3 in other automation.** Nothing but the daily workflow reads it
  today (checked by grep before landing).

## Log

- 2026-09-18: written after the daily run failed on `himalayas.app` robots.txt
  403. Approved in plan mode the same day.
- 2026-09-18: implemented as above. Deviations:
  - **Workflow:** the fetch step exits 0 on rc 3 itself, after writing `rc` and the
    summary, so the existing deploy condition (`steps.fetch.outcome == 'success'`)
    didn't need to change. A final step fails the job when `rc == '3'`. The step's
    shell logic was dry-run locally under `bash -eo pipefail` for rc 0, 3 and 2.
  - **Added:** `tests/test_host_block.py` is in `ci.yml`'s named "Conduct guards"
    step.
  - `_blocked()` takes the exception rather than origin and reason separately; its
    message names the origin.
- 2026-09-19: **first live run under ADR-016 behaved as designed.** Scheduled run
  35443320514: `himalayas.app` answered 403 to robots.txt again. The other six
  sources were fetched; the corpus was published and deployed; only the final
  "Fail if any host was blocked" step went red (exit 1). 0 mitigations elsewhere.
  **Himalayas is intermittent from GitHub runners, not banned:**

  | Run | Trigger (UTC) | Himalayas |
  |---|---|---|
  | 2026-09-17 13:39 | schedule | fetched |
  | 2026-09-18 13:03 | schedule | robots 403 (this plan's trigger) |
  | 2026-09-18 19:51 | manual | fetched |
  | 2026-09-19 12:36 | schedule | robots 403 |

  Both refusals came at the scheduled time and both successes did not, but two
  data points are not a pattern. The likelier cause is Himalayas' edge refusing some
  runner IP ranges. Nothing to change in code. **Not** to be worked around (no
  proxy, no retry), per the conduct rules. If it keeps failing: disable
  `himalayas` for CI only and fetch it locally, or move the schedule. That is a
  user decision.
- 2026-09-19: **user chose to vary the schedule.** From 2026-09-20 the daily fetch
  triggers hourly, and `scripts/daily_gate.py` picks each day's start time
  (00:00–20:00 UTC, a different time daily), with at most one fetch attempt per
  UTC day (`tests/test_daily_gate.py`, both properties verified failable). See
  `docs/engineering/deployment.md` → Timing. **Next:** after about two weeks, compare
  Himalayas' outcomes against start times. If refusals don't track the time of
  day, it is the runner's IP address, and the remaining choice is to fetch
  Himalayas from a local machine only.

## Validation

- `python -m pytest tests -q` → `390 passed in 20.55s`
- 17 new tests in `tests/test_host_block.py`, written first: 11 red before the
  change (4 pin behaviour that must not change: evidence logging, integrity halt,
  exit 0, exit 2).
- Broken one at a time, each went red: `get()` pre-check removed (5 failed);
  record-on-raise removed (10); `fetch_role` catching all `CorpusIntegrityError`
  (2); web ingest without the catch (1); `cmd_fetch` returning 0 (1).
  - The cross-role zero-requests test did **not** fail without the pre-check: the
    robots.txt 403 is also cached as sticky, so no request goes out either way. The
    pre-check is pinned by the Fetcher-level tests, which use a fresh URL on the
    same origin.
- Existing conduct tests unchanged and passing (`test_get_halts_on_a_live_mitigation`,
  `test_unreadable_robots_*`).
- `validate_manifest.py` → `61 journeys, 62 tests, 7 open gaps, 0 warning(s)`;
  `validate_context.py` → `34 docs, 0 warning(s)`
- **Not run:** the real daily workflow, which needs a push and a live run against
  the third-party hosts. Expected next run: Himalayas listed under Blocked hosts,
  every other source fetched, corpus published and deployed, job red.

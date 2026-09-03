# Jobs — Agent Instructions

A personal job-search corpus tool. `jobsearch/` crawls Wellfound listings for
configurable roles and locations into SQLite and serves a local filter/sort UI with
apply links. `wellfound-probe/` is the completed research that established *how* to
read Wellfound at all — frozen evidence, not a live component.

There is no scorer, recommender, or auto-apply here, by design. The user filters; the
tool fetches and displays.

## Source of truth

This repository is authoritative. Conversation history is not. If you learned
something in chat that isn't written here, it does not survive this session — write it
down or lose it.

When sources disagree, trust in this order:

1. Code, tests, schemas, config — what actually runs
2. Accepted ADRs in `docs/architecture/decisions/`
3. Active plans in `docs/plans/active/`
4. Engineering docs under `docs/`
5. `wellfound-probe/REPORT.md` — accurate as of 2026-09-02, describes a third-party
   site that can change under us
6. Everything else, including this file

Conflicts get investigated and recorded, never silently resolved in favour of
whichever is easiest.

## Before you start

1. Read `docs/index.md` and follow it to what your task touches — not everything.
2. Check `docs/plans/active/` for work already underway in that area.
3. Read the implementation and its tests.
4. Run `git status`.
5. For anything substantial, write or update an execution plan first.

## Stack

Python 3.11+ (developed on 3.14). `httpx`, `beautifulsoup4`, `lxml`, `pyyaml`,
`fastapi`, `uvicorn`, `pytest`. SQLite (stdlib) with a WAL journal. No build step, no
CI, no deploy, no container. `wellfound-probe/` additionally uses `playwright` for one
probe module.

## Layout

| Path | Holds |
|------|-------|
| `jobsearch/src/` | The tool. See `docs/architecture/repository-map.md` |
| `jobsearch/src/web/` | FastAPI app + one static HTML page, no build step |
| `jobsearch/tests/` | Offline tests against synthetic fixtures |
| `jobsearch/targets.yaml` | Roles, locations, conduct settings — **all** crawl config |
| `wellfound-probe/` | Frozen research. Read `REPORT.md`, don't extend the code |
| `quality/test-manifest.yaml` | What matters vs. what protects it |

## Commands

Run from `jobsearch/` unless noted. Full list in `docs/engineering/commands.md`.

| Task | Command |
|------|---------|
| Install | `pip install httpx beautifulsoup4 lxml pyyaml fastapi uvicorn pytest` |
| Test | `python -m pytest tests -q` |
| Crawl | `python -m src.cli crawl` |
| Serve | `python -m src.cli serve` |
| Stats | `python -m src.cli stats` |
| Prune old jobs | `python -m src.cli prune` (add `--apply` to delete) |

All verified 2026-09-03. There is no lint or typecheck configured — don't claim one ran.

## Conduct rules — these are not style preferences

This tool talks to a live third-party site that has no API and no agreement with us.
Violating any of these can get the user's IP or account blocked, which breaks the tool
for everyone using it.

- **`robots.txt` is enforced in code**, in `src/fetch.py`. `/_jobs/` is disallowed.
  Re-verify before crawling any new path family; don't assume.
- **3–5 s between requests, concurrency 1.** `src/config.py` rejects a `delay_range`
  floor below 3.0 s at load time. Do not remove that guard.
- **No anti-bot evasion, ever.** No proxies, no CAPTCHA services, no stealth plugins,
  no fingerprint spoofing, no UA spoofing. The User-Agent is honest and carries a
  contact address. If something only works by defeating a challenge, it does not go in
  this repo.
- **Halt on the first mitigation.** `cf-ray` and `cf-mitigated` are logged for every
  response. A non-`None` mitigation or a 403/429 raises `MitigationDetected` and stops
  the crawl. Never retry through it, never add a backoff-and-continue path.
- All network access goes through `Fetcher.get`. Adding a second path out to the
  network bypasses every rule above.

## Conventions

- **Store raw, derive later.** Apollo nodes go into `job_raw.raw_json` verbatim.
  Filterable columns are derived in the `jobs` view via `json_extract` plus the SQL
  functions in `src/derive.py`. Never normalise at ingest — re-deriving is free,
  re-crawling is not. See ADR-002.
- **The four assertions raise, never warn.** `SilentRoleFallback`, `PageWrap`,
  `YieldFloor`, `SchemaDrift` all guard failures that return HTTP 200 with plausible
  data. Downgrading one to a log line silently corrupts the corpus. See ADR-003.
- **Nothing about roles or locations is hardcoded.** They live in `targets.yaml`,
  overridable with `--roles` / `--locations`. Adding a literal role slug to `src/` is
  a bug. The web API refuses any slug not listed in `targets.yaml`.
- **Three URL shapes, one function.** `search_url` maps location `anywhere` ->
  `/role/{slug}` (widest, the default), `remote` -> `/role/r/{slug}`, and any other
  token -> `/role/l/{slug}/{token}`. All three are verified live and tested.
- **"Location" means two different things; do not conflate them.** A *slice token*
  (`anywhere`/`remote`/a city) is what we asked Wellfound to pre-filter on, recorded
  in `job_provenance.location`. A *place* (Pune, San Francisco) comes from
  `locationNames` and lives in `job_location`. The UI filters on places. See ADR-005.
- **The age cutoff never ends a slice early.** `max_age_days` (default 30) filters
  jobs at ingest, in enrichment, and in the UI. It must NOT stop paging: Wellfound
  does not order results by date, so a page of only-stale jobs is followed by pages
  with fresh ones. Early termination would silently lose them. See ADR-006.
- **Unknown age is not old.** A job with no `liveStartAt` is kept by the cutoff.
- **`rebuild_locations()` after any crawl.** `job_location` is derived from
  `job_raw`; the crawl paths call it, and a new derivation never needs a re-crawl.
- **FastAPI request models stay at module level** in `src/web/app.py`. With
  `from __future__ import annotations`, a model defined inside `create_app()` is
  invisible to FastAPI's hint resolution and the body silently degrades into a
  required query param (HTTP 422).
- **Wellfound sends `''`, not `null`,** for absent values. The `jobs` view wraps them
  in `NULLIF`. Skipping that makes every "has salary" filter and fill-rate wrong.
- **Unparsed beats wrongly parsed.** `src/derive.py` returns `NULL` for anything it
  doesn't recognise and always keeps the raw string alongside.
- `user_state` is keyed separately from `job_raw` so shortlist/applied/hidden survive
  re-crawls that rewrite job rows. Don't merge those tables.

## Testing contract

- New P0/P1 behaviour → a `quality/test-manifest.yaml` entry in the same change.
- Bug fix → write the failing test first, watch it fail, then fix.
- **Verify a test can fail.** Break the behaviour, see red, restore it.
- Never assert on mocks of our own code. Fakes belong at the network boundary only.
- Tests must not hit the network. Fixtures live in `tests/fixtures.py`.
- State one of: tests added (what), existing tests cover it (which), or no test needed
  (why). An unstated decision is not acceptable.
- Never claim the suite passed without running it — paste the output.

## Before you finish

- Tests actually run, not assumed.
- Docs updated for anything that changed behaviour, schema, config, or conduct rules.
- Active execution plan updated with what happened and what's left.
- Report validation you did *not* run, and why.

## Never

- Commit secrets or `.env` values. The `user_agent` in `targets.yaml` carries a real
  email — that is deliberate and public-facing, but don't add anything else personal.
- Commit `jobs.db` or `cache/` (both gitignored). They are large and regenerable.
- Claim tests passed without running them.
- Weaken an assertion or a conduct guard to make something pass.
- Extend `wellfound-probe/` — it is a finished artifact. New work goes in `jobsearch/`.

---
status: completed
created: 2026-09-06
last_updated: 2026-09-06
areas: [api, jobsearch/src/hosted.py, jobsearch/src/cli.py, jobsearch/src/web, .github/workflows]
---

# Free hosting: GitHub Actions crawls, Vercel serves

## Objective

A daily 14:00 fetch without the user's PC, at no cost. **Code done and tested. The two
hosted halves are unverified** — no real Actions schedule has run and no Vercel deploy
has happened.

## Why this shape

The user declined a ~$5/mo VPS and chose Actions + Vercel free. The split is coherent:
Vercel cannot crawl (75–180 min against serverless timeouts, read-only FS) but can
serve; Actions has no such limit.

Two constraints did the real design work:

**The corpus must stay a SQLite file.** The `jobs` view calls 8 Python functions
registered per-connection (`derive.py::register`). Turso, Neon and Supabase execute SQL
server-side where those do not exist, so every query against `jobs` would fail with "no
such function". So: Actions rebuilds the file daily, publishes it as a release asset,
Vercel bakes it into the bundle at build time.

**Marks stay on the user's machine.** A hosted KV was built first, on a premise that
turned out to be wrong — see the correction below. The published corpus is stripped of
`user_state` and a local `sync` carries marks across a refresh.

## What changed

- **`web/app.py`** — `JOBSEARCH_READONLY`. A `mutating()` decorator registers
  `/api/crawl`, `/api/fetch`, `/api/ingest` and `/api/enrich` only when it is unset;
  hosted they are 404, not 403. `/api/meta` reports the mode and the UI hides the fetch
  panel.
- **`store.py` + `cli.py`** — `export_user_state` / `import_user_state` /
  `clear_user_state`, driving two commands: `sync <file.db>` (adopt a downloaded corpus,
  carrying marks across, dry-run by default like `prune`) and `export --out` (a
  publishable copy with marks stripped and the WAL checkpointed).
- **`src/hosted.py`** — `stage_corpus`, copying the read-only bundle to a writable temp
  dir because WAL needs `-wal`/`-shm` sidecars. Here rather than in `api/index.py` so
  the suite covers it.
- **`.gitignore`** — `*.db.bak*` / `*.bak-*`. `sync --apply` writes
  `jobs.db.bak-<timestamp>`, which neither `*.db` nor `jobs.db` matched. That gap is
  exactly how a 32 MB backup got committed in `79b54a2`.
- **`api/index.py`, `vercel.json`, `scripts/vercel-build.sh`, `public/robots.txt`,
  root `requirements.txt`** — the Vercel surface. `noindex`, catch-all rewrite, and a
  build that refuses a truncated corpus.
- **`.github/workflows/daily-fetch.yml`** — 08:30 UTC (14:00 IST), `concurrency:
  daily-fetch` with `cancel-in-progress: false`, 300 min timeout, publishes even on a
  mitigation halt so the site serves a partial day.
- **Automatic pruning** of the published corpus, to stay under Vercel's 250 MB bundle
  limit. `prune_stale` gained `keep_marked`; `export` VACUUMs; `sync` calls
  `carry_forward_marked`. This amends ADR-006 — see its 2026-09-06 addendum, which
  records that a CI runner has no `cache/`, so the prune is irreversible there in a way
  the original decision assumed it would not be.

## The correction that removed a whole component

The KV was justified with "Actions rebuilds `jobs.db` from scratch daily, so
`user_state` starts empty." **That was wrong.** The workflow downloads the previous
corpus first and updates it in place, so marks written into a corpus already persist.
The user challenged the need for Upstash and was right to.

The KV therefore bought exactly one thing — a status click on the Vercel URL sticking —
which was not wanted. the `userstate` module was deleted along with its egress exemption, so
`test_only_fetch_py_can_reach_the_network` is back to a single allowed module. That is
stronger than the bounded exemption that briefly replaced it. `sync` takes a file path
rather than downloading, specifically to keep it that way.

## Verification

- `python -m pytest tests -q` → **272 passed**.
- `tests/test_hosted.py` (CONDUCT-011, FILTER-007; journeys CJ-054, CJ-055). The
  read-only and sync/export tests were all watched fail first.
- End-to-end on the real 6,266-job corpus, in separate processes as real usage runs:
  `export` stripped the marks and a plain `sqlite3` read of the upload candidate found 0
  `user_state` rows; `prune --apply` removed 1,883 jobs and left the file at 59.7 MB;
  `export` took it to **46.0 MB**; `sync` restored the marks and a job marked applied at
  34 days old survived the whole round trip.
- Two tests initially passed without the feature because the fixture reused job ids
  across both corpora — caught and fixed before implementing.
- Read-only mode driven against the real 4,107-row corpus: all four crawl routes 404,
  `/api/jobs` and `/api/facets` correct, a status write round-tripped through the
  `statuses` filter.
- Both validators clean.

## Remaining

- **Unverified:** the Actions schedule, the release-asset round trip, the Vercel build
  and `includeFiles` picking up a build-time-downloaded `jobs.db`. All first-push tests.
- **Cloudflare from runner IPs.** Shared cloud ranges are treated more harshly than a
  home connection, so Wellfound may mitigate more often. Halting is correct behaviour;
  frequency is unknown until it runs. **No retry** — that is the forbidden
  backoff-and-continue path.
- **Refreshing the local corpus is manual** — `gh release download` then `sync --apply`.
  A missed `sync` means stale listings locally, not lost marks.
- **The hosted UI cannot mark a job applied.** Deliberate: it is the price of having no
  hosted store. Marks are made locally.
- **A local `export` can publish stale jobs that a local mark spared.** Harmless — they
  are public listings and the mark is stripped — and it cannot happen on the workflow
  path, whose corpus has no marks. Only affects the manual seeding step.
- `jobs.db.bak-before-reconcile` (32 MB, commit `79b54a2`) is still in git history and
  becomes public with the repo. It holds 3,968 job rows and **zero** `user_state` rows.

## Done when

- [x] Hosted app serves read-only with no crawl routes
- [x] Local marks survive a corpus refresh
- [x] The published corpus carries no marks
- [x] Daily workflow written, concurrency-1 preserved
- [x] Vercel entrypoint and build
- [x] Suite green, new guards verified failable
- [ ] First real Actions run and Vercel deploy — **user's next step**

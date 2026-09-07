# Hosted deployment: Actions crawls, Vercel serves

**Status:** written 2026-09-06. The code is tested; **the two hosted halves are
unverified** — no real Actions schedule has run and no Vercel deploy has happened.
Treat the first push as the test.

A daily fetch at 14:00 without the user's PC, at no cost.

## Why the work is split

Vercel cannot crawl. A cold sweep is 75–180 minutes at the mandated 3–5 s spacing,
against Hobby function timeouts measured in minutes, on a read-only filesystem with no
place for SQLite. GitHub Actions has no such limit. So Actions crawls and publishes a
corpus; Vercel only serves it.

```
14:00 IST ─► GitHub Actions
              1. download yesterday's corpus.db   (release asset, tag `corpus`)
              2. python -m src.cli fetch           (3-5s spacing, concurrency 1)
              3. python -m src.cli prune --apply   (drops jobs past max_age_days)
              4. python -m src.cli export          (strips user_state, VACUUMs)
              5. upload corpus.db ───────────────► release asset `corpus`
              6. POST the Vercel deploy hook
                                   │
Vercel build ◄─────────────────────┘   scripts/vercel-build.sh pulls the asset
                                   │   into the bundle
Vercel function ──► stage corpus to /tmp, serve read-only

You ────────► gh release download corpus  +  src.cli sync --apply
```

### Why the corpus is a file and not a database service

The `jobs` view calls **8 Python functions** registered per-connection
(`src/derive.py::register`) — `salary_min`, `salary_usd`, `size_label`,
`remote_label` and friends. Turso, Neon and Supabase all execute SQL server-side,
where those functions do not exist, so every query against `jobs` would fail with
"no such function". The read side has to be a Python process with a local SQLite
file. That is not a preference; it is what the schema requires.

### Why there is no hosted store

There was, briefly, on a mistaken premise: that Actions rebuilt the corpus from scratch
daily and so lost `user_state`. It does not — it downloads the previous corpus first and
updates it in place, so marks written into a corpus persist on their own. The only thing
a hosted KV bought was a status click made *on the Vercel URL* sticking, which is not
wanted. So there is no Upstash, no Blob, no Drive and no credentials.

Marks live on your machine. Two commands keep that workable:

| Command | Does |
|---|---|
| `python -m src.cli sync <file.db> --apply` | Adopt a downloaded corpus, carrying your marks across and backing up the old file |
| `python -m src.cli export --out corpus.db` | Write a publishable copy with `user_state` stripped |

`sync` does no network on purpose — it takes a path, so `src/fetch.py` stays the only
module that can reach the network (AGENTS.md, no exemptions).

### Refreshing your local copy

The repo is public, so the corpus is a plain URL — no `gh`, no auth. One command per
line, because Windows PowerShell 5.1 has no `&&`.

```powershell
Invoke-WebRequest -Uri "https://github.com/<owner>/<repo>/releases/download/corpus/corpus.db" -OutFile "$env:TEMP\corpus.db"
cd jobsearch
python -m src.cli sync "$env:TEMP\corpus.db" --apply
```

```bash
curl -fL -o /tmp/corpus.db https://github.com/<owner>/<repo>/releases/download/corpus/corpus.db
cd jobsearch
python -m src.cli sync /tmp/corpus.db --apply
```

Without `sync` this would be a plain overwrite and every applied mark would go with it.

## Setup

**1. Make the repo public.** Public repositories get unlimited Actions minutes. A
private repo allows 2,000 min/month against a ~5,400 min/month daily sweep, which stops
partway through the month.

**2. One repository secret** (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `VERCEL_DEPLOY_HOOK` | from Vercel → Settings → Git → Deploy Hooks |

**3. Seed the corpus** so the first run is not cold. Use `export`, not a raw copy — it
strips your marks and checkpoints the WAL, so nothing personal is published and no rows
are left behind in `jobs.db-wal`:

```
cd jobsearch
python -m src.cli export --out ../corpus.db
cd ..
```

`cd` has to happen because `python -m src.cli` resolves `src` relative to the working
directory, and one command per line because Windows PowerShell 5.1 rejects `&&` as a
statement separator.

Then publish `corpus.db` under a release tagged `corpus`. **No `gh` required** — the
web UI works: Releases → *Draft a new release* → tag `corpus` → attach the file →
*Publish*. With the CLI installed it is:

```
gh release create corpus --title Corpus --notes "Rebuilt automatically."
gh release upload corpus corpus.db --clobber
```

The workflow itself uses `gh`, which is preinstalled on GitHub's runners; it is only a
local convenience.

**4. Vercel project.** No environment variables are required. The build derives the
corpus URL from the repository Vercel is building
(`VERCEL_GIT_REPO_OWNER`/`VERCEL_GIT_REPO_SLUG`), and `api/index.py` defaults
`JOBSEARCH_READONLY` to `1`.

Optional overrides:

| Variable | When you need it |
|---|---|
| `CORPUS_URL` | A private repo, a corpus hosted elsewhere, or a non-git deploy |
| `CORPUS_TAG` / `CORPUS_ASSET` | A release tag or asset name other than `corpus` / `corpus.db` |

**Order matters.** The build downloads the release, so step 3 has to happen before the
first deploy — otherwise the build fails with a 404 and tells you to seed it. That is
deliberate: Vercel keeps the previous deployment live when a build fails, which is
better than serving an empty corpus.

`vercel.json` carries only the build command and the function config. Routing is left
to Vercel's own FastAPI detection, and `robots.txt` plus `X-Robots-Tag: noindex` are
served by the app itself (`web/app.py`) rather than by a static file and a rewrite —
one routing path instead of two, and testable locally.

`.python-version` pins 3.12. Without it Vercel picks a default and says so in the build
log; an unpinned runtime is a silent variable in a deploy you cannot reproduce.

The root `requirements.txt` is not `jobsearch/requirements.txt`. It **must** include
`beautifulsoup4`: `store.connect()` calls `sources.registry()`, which eagerly imports
all seven source modules, and `sources/ashby.py` imports bs4 at module level — so
omitting it fails at import, after a green build. `lxml` is deliberately excluded; only
the crawl path uses it and the read-only app cannot reach that path. Both facts were
verified by building a clean virtualenv from this file and running a cold start against
the real corpus.

**5. Run it once by hand** — Actions → Daily fetch → Run workflow — rather than waiting
for 14:00.

## What is public

The repo, the published `corpus.db`, and the UI — all third-party job listings.

**Your shortlist, applied and hidden marks are not published.** `export` strips
`user_state` before upload, and the workflow publishes that output rather than
`jobs.db`. This is asserted by a test, not just documented: see CJ-055.

`noindex` and `public/robots.txt` keep the listings out of search results; they do not
make the URL private. Vercel's password protection is a Pro feature, so there is no free
way to gate it without writing an auth layer.

The read-only app registers **no crawl routes at all** (`web/app.py::create_app`, gated
on `JOBSEARCH_READONLY`). `/api/crawl`, `/api/fetch`, `/api/ingest` and `/api/enrich`
return 404 rather than existing and refusing. That is a conduct control: a public
`/api/fetch` would be an internet-reachable trigger for crawling third-party sites,
with the concurrency-1 guarantee broken by whoever else calls it.

## When it halts

`cmd_fetch` returns **exit 2** when a Cloudflare mitigation stops the crawl. That is the
conduct guard working. The workflow still publishes the partial corpus so the site
serves the day, but the step fails and GitHub emails you.

**Do not add a retry.** Retrying through a mitigation is the backoff-and-continue path
the design forbids (`AGENTS.md`; ADR-011 — a cached mitigation is sticky forever, on
purpose). Read the run log, work out what changed, fix the cause.

Actions runners use shared cloud IP ranges that Cloudflare treats more harshly than a
home connection, so Wellfound may mitigate more often here than it does locally. The
other six sources are unaffected either way. How often is unknown until it runs.

## The site is stale but the run was green

Almost always: `VERCEL_DEPLOY_HOOK` is not set.

Vercel bakes the corpus into the bundle at **build** time, so publishing the release
changes nothing until something triggers a rebuild. The corpus is a release asset, not a
commit, so Vercel's git integration never fires on its own — the workflow has to poke
the deploy hook.

A run with no hook still publishes the corpus and still passes, because the corpus is
genuinely updated and the next successful deploy picks it up. It now emits a
`::warning::` on the run summary saying the site was not updated, rather than a line in
the log.

Check the run summary for either:

```
- Corpus published: 61 MB
- ✅ Vercel rebuild triggered.
```

or the warning. To fix, add the secret under Settings → Secrets and variables → Actions,
then re-run the workflow or trigger a Vercel redeploy by hand — the release asset is
already current, so a rebuild alone is enough.

## Timing

`cron: "30 8 * * *"` is **08:30 UTC = 14:00 IST**. GitHub cron has no timezone. Scheduled
runs are best-effort; 5–20 minutes late under load is normal.

## Keeping under the 250 MB bundle limit

The corpus grows every day. The workflow prunes jobs past `max_age_days` (30) and
`export` runs `VACUUM`.

**The VACUUM is the part that matters.** SQLite does not hand deleted pages back to the
OS, so a prune on its own reclaims nothing. Measured on a real 6,266-job corpus:

| | Size |
|---|---|
| Before pruning | 59.7 MB |
| After deleting 1,883 stale jobs | 59.7 MB — *unchanged* |
| After `export` (VACUUM) | 46.0 MB |

This amends ADR-006, which said existing corpora are never touched automatically — see
its 2026-09-06 addendum. Your **local** corpus is still never auto-pruned; only the
published one is.

### Jobs you applied to are never lost

A job you marked `applied` or `shortlisted` is protected two ways:

- a local `prune` skips it (`keep_marked`, on by default);
- the published corpus has no marks, so its prune drops it — and `sync` copies the row
  back from the corpus it is replacing (`store.carry_forward_marked`).

`hidden` is deliberately not protected: it means "stop showing me this", and the mark
survives in `user_state` whether or not the listing does.

## Load

A daily all-roles × all-sources fetch is ~1,150+ requests/day to third parties,
indefinitely — `cache_ttl_hours` is 6, so a daily run re-fetches every listing. Inside
the rate limit and inside the design, but a standing load rather than the occasional one
this was built for. `workflow_dispatch` takes a `roles` input; narrowing the scheduled
run to the roles you actually apply to, with a weekly full sweep, cuts it a lot.

## A VPS was considered and rejected

A ~$5/mo VPS on a private tailnet was the first recommendation: one stable IP, a
persistent disk, and a private URL. It was rejected on cost. The trade accepted in
exchange is a public corpus of listings, a higher mitigation rate from shared runner
IPs, and a manual `sync` where a persistent disk would have needed none.

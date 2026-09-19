# Quick reference

Every command, grouped by what you want to do. Run them from `jobsearch/`, except the
few marked **(repo root)**. For what each one verifiably did the last time it ran, see
[engineering/commands.md](engineering/commands.md).

## First time

```bash
cd jobsearch
pip install -r requirements.txt              # the tool
pip install playwright                       # optional: only for Assist
python -m pytest tests -q                    # offline test suite, ~15 s
```

## Browse jobs

```bash
python -m src.cli serve                      # open http://127.0.0.1:8000
python -m src.cli serve --port 8013          # another port
python -m src.cli stats                      # corpus size, fill rates, applications
```

Stop the server with `Ctrl+C`. It must be stopped before `sync` (next section).

## Get fresh jobs

Choose one. Downloading the published corpus takes about a minute; fetching it
yourself takes over an hour.

**Download the corpus the daily GitHub Action already built (fast):**

```bash
gh release download corpus --repo nagmudit/Jobs --pattern corpus.db --dir /tmp --clobber
python -m src.cli sync /tmp/corpus.db            # dry run: says what it would do
python -m src.cli sync /tmp/corpus.db --apply    # swap it in; keeps your marks
```

`sync` backs up the old database as `jobs.db.bak-<timestamp>` and keeps your
shortlist, applied and hidden marks and your application history.

**Fetch it yourself (slow on purpose, at 3–5 s per request):**

```bash
python -m src.cli fetch --roles artificial-intelligence-engineer   # one role, every source
python -m src.cli fetch --roles software-engineer,backend-engineer  # several roles
python -m src.cli fetch --roles software-engineer --sources greenhouse,ashby
python -m src.cli fetch                          # every role in targets.yaml (~75-95 min)
python -m src.cli ingest                         # API sources with no role filter
python -m src.cli ingest --sources himalayas
python -m src.cli crawl                          # Wellfound only, roles x locations
python -m src.cli crawl --roles software-engineer --locations remote
```

Role slugs: `software-engineer`, `artificial-intelligence-engineer`,
`machine-learning-engineer`, `product-manager`, `backend-engineer`, `mobile-engineer`,
`frontend-engineer`, `full-stack-engineer`, `software-architect`, `devops-engineer`.
Add more in `targets.yaml`; `python -m src.cli roles --location remote` checks
candidates against the live site.

Long fetches can run in the background. Re-running within 6 hours is cheap, because
listings are cached (`cache_ttl_hours`).

Fetch exit codes: `0` done · `3` done, but a site refused us and was skipped ·
`2` stopped on a data-integrity error.

## Keep the corpus tidy

```bash
python -m src.cli prune                      # report jobs older than max_age_days (20)
python -m src.cli prune --apply              # delete them (marked jobs are kept)
python -m src.cli reconcile                  # report rows that no longer match their role
python -m src.cli reconcile --apply
python -m src.cli enrich --ids wellfound:123,wellfound:456   # Wellfound detail pages
python -m src.cli enrich --limit 25
python -m src.cli export --out corpus.db     # shareable copy: marks and history removed
```

## Apply to jobs (Assist)

Setup and the full rules are in
[engineering/resume-and-answers.md](engineering/resume-and-answers.md).

```bash
python -m src.cli profile check              # profile, answers and resume are valid and current
python -m src.cli profile pin My_Resume.pdf  # after changing resume + profile.yaml
python -m src.cli answers pending            # questions still needing your answer
```

Then `serve`, filter **Source** to greenhouse / ashby / lever / workable, and click
**Assist** on a row. It fills the form in a Chrome window; you review and submit.
Assist appears only on a local server, never on the hosted site.

## When your resume changes

1. Put the new PDF in `docs/resume/` and keep the old one.
2. Update `docs/resume/profile.yaml` to match.
3. `python -m src.cli profile pin <new>.pdf`

## Checks before committing

```bash
python -m pytest tests -q                    # the suite
python ../quality/validate_manifest.py       # test manifest
python ../quality/validate_context.py        # docs: dead paths, broken links, secrets
python ../scripts/check_layout.py            # UI in a real browser (needs serve on :8099)
python ../scripts/check_assist.py --pick     # Assist on live forms, FAKE profile
```

## The daily GitHub Action

```bash
gh run list --workflow daily-fetch.yml --limit 5      # recent runs (repo root)
gh run view <run-id> --log | grep -E "blocked|BLOCKED"  # why a run went red
gh workflow run daily-fetch.yml                        # run it now (counts as today's run)
python scripts/daily_gate.py --event schedule --attempted 0   # today's start time (repo root)
```

It runs once a day at a time that changes daily (00:00–20:00 UTC). A red run whose
summary lists **Blocked hosts** still published everything else; see
[engineering/deployment.md](engineering/deployment.md).

## Handy one-offs

```bash
# Back up the database safely (WAL mode: checkpoint first)
python -c "from src import store; c=store.connect('jobs.db'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)')"
cp jobs.db jobs.db.backup

# Serve the hosted read-only variant locally
JOBSEARCH_READONLY=1 python -m src.cli serve
```

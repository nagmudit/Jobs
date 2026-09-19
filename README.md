# jobsearch

A personal job-search tool. It gathers listings from several job boards into one local
SQLite database and gives you a fast filter-and-sort UI with a direct apply link on
every row. For jobs hosted on an applicant-tracking system, it can open the
application form and pre-fill it from your resume, for you to review and submit.

It is built to be a **polite, honest client** of the sites it reads: robots.txt is
enforced in code, requests are spaced 3–5 seconds apart one at a time, and a site that
refuses it is left alone. Nothing here scores jobs for you, applies on your behalf, or
gets past a CAPTCHA. Those are deliberate choices, explained below.

## What it does

**Collects jobs from eight sources into one table**

| Source | How it is read |
|---|---|
| Wellfound | Public role pages, searched by role on the site |
| Himalayas | Public JSON API; filtered locally by role |
| RemoteOK | Public JSON API; filtered locally by role |
| vickybytes | One API endpoint, with the site owner's permission ([ADR-013](docs/architecture/decisions/ADR-013-out-of-band-robots-permission.md)) |
| Greenhouse, Ashby, Lever, Workable | Company job boards, for companies the other sources already surfaced |

Everything is stored exactly as received and every column is derived from that, so
improving a parser never needs a re-crawl. The UI labels each job with its source, and
says whether the role filtering happened on the site or locally.

**Lets you filter and sort it quickly**

- Filters: source, free-text search, location (real places, e.g. Pune or Berlin),
  the role a job was found via, remote/onsite, company size, job type, company,
  minimum salary, equity, age, and your own status. Each filter's counts update as
  you narrow the others.
- Sort: newest, oldest, salary (compared in approximate USD across currencies, but
  shown in the original currency), company, title, size or equity.
- Jobs older than 20 days (configurable) are dropped at ingest and pruned daily.
  Expired listings are hidden.

**Tracks your applications**

- Shortlist, hide, or mark a job applied, interviewing, offer or rejected.
- Clicking **Apply** is noted, and a tray later asks *"did you apply?"*, so you never
  have to find the row again. After 10 days it asks *"any news?"*.
- A small stats strip: applications this week, this month and all time, response
  rate, and median days to a reply.
- Your marks and history stay on your machine. They are stripped from anything
  published and carried across when you pull a fresh corpus.

**Pre-fills applications (Assist)**

On jobs whose form is on Greenhouse, Ashby, Lever or Workable (all four are tested
against live forms), **Assist** opens the form in a visible Chrome window and:

- types your name, contact details and links from a `profile.yaml` built from your
  resume, and attaches your resume PDF;
- answers screening questions ("Do you need visa sponsorship?") from an
  `answers.yaml` that you write;
- outlines what it filled in green, and what it left for you in amber with the
  reason;
- logs every question it could not answer to `unanswered.yaml`, so the answers file
  grows with each form you meet.

**You review the form and press Submit. The tool never submits, never clicks, and
never touches a CAPTCHA.** Matching is deliberately strict: an answer is filled only
when the question matches exactly and the answer is one of the offered options.
Otherwise it is left blank. If your resume PDF changes and the profile has not been
updated to match, Assist refuses to fill. Your personal files are gitignored and never
leave your machine. Details: [ADR-017](docs/architecture/decisions/ADR-017-assisted-apply.md)
and the [resume & answers guide](docs/engineering/resume-and-answers.md).

**Refreshes itself daily, for free (optional)**

A GitHub Actions workflow fetches every source once a day, at a different time each
day, and publishes the corpus as a release asset. A Vercel deployment serves a
read-only copy of the UI. You can browse from any device, and your PC does not need
to be on. See [deployment](docs/engineering/deployment.md).

## What it deliberately does not do

- **No auto-apply.** An application goes out under your name and cannot be taken
  back, and bot-submitted applications are what ATS vendors flag. Assist fills; you
  decide.
- **No CAPTCHA or bot-check circumvention**: no solver services, stealth browsers,
  proxies or fingerprint spoofing. A challenge means a human, so you solve it.
- **No scoring or recommendations.** You filter; the tool fetches and displays.
- **No crawling past a refusal.** If a site blocks a run or its robots.txt cannot be
  read, that site gets no further request for the rest of the run, and the run is
  marked failed so a person notices ([ADR-016](docs/architecture/decisions/ADR-016-per-host-halt.md)).

The full conduct rules are in [AGENTS.md](AGENTS.md). They are enforced in code and
tested, not left to good intentions.

## Quick start

Needs Python 3.11 or newer. Commands run from `jobsearch/`.

```bash
git clone https://github.com/nagmudit/Jobs.git
cd Jobs/jobsearch
pip install -r requirements.txt

# Get jobs. The fast way is to download the corpus the daily workflow already built:
gh release download corpus --repo nagmudit/Jobs --pattern corpus.db --dir /tmp
python -m src.cli sync /tmp/corpus.db --apply

# ...or fetch yourself (slow on purpose: 3-5 s per request, about 10-20 min per role)
python -m src.cli fetch --roles artificial-intelligence-engineer

python -m src.cli serve          # then open http://127.0.0.1:8000
```

Roles, locations, sources and rate limits all live in
[`jobsearch/targets.yaml`](jobsearch/targets.yaml). Nothing is hardcoded.

**To use Assist:** `pip install playwright`, copy
[`docs/resume.example/`](docs/resume.example/) to `docs/resume/`, fill it in, add your
resume PDF, then run `python -m src.cli profile pin <your.pdf>`. The
[resume & answers guide](docs/engineering/resume-and-answers.md) walks through it.

**All the commands on one page:** [docs/quick-reference.md](docs/quick-reference.md).

## How it works

```
targets.yaml ─► fetch / crawl / ingest ─► src/fetch.py ─► the job sites
                                            (robots.txt, 3-5 s spacing, one at a time,
                                             halt a site on refusal, disk cache)
                          │
                          ▼
                 SQLite: raw payloads ─► derived columns (triggers) ─► `jobs` view
                          │
                          ▼
            FastAPI + one HTML page (no build step)  ── Assist ─► visible Chrome
```

- Every request goes through one module, `jobsearch/src/fetch.py`. The only other
  path to the network is Assist's browser, and its limits are tested
  ([ADR-017](docs/architecture/decisions/ADR-017-assisted-apply.md)).
- Four integrity checks raise instead of warning. They catch the failures that come
  back as HTTP 200 with plausible-looking data, such as a site silently ignoring your
  role filter ([ADR-003](docs/architecture/decisions/ADR-003-assertions-raise.md)).
- The test suite (500+ tests) runs offline against synthetic fixtures. CI runs it on
  Python 3.11 and 3.14, and again with no network access at all.

## Repository layout

| Path | What it is |
|---|---|
| `jobsearch/` | The tool: `src/` code, `tests/`, `targets.yaml` config |
| `jobsearch/src/sources/` | One module per job source |
| `jobsearch/src/assist/` | Assisted apply: profile, answer matching, browser |
| `docs/` | Architecture decisions, plans, engineering guides. Start at [docs/index.md](docs/index.md) |
| `docs/resume.example/` | Fake-data templates for Assist (your real copy is gitignored) |
| `api/`, `vercel.json`, `scripts/` | The hosted read-only deployment, and check scripts |
| `.github/workflows/` | CI, and the daily fetch |
| `quality/` | Test manifest and doc validators |
| `wellfound-probe/` | Finished research into how Wellfound can be read. Frozen |

## Documentation

- [docs/quick-reference.md](docs/quick-reference.md): every command, by task
- [docs/index.md](docs/index.md): map of all docs, and which to read for what
- [AGENTS.md](AGENTS.md): conduct rules and conventions (for contributors and coding agents)
- [docs/architecture/decisions/](docs/architecture/decisions/): why things are the way they are (ADR-001 to ADR-017)
- [docs/engineering/resume-and-answers.md](docs/engineering/resume-and-answers.md): using Assist
- [docs/engineering/deployment.md](docs/engineering/deployment.md): the daily workflow and the hosted site

## Status

A personal tool, maintained by one person, and shared in case it is useful. It reads
third-party sites that can change without notice, and the daily workflow is the early
warning when one does. If you run your own copy, keep the rate limits as they are:
they protect your IP address, and the sites.

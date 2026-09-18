# ADR-016: A block stops that host for the rest of the run, not the whole run

**Status:** accepted · **Date:** 2026-09-18 · **Amends** the "Halt on the first
mitigation" conduct rule in `AGENTS.md`, [ADR-003](ADR-003-assertions-raise.md) and
[ADR-007](ADR-007-multi-source.md)

## Context

On 2026-09-18 the daily workflow died partway through its first role. Ashby and
Greenhouse had finished; then `himalayas.app` answered **403 to its own
`robots.txt`**. `Fetcher._load_robots` raised, correctly: an unreadable
robots.txt is never crawled (ADR-007). The error also ended the whole process,
so:

- the rest of `software-engineer` (Lever, RemoteOK, vickybytes, Workable) and all
  9 other roles never ran, although none of those hosts had refused anything;
- the robots failure is a bare `RuntimeError`, which `cmd_fetch` does not catch,
  so it surfaced as a traceback rather than the `HALTED` line a mitigation gets;
- the workflow deploys only on a successful fetch, so the site kept the previous
  day's corpus.

The halt rule was written when Wellfound was the only source: "the crawl" and
"the host" were the same thing. There are now eight sources on eight origins. A
single site blocking GitHub's runners now costs the whole day's run on every
host.

## Decision

**A block is scoped to the origin that issued it.** On the first one, that
origin gets no further request of any kind for the rest of the run, including its
robots.txt. Every other origin carries on.

- **What counts as a block:** exactly what halts today. `MitigationDetected`
  (403, 429, 503 or `cf-mitigated`), and a robots.txt that cannot be read (any
  non-2xx or transport failure). Nothing else changes:
  - `SilentRoleFallback`, `YieldFloor`, `SchemaDrift` and the other
    `CorpusIntegrityError`s still halt the whole run. They mean our parser is
    wrong, not that a host said no.
  - a `RobotsDisallowed` path still raises as it does today.
  - a transport failure on an ordinary request still raises as it does today.
- **Enforced in `Fetcher`, not by callers.** `Fetcher.get` records the origin
  in a per-instance `blocked` map before it re-raises, and refuses any later URL
  on that origin with `HostBlocked`, *before* robots, throttle, cache or
  transport. A caller that swallowed the exception and tried again would still
  send nothing. This is what keeps it from being a retry path.
- **Callers continue with the next source.** `fetch_role` catches the block per
  source and records a `filter_mode: "blocked"` result with the origin and
  reason. The same applies to the source loop in `ingest`, on both the CLI and
  the web side. `crawl` and `enrich` each talk to one host, so for them a block
  and a full halt are still the same thing, and they are unchanged.
- **One `Fetcher` per run.** `cmd_fetch` and the web fetch job already share one
  `Fetcher` across roles, so a host blocked in role 1 gets **zero** requests in
  roles 2–10.
- **It still fails loudly.** `fetch` exits **3** ("completed, some hosts
  blocked") and prints each blocked origin with its reason. Exit 2 stays "halted
  on an integrity error". The workflow deploys the corpus on 0 or 3, so the site
  gets the hosts that answered, then fails the job on 3 so GitHub still emails.

## What does not change

- No retry, no backoff, no later attempt within the run. A blocked origin is
  never contacted again by that process.
- **Across runs, nothing changes:**
  - Locally, the refusal is cached as a mitigation and stays sticky forever
    (ADR-011), so the next run refuses the host without sending anything. Clearing
    that entry is still a deliberate human act.
  - On CI the runner starts with no cache, so each daily run spends one robots.txt
    request on the host, exactly as it does today.
- Robots enforcement, the 3–5 s floor, concurrency 1, the User-Agent policy,
  and the evidence trail (`request_log` records the refusing response before
  anything raises).

## Consequences

- One site blocking the runner costs that site's jobs for the day, not every
  site's.
- **A Wellfound mitigation no longer stops the ATS and API sources.** That is
  the intended effect. It rests on Wellfound's block being about Wellfound
  traffic, which is the only traffic it can see from us.
- A red daily run no longer means "nothing was fetched". The step summary lists
  the blocked hosts, so the reader can tell a partial day from a dead one.
- The rule in `AGENTS.md` becomes "stop all traffic to that host", instead of
  "stop the crawl".

## Alternatives

- **Disable the source in `targets.yaml`** when it blocks. That is still
  available, and it is the right move for a block that turns out to be
  permanent. As the *only* response, it makes every new block cost a whole day
  before a human notices.
- **Skip the host and don't fail the run.** Rejected: a block is exactly the
  thing a human needs to hear about.
- **Retry the host later in the same run.** Rejected: that is the
  backoff-and-continue path the conduct rules forbid.

## Related

`jobsearch/src/fetch.py` · `jobsearch/src/roles.py` · `jobsearch/src/cli.py`
(`cmd_fetch`, `cmd_ingest`) · `jobsearch/src/web/app.py` (`CrawlJob`) ·
`.github/workflows/daily-fetch.yml` · [plan](../../plans/active/per-host-halt.md)

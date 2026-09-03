# ADR-007: What multi-source costs, and robots is per-origin from now on

**Status:** accepted (robots fix) · **proposed** (the rest) · **Date:** 2026-09-04

## Context

A survey of other job platforms ([research note](../../research/job-platform-survey.md))
found three readable without evasion: **RemoteOK** (`/api`), **Himalayas**
(`/jobs/api`, 101,235 jobs), and **We Work Remotely** (RSS). All three return
documented JSON or XML rather than an undocumented internal cache, so they are
materially easier to read than Wellfound.

Adding any of them is not a plugin drop-in. Four things in the codebase assume exactly
one platform, and one of them is a conduct bug.

## Decision

### Accepted and implemented now: robots.txt is cached per origin

`Fetcher._load_robots` cached a single rule set on the instance and returned it for
every subsequent host. The type hint said `dict[str, ...]`; the code ignored the key.

The moment a second host is fetched with the same `Fetcher`, that host is judged by the
**first** host's `robots.txt` — which can permit a path the second site forbids. That is
a conduct violation waiting for the first multi-source commit, so it is fixed ahead of
the feature that would trigger it.

Also fixed: a robots.txt that cannot be read is **never cached**. Previously a failed
load left `_robots` unset and the next call re-fetched; now the failure is explicit and
repeatable, so a host like naukri.com (403 on its own robots.txt) keeps raising
"refusing to crawl blind" rather than becoming permissive on a retry.

Rate limiting stays **global across hosts**, not per-host. Per-host would be the
conventional choice and would be faster; global is stricter, and this tool has no need
to go faster.

### Proposed, not built: the rest of multi-source

1. **`job_raw` needs a composite key.** `source_job_id` is the primary key today.
   RemoteOK id `1137286` and a Wellfound id will collide silently — one would overwrite
   the other. Needs `(source, source_job_id)`, which is a schema migration touching
   `job_provenance`, `job_detail`, `job_location` and `user_state`.

2. **The `jobs` view hardcodes Wellfound's shape** — `json_extract(raw_json,'$.title')`,
   `'$.compensation'`, and an `apply_url` built from `wellfound.com`. The clean form is
   one view per source, `UNION ALL`-ed into `jobs`, each mapping its own raw shape onto
   the same columns.

3. **Two of the four assertions are Wellfound-specific.** `SilentRoleFallback` and
   `PageWrap` describe *its* failure modes and have no meaning for a JSON API.
   `SchemaDrift` and `MitigationDetected` generalise. JSON sources need their own
   equivalents: a response **shape** check, and a `totalCount` sanity check.

4. **`targets.yaml` is single-source.** `roles` and `locations` are Wellfound slugs.
   Multi-source needs per-source target blocks.

## Rationale

ADR-002 pays off here: `raw_json` already stores whatever shape a source returns, so
adding a source is a new ingester plus a new view — storage does not change shape. The
expensive parts are the composite key and the view split, both of which are mechanical.

The robots fix is separated out and done now because it is a correctness issue that
exists regardless of whether multi-source ever ships, and because it is cheap.

## Alternatives

- **A `Fetcher` per host** — what the survey script did as a workaround. Fine for
  throwaway recon, wrong for the tool: separate instances mean separate rate-limit
  state, so N hosts would issue N concurrent request streams.
- **Per-host rate limiting** — conventional and faster. Rejected: the global limit is
  stricter and this tool gains nothing from speed.
- **Normalise every source into one shape at ingest** — rejected for the same reason as
  ADR-002. It discards fields nobody has asked for yet.

## Consequences

- A single `Fetcher` can now safely span hosts, which is the precondition for every
  other item above.
- `RobotsDisallowed` messages now name the origin, so a denial says which site's rules
  applied.
- Eight tests in `tests/test_robots_multiorigin.py` pin the behaviour, including
  order-independence — a cache keyed on anything but origin passes one direction and
  fails the other.
- The remaining four items stay **proposed**. Nothing in the code claims multi-source
  support, and `targets.yaml` still describes Wellfound only.
- ADR-006's 30-day age heuristic should **not** be applied blindly to Himalayas or WWR:
  both publish a real expiry date, which is a better signal than inferred age.

## Related

`jobsearch/src/fetch.py::_load_robots` · `jobsearch/tests/test_robots_multiorigin.py` ·
[research/job-platform-survey.md](../../research/job-platform-survey.md) · ADR-002 ·
ADR-003 · ADR-006

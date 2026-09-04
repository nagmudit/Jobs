"""Himalayas: a documented public JSON API with offset paging.

    GET https://himalayas.app/jobs/api?limit=20&offset=N
    -> {jobs: [...], totalCount, offset, limit, nextCursor}

robots.txt disallows the HTML `/jobs?page=` but explicitly allows `/jobs/api`.
The HTML page returns 403 anyway. Use the API.

## Two measured facts that shape this module

**`limit` is capped at 20.** Asking for 100 returns 20. `totalCount` was 101,235
on 2026-09-04, so a full ingest is ~5,060 requests / ~5.6 h at the mandated
3-5 s. Callers page a bounded window, exactly like a Wellfound slice.

**`salaryPeriod` is not always annual.** 3 of 40 sampled were `hourly`, and
currency varies (USD, CAD, EUR, PLN). An hourly 45 emitted next to an annual
45000 sorts silently wrong, so numeric salary is emitted ONLY for `annual` and
the currency is carried through. Anything else keeps a raw string and NULL
numerics -- unparsed beats wrongly parsed.

**There is no server-side filtering of any kind.** `search`, `q`, `query`,
`category`, `keyword`, `title` and `seniority` are all silently ignored --
verified 2026-09-04, where `search=engineer` returned *Grants Coordinator*,
*Account Executive* and *Industrial Power Systems Inspector*. Role filtering
therefore happens locally, via `src/relevance.py`, after the fetch.

Unlike Wellfound, every posting carries a real `expiryDate`. That is a better
liveness signal than the inferred-age heuristic in ADR-006, and it is stored.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import urlparse

from .. import assertions as A
from .. import store as S
from ..relevance import matches

NAME = "himalayas"
BASE = "https://himalayas.app"
PAGE_LIMIT = 20  # server-enforced; asking for more is silently clamped


def api_url(offset: int = 0, limit: int = PAGE_LIMIT) -> str:
    return f"{BASE}/jobs/api?limit={limit}&offset={offset}"


def parse(payload: str, url: str) -> dict[str, Any]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise A.SchemaDrift(f"{url}: response is not JSON: {e}") from e
    if not isinstance(data, dict) or "jobs" not in data:
        keys = sorted(data)[:12] if isinstance(data, dict) else type(data).__name__
        raise A.SchemaDrift(f"{url}: expected an object with 'jobs'; got {keys}")
    if not isinstance(data["jobs"], list):
        raise A.SchemaDrift(f"{url}: 'jobs' is {type(data['jobs']).__name__}, not a list")
    return data


def native_id(job: dict) -> str | None:
    """The guid is a full URL and is unique (40/40 sampled). Store the path,
    not the whole URL -- shorter, and stable if the domain ever moves."""
    guid = job.get("guid") or job.get("applicationLink")
    if not guid:
        return None
    path = urlparse(str(guid)).path.strip("/")
    return path or str(guid)


def ingest(
    fetcher,
    conn,
    target: dict[str, Any],
    cutoff_ts: int | None = None,
    max_pages: int = 20,
    on_page: Callable[[dict], None] | None = None,
    **_: Any,
) -> dict[str, Any]:
    role = target.get("role")
    keywords = target.get("keywords") or []
    offset = int(target.get("offset", 0))
    seen = new = stale = filtered = pages = 0
    claimed: int | None = None
    ended = "max_pages"
    prev_yield: int | None = None

    for _ in range(max_pages):
        url = api_url(offset)
        resp = fetcher.get(url, max_age=fetcher.listing_ttl)
        if not resp.ok:
            ended = f"http_{resp.status}"
            break

        data = parse(resp.text, url)
        jobs = data["jobs"]

        if claimed is None:
            claimed = data.get("totalCount")
        # The server clamps `limit`; if it ever stops honouring `offset` the
        # same way Wellfound wraps pages, we would silently re-ingest page 1.
        got_offset = data.get("offset")
        if got_offset is not None and int(got_offset) != offset:
            raise A.PageWrap(
                f"{url}: requested offset={offset}, server returned "
                f"offset={got_offset}. Stopping rather than re-ingesting.")

        if not jobs:
            ended = "exhausted"
            break
        A.check_yield(len(jobs), 1, url, prev_yield)

        for j in jobs:
            nid = native_id(j)
            if not nid:
                continue
            ts = j.get("pubDate")
            if cutoff_ts and isinstance(ts, (int, float)) and ts < cutoff_ts:
                stale += 1
                continue
            # Himalayas ignores every server-side filter parameter, so the role
            # filter has to happen here. Counted separately from stale so the
            # telemetry distinguishes "too old" from "wrong role".
            if not matches(j.get("title"), j.get("categories"), keywords):
                filtered += 1
                continue
            cslug = j.get("companySlug") or S.slugify_company(j.get("companyName") or "")
            if cslug:
                S.upsert_company(conn, cslug, {
                    "name": j.get("companyName"), "slug": cslug,
                    "logo": j.get("companyLogo")}, source=NAME)
            if S.upsert_job(conn, nid, cslug, j, source=NAME):
                new += 1
            # Role, not the internal target name, so found_via_roles is one
            # vocabulary across every source.
            S.add_provenance(conn, S.job_uid(NAME, nid),
                             role or target.get("name", "all"), "remote", pages + 1)
            seen += 1
        conn.commit()

        pages += 1
        prev_yield = len(jobs)
        if on_page:
            on_page({"source": NAME, "page": pages, "jobs": len(jobs),
                     "kept": seen, "stale_skipped": stale,
                     "filtered_out": filtered, "companies": 0})
        offset += len(jobs)

    return {"source": NAME, "target": role or target.get("name", "all"),
            "ended_reason": ended, "seen": seen, "new": new,
            "stale_skipped": stale, "filtered_out": filtered, "pages": pages,
            "claimed": claimed, "filter_mode": "local" if keywords else "none"}


CORE_VIEW_SQL = """
SELECT
  'himalayas'                                              AS source,
  j.source_job_id                                          AS source_job_id,
  j.native_id                                              AS native_id,
  j.company_slug                                           AS company_slug,
  json_extract(j.raw_json,'$.title')                       AS title,
  json_extract(j.raw_json,'$.companyName')                 AS company,
  NULL                                                     AS company_size,
  NULL                                                     AS company_size_min,
  NULLIF(json_extract(j.raw_json,'$.excerpt'),'')          AS high_concept,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$.categories')) AS badges,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$.locationRestrictions')) AS location_raw,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$.locationRestrictions')) AS remote_locations,
  1                                                        AS remote,
  'REMOTE'                                                 AS remote_config,
  'Remote'                                                 AS remote_label,
  json_extract(j.raw_json,'$.employmentType')              AS job_type,
  CASE WHEN json_extract(j.raw_json,'$.minSalary') IS NOT NULL THEN
    COALESCE(json_extract(j.raw_json,'$.currency'),'') || ' ' ||
    json_extract(j.raw_json,'$.minSalary') || ' – ' ||
    COALESCE(json_extract(j.raw_json,'$.maxSalary'), json_extract(j.raw_json,'$.minSalary')) ||
    ' /' || COALESCE(json_extract(j.raw_json,'$.salaryPeriod'),'?')
  END                                                      AS salary_raw,
  -- Numeric ONLY for annual. Hourly figures are two orders of magnitude apart
  -- and would sort silently wrong against annual ones.
  CASE WHEN json_extract(j.raw_json,'$.salaryPeriod') = 'annual'
       THEN json_extract(j.raw_json,'$.minSalary') END     AS salary_min_native,
  CASE WHEN json_extract(j.raw_json,'$.salaryPeriod') = 'annual'
       THEN json_extract(j.raw_json,'$.maxSalary') END     AS salary_max_native,
  json_extract(j.raw_json,'$.currency')                    AS salary_currency,
  json_extract(j.raw_json,'$.salaryPeriod')                AS salary_period,
  json_extract(j.raw_json,'$.pubDate')                     AS posted_ts,
  json_extract(j.raw_json,'$.expiryDate')                  AS expires_ts,
  json_extract(j.raw_json,'$.description')                 AS description,
  NULL                                                     AS ats_source,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$.seniority')) AS primary_role_title,
  NULL                                                     AS auto_posted,
  COALESCE(json_extract(j.raw_json,'$.applicationLink'),
           json_extract(j.raw_json,'$.guid'))              AS apply_url,
  j.first_seen                                             AS first_seen,
  j.last_seen                                              AS last_seen
FROM job_raw j
WHERE j.source = 'himalayas'
"""

"""RemoteOK: a documented public JSON API.

    GET https://remoteok.com/api            -> latest ~100 postings
    GET https://remoteok.com/api?tag=<tag>  -> filtered by tag

The response is a JSON ARRAY whose FIRST element is not a job -- it is a legal
notice carrying the API terms of service. Skipping it by position would be
fragile; we skip any element without a `position` key.

## Terms

RemoteOK's own notice, returned in that first element, verbatim:

    "Please link back (with follow, and without nofollow!) to the URL on Remote
    OK and mention Remote OK as a source, so we get traffic back from your site.
    If you do not we'll have to suspend API access.
    Please don't use the Remote OK logo without written permission as it's a
    registered trademark, please DO use our name Remote OK though."

This tool is local and single-user: it has no public page from which to link
back, so the clause aimed at republishers does not bite. What we do honour is
attribution -- `apply_url` points at the RemoteOK posting, and the UI shows the
source name on every row. We do not copy the logo. If this ever grows a public
surface, the link-back requirement becomes a real obligation.

## Shape

No pagination: the endpoint returns the latest set and nothing else. There is
no total count to sanity-check against, so `parse` guards the array shape. There
is deliberately NO yield floor: a narrow tag legitimately returns zero jobs.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import quote

from .. import assertions as A
from .. import store as S
from ..relevance import matches

NAME = "remoteok"
BASE = "https://remoteok.com"


def api_url(tag: str | None = None) -> str:
    # Tags are encoded: four of the working tags contain a space, and the space
    # is load-bearing -- "front end" returns 100 jobs while "frontend" returns 0,
    # "full stack" works while "fullstack" does not.
    return f"{BASE}/api?tag={quote(tag)}" if tag else f"{BASE}/api"


def parse(payload: str, url: str) -> list[dict[str, Any]]:
    """Job objects only. Raises rather than returning junk on a shape change."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise A.SchemaDrift(f"{url}: response is not JSON: {e}") from e
    if not isinstance(data, list):
        raise A.SchemaDrift(
            f"{url}: expected a JSON array, got {type(data).__name__}")
    jobs = [d for d in data if isinstance(d, dict) and d.get("position")]
    # The legal-notice element is always present, even when a tag matches
    # nothing. "Only the notice" is a legitimately EMPTY result (verified live:
    # ?tag=frontend, ?tag=zzz-not-a-real-tag both return exactly that), not a
    # shape change -- raising there would abort a healthy ingest.
    data_elems = [d for d in data
                  if isinstance(d, dict) and "legal" not in d]
    if data_elems and not jobs:
        raise A.SchemaDrift(
            f"{url}: {len(data_elems)} non-notice elements but none carry "
            f"'position' -- the job shape changed. "
            f"Keys seen: {sorted(data_elems[-1].keys())[:12]}")
    return jobs


def native_id(job: dict) -> str | None:
    jid = job.get("id") or job.get("slug")
    return str(jid) if jid else None


def ingest(
    fetcher,
    conn,
    target: dict[str, Any],
    cutoff_ts: int | None = None,
    max_pages: int = 1,
    on_page: Callable[[dict], None] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """`target` accepts `tag`, `role`, `keywords`, `categories`.

    max_pages is ignored: the endpoint has no pagination, so one request is the
    whole thing.

    **Pass no tag for role fetching.** Measured 2026-09-04, the tag endpoints
    serve an archive rather than the live feed:

        /api (no tag)          100 jobs, median age   5 days, 100 within 30d
        ?tag=ai                 75 jobs, median age 112 days,   1 within 30d
        ?tag=machine learning   20 jobs, median age 144 days,   0 within 30d
        ?tag=software           30 jobs, median age 136 days,   0 within 30d

    So tag + the 30-day cutoff yields essentially nothing. The role filter is
    applied locally against the fresh unfiltered feed instead. The tag is kept
    as an option for deliberately mining older postings.

    Jobs failing the role filter are counted as `filtered_out`, separate from
    `stale_skipped`, so the telemetry distinguishes "wrong role" from "too old".
    """
    tag = target.get("tag")
    role = target.get("role")
    keywords = target.get("keywords") or []
    url = api_url(tag)
    resp = fetcher.get(url, max_age=fetcher.listing_ttl)
    if not resp.ok:
        return {"source": NAME, "target": tag or "all", "ended_reason": f"http_{resp.status}",
                "seen": 0, "new": 0, "stale_skipped": 0, "pages": 0}

    jobs = parse(resp.text, url)
    # No yield floor here: a narrow tag legitimately returns zero jobs (verified
    # live -- ?tag=frontend and ?tag=zzz both return only the legal notice).

    seen = new = stale = filtered = 0
    for j in jobs:
        nid = native_id(j)
        if not nid:
            continue
        ts = j.get("epoch")
        if cutoff_ts and isinstance(ts, (int, float)) and ts < cutoff_ts:
            stale += 1
            continue
        # Title ONLY -- never RemoteOK's own tags. Its tagging is unreliable:
        # ?tag=engineer returns Kitchen Technician, Joiner and JANITOR, all of
        # which genuinely carry the 'engineer' tag. Matching on tags would put
        # carpenters in the software-engineer role.
        if not matches(j.get("position"), None, keywords):
            filtered += 1
            continue
        # RemoteOK has no company entity, only a name. Slugify it so the
        # company facet still works; the raw name is preserved in raw_json.
        cslug = S.slugify_company(j.get("company") or "")
        if cslug:
            S.upsert_company(conn, cslug, {"name": j.get("company"),
                                           "logo": j.get("company_logo")}, source=NAME)
        if S.upsert_job(conn, nid, cslug, j, source=NAME):
            new += 1
        # Provenance records the ROLE, not the tag, so found_via_roles is one
        # vocabulary across every source.
        S.add_provenance(conn, S.job_uid(NAME, nid), role or tag or "all",
                         "remote", 1)
        seen += 1
    conn.commit()

    if on_page:
        on_page({"source": NAME, "page": 1, "jobs": len(jobs), "kept": seen,
                 "stale_skipped": stale, "filtered_out": filtered, "companies": 0})
    return {"source": NAME, "target": role or tag or "all",
            "ended_reason": "exhausted", "seen": seen, "new": new,
            "stale_skipped": stale, "filtered_out": filtered, "pages": 1,
            "claimed": len(jobs),
            "filter_mode": "server+local" if tag else "local"}


CORE_VIEW_SQL = """
SELECT
  'remoteok'                                               AS source,
  j.source_job_id                                          AS source_job_id,
  j.native_id                                              AS native_id,
  j.company_slug                                           AS company_slug,
  json_extract(j.raw_json,'$.position')                    AS title,
  json_extract(j.raw_json,'$.company')                     AS company,
  NULL                                                     AS company_size,
  NULL                                                     AS company_size_min,
  NULL                                                     AS high_concept,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$.tags')) AS badges,
  NULLIF(json_extract(j.raw_json,'$.location'),'')         AS location_raw,
  NULL                                                     AS remote_locations,
  1                                                        AS remote,
  'REMOTE'                                                 AS remote_config,
  'Remote'                                                 AS remote_label,
  NULL                                                     AS job_type,
  -- RemoteOK gives numbers, not a string; synthesise a readable one so the
  -- salary column is never blank where numbers exist.
  CASE WHEN json_extract(j.raw_json,'$.salary_min') > 0
       THEN '$' || json_extract(j.raw_json,'$.salary_min') ||
            ' – $' || json_extract(j.raw_json,'$.salary_max')
  END                                                      AS salary_raw,
  NULLIF(json_extract(j.raw_json,'$.salary_min'),0)        AS salary_min_native,
  NULLIF(json_extract(j.raw_json,'$.salary_max'),0)        AS salary_max_native,
  CASE WHEN json_extract(j.raw_json,'$.salary_min') > 0 THEN 'USD' END AS salary_currency,
  CASE WHEN json_extract(j.raw_json,'$.salary_min') > 0 THEN 'annual' END AS salary_period,
  json_extract(j.raw_json,'$.epoch')                       AS posted_ts,
  NULL                                                     AS expires_ts,
  json_extract(j.raw_json,'$.description')                 AS description,
  NULL                                                     AS ats_source,
  NULL                                                     AS primary_role_title,
  NULL                                                     AS auto_posted,
  COALESCE(json_extract(j.raw_json,'$.url'),
           json_extract(j.raw_json,'$.apply_url'))         AS apply_url,
  j.first_seen                                             AS first_seen,
  j.last_seen                                              AS last_seen
FROM job_raw j
WHERE j.source = 'remoteok'
"""

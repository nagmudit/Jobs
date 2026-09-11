"""vickybytes.com: one unauthenticated JSON endpoint, the whole feed per request.

    GET https://vickybytes.com/api/opportunities   -> array of ~316 opportunities

## Permission

Their robots.txt carries `Disallow: /api`, and the listings are reachable no
other way: the sitemap it advertises holds ~67 blog and marketing pages and zero
listings. The site owner was asked and agreed to a personal daily fetch of this
one endpoint, and that grant is declared in `targets.yaml` under
`robots_overrides` with who gave it and when. Without that entry `Fetcher`
refuses the URL and this module cannot run -- which is the intended revocation
path. See ADR-013.

The grant is informal and carries no rate limit, so our own 3-5 s spacing and
concurrency 1 still apply; they are this repo's floor, not the site's to waive.

## Shape

No pagination, no filtering, no total count -- one request returns everything.
Measured 2026-09-08: 316 entries, 24% within the 30-day cutoff, median age 76
days, 40 inside a week. Stale-heavy but genuinely live.

`type` is the WORK MODE, not the kind of posting: onsite / hybrid / remote, plus
`job` for entries that never specified one. Filtering on `type == 'job'` would
discard 88% of the feed.

`apply_method` is `link` for 314 of 316; the two `form` entries carry
`contentLink: null` and are skipped rather than stored with a dead Apply button.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

from .. import assertions as A
from .. import store as S
from ..relevance import matches

NAME = "vickybytes"
BASE = "https://vickybytes.com"

# Tags double as this feed's only structured signal. These name the shape of the
# engagement rather than a skill, so they are what `job_type` can be read from.
JOB_TYPE_TAGS = ("full-time", "part-time", "internship", "contract", "freelance")


def api_url() -> str:
    return f"{BASE}/api/opportunities"


def parse(payload: str, url: str) -> list[dict[str, Any]]:
    """Opportunity objects only. Raises rather than returning junk on drift."""
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise A.SchemaDrift(f"{url}: response is not JSON: {e}") from e
    if not isinstance(data, list):
        raise A.SchemaDrift(f"{url}: expected a JSON array, got {type(data).__name__}")
    out = [j for j in data if isinstance(j, dict) and j.get("id") is not None]
    if data and not out:
        raise A.SchemaDrift(
            f"{url}: {len(data)} elements, none carrying an 'id' -- shape changed")
    return out


def native_id(j: dict[str, Any]) -> str | None:
    nid = j.get("id")
    return str(nid) if nid is not None else None


def posted_ts(j: dict[str, Any]) -> int | None:
    """Unix seconds from the ISO timestamp, or None.

    None means UNKNOWN AGE, which ADR-006 keeps rather than drops -- an
    unparseable date must never silently delete a job.
    """
    raw = j.get("timestamp") or j.get("created_sxp")
    if not raw:
        return None
    try:
        return int(dt.datetime.fromisoformat(str(raw)).timestamp())
    except (TypeError, ValueError):
        return None


def ingest(
    fetcher,
    conn,
    target: dict[str, Any],
    cutoff_ts: int | None = None,
    max_pages: int = 1,
    on_page: Callable[[dict], None] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """`target` accepts `role` and `keywords`. `max_pages` is ignored -- the
    endpoint has no pagination, so one request is the whole feed.

    The role filter is applied LOCALLY (ADR-009): the endpoint takes no query
    parameters of any kind, so there is nothing to filter server-side. Tags are
    passed to `matches` alongside the title because they are curated per posting
    ('python', 'devops', 'ai') rather than crowd-applied, unlike RemoteOK's.
    """
    role = target.get("role")
    keywords = target.get("keywords") or []
    url = api_url()
    resp = fetcher.get(url, max_age=fetcher.listing_ttl)
    if not resp.ok:
        return {"source": NAME, "target": role or "all",
                "ended_reason": f"http_{resp.status}", "seen": 0, "new": 0,
                "stale_skipped": 0, "filtered_out": 0, "pages": 0,
                "filter_mode": "local"}

    jobs = parse(resp.text, url)
    # No yield floor: an empty or fully-stale feed is a legitimate answer here,
    # and this endpoint publishes no claimed total to check a shortfall against.

    seen = new = stale = filtered = 0
    for j in jobs:
        nid = native_id(j)
        if not nid:
            continue
        if not j.get("is_live"):
            filtered += 1
            continue
        # `form` postings apply through a vickybytes-hosted form and carry no
        # link. Storing one means a row whose Apply button goes nowhere, and a
        # working apply link is the tool's whole promise.
        if not str(j.get("contentLink") or "").strip():
            filtered += 1
            continue
        ts = posted_ts(j)
        if cutoff_ts and ts is not None and ts < cutoff_ts:
            stale += 1
            continue
        if not matches(j.get("title"), j.get("tags"), keywords):
            filtered += 1
            continue

        cslug = S.slugify_company(j.get("fullName") or "")
        if cslug:
            S.upsert_company(conn, cslug, {"name": j.get("fullName"),
                                           "logo": j.get("imageLink")}, source=NAME)
        if S.upsert_job(conn, nid, cslug, j, source=NAME):
            new += 1
        S.add_provenance(conn, S.job_uid(NAME, nid), role or "all", "anywhere", 1)
        seen += 1
    conn.commit()

    if on_page:
        on_page({"source": NAME, "page": 1, "jobs": len(jobs), "kept": seen,
                 "stale_skipped": stale, "filtered_out": filtered, "companies": 0})
    return {"source": NAME, "target": role or "all", "ended_reason": "exhausted",
            "seen": seen, "new": new, "stale_skipped": stale,
            "filtered_out": filtered, "pages": 1, "claimed": len(jobs),
            "filter_mode": "local"}


CORE_VIEW_SQL = """
SELECT
  'vickybytes'                                             AS source,
  j.source_job_id                                          AS source_job_id,
  j.native_id                                              AS native_id,
  j.company_slug                                           AS company_slug,
  json_extract(j.raw_json,'$.title')                       AS title,
  json_extract(j.raw_json,'$.fullName')                    AS company,
  NULL                                                     AS company_size,
  NULL                                                     AS company_size_min,
  NULLIF(json_extract(j.raw_json,'$.shortDescription'),'') AS high_concept,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$.tags')) AS badges,
  -- This feed ends locations with a dangling separator as well as whitespace:
  -- 'Bengaluru, India |', 'Gurugram ', 'Pune,'. Left alone the pipe renders
  -- straight into the table, and the dirty string becomes its own location
  -- facet sitting next to the clean one. Trimmed both ends, twice, because the
  -- separator is usually followed by a space.
  NULLIF(trim(trim(trim(trim(
    COALESCE(json_extract(j.raw_json,'$.location'),'')), '|,-/;'))), '') AS location_raw,
  NULL                                                     AS remote_locations,
  -- `type` is the WORK MODE, not the kind of posting. 'job' means the poster
  -- never said, which is unknown rather than onsite -- so NULL, not 0.
  CASE json_extract(j.raw_json,'$.type')
    WHEN 'remote' THEN 1 WHEN 'hybrid' THEN 1
    WHEN 'onsite' THEN 0 END                               AS remote,
  CASE json_extract(j.raw_json,'$.type')
    WHEN 'remote' THEN 'REMOTE' WHEN 'hybrid' THEN 'HYBRID'
    WHEN 'onsite' THEN 'ONSITE' END                        AS remote_config,
  CASE json_extract(j.raw_json,'$.type')
    WHEN 'remote' THEN 'Remote' WHEN 'hybrid' THEN 'Onsite or Remote'
    WHEN 'onsite' THEN 'Onsite' END                        AS remote_label,
  (SELECT value FROM json_each(j.raw_json,'$.tags')
    WHERE lower(value) IN ('full-time','part-time','internship','contract','freelance')
    LIMIT 1)                                               AS job_type,
  NULLIF(trim(COALESCE(json_extract(j.raw_json,'$.salaryRange'),'')),'') AS salary_raw,
  -- Numerics stay NULL on purpose: the outer `jobs` view already falls back to
  -- salary_min/max/currency over salary_raw, and those parse this feed's
  -- formats correctly -- including leaving hourly rates unparsed (ADR-012).
  NULL                                                     AS salary_min_native,
  NULL                                                     AS salary_max_native,
  NULL                                                     AS salary_currency,
  NULL                                                     AS salary_period,
  CAST(strftime('%s', json_extract(j.raw_json,'$.timestamp')) AS INTEGER) AS posted_ts,
  NULL                                                     AS expires_ts,
  NULLIF(COALESCE(NULLIF(json_extract(j.raw_json,'$.description'),''),
                  json_extract(j.raw_json,'$.shortDescription')),'') AS description,
  -- The apply link often points at Greenhouse/Lever/Ashby, but this stays NULL:
  -- populating it would enrol ~300 companies into ATS expansion (ADR-010) and
  -- lengthen every fetch. That is an opt-in, not a side effect of adding a feed.
  NULL                                                     AS ats_source,
  NULL                                                     AS primary_role_title,
  NULL                                                     AS auto_posted,
  json_extract(j.raw_json,'$.contentLink')                 AS apply_url,
  j.first_seen                                             AS first_seen,
  j.last_seen                                              AS last_seen
FROM job_raw j WHERE j.source = 'vickybytes'
"""

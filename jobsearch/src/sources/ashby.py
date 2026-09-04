"""Ashby hosted job boards — company-scoped, not searchable.

Official docs (developers.ashbyhq.com/docs/public-job-posting-api), read
2026-09-04, document the lightweight endpoint as::

    GET https://api.ashbyhq.com/posting-api/job-board/{JOB_BOARD_NAME}

It returns every currently published posting and optionally compensation. The
board name is the final path component of ``https://jobs.ashbyhq.com/{name}``.

That documented API cannot be used under this repository's conduct contract:
``api.ashbyhq.com/robots.txt`` returned HTTP 401 during live verification, and
an unreadable robots file is a hard stop (ADR-007). We do not bypass that guard.

The hosted board is a separate origin. Its robots.txt was readable and allowed
both paths used here, verified live 2026-09-04:

    GET https://jobs.ashbyhq.com/{board}
    GET https://jobs.ashbyhq.com/{board}/{posting_id}

The board HTML embeds all current posting summaries in ``window.__appData``.
After the role filter, each matching posting page supplies its full record in
the same object plus a JobPosting JSON-LD block. JSON-LD carries ``datePosted``
and structured annual salary bounds/currency, so those values are mapped
without parsing display strings. This costs one board request plus one request
per role-matching posting, never one detail request per off-role posting.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from bs4 import BeautifulSoup

from .. import assertions as A
from .. import store as S
from ..relevance import matches

NAME = "ashby"
BASE = "https://jobs.ashbyhq.com"
APP_DATA = "window.__appData ="


def board_url(token: str, content: bool = True) -> str:
    """Public hosted board. ``content`` keeps the shared resolver signature."""
    del content
    return f"{BASE}/{quote(token, safe='')}"


def detail_url(token: str, posting_id: str) -> str:
    return f"{board_url(token)}/{quote(str(posting_id), safe='')}"


def _app_data(payload: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(payload, "html.parser")
    script = next(
        ((s.string or s.get_text()) for s in soup.find_all("script")
         if APP_DATA in (s.string or s.get_text())),
        None,
    )
    if script is None:
        raise A.SchemaDrift(f"{url}: expected window.__appData in board HTML")
    raw = script.split(APP_DATA, 1)[1].lstrip()
    try:
        data, _ = json.JSONDecoder().raw_decode(raw)
    except json.JSONDecodeError as e:
        raise A.SchemaDrift(f"{url}: window.__appData is not JSON: {e}") from e
    if not isinstance(data, dict):
        raise A.SchemaDrift(
            f"{url}: window.__appData is {type(data).__name__}, not an object")
    return data


class BoardNotFound(RuntimeError):
    """Ashby serves HTTP 200 for a board that does not exist.

    Distinct from SchemaDrift on purpose. `company_ats` caches board tokens and
    boards do get renamed or deleted -- the Phase 1 probe recorded exactly that
    for BlueVine's Greenhouse board. Greenhouse signals it with a 404; Ashby
    signals it with a 200 whose `organization` and `jobBoard` are both null. If
    that raised SchemaDrift it would abort every remaining company in the run,
    which is a stale-cache problem being reported as a corpus-integrity one.
    """


def parse(payload: str, url: str) -> dict[str, Any]:
    """Parse one hosted board.

    Raises BoardNotFound for Ashby's silent-200 not-found page, and SchemaDrift
    only for a payload whose shape genuinely moved.
    """
    data = _app_data(payload, url)
    board = data.get("jobBoard")
    organization = data.get("organization")
    if board is None and organization is None:
        raise BoardNotFound(f"{url}: no such Ashby board (HTTP 200, nulled payload)")
    if not isinstance(board, dict) or not isinstance(organization, dict):
        raise A.SchemaDrift(f"{url}: expected organization and jobBoard objects")
    jobs = board.get("jobPostings")
    if not isinstance(jobs, list):
        raise A.SchemaDrift(
            f"{url}: jobBoard.jobPostings is "
            f"{type(jobs).__name__}, not a list")
    teams = board.get("teams")
    if not isinstance(teams, list):
        raise A.SchemaDrift(
            f"{url}: jobBoard.teams is {type(teams).__name__}, not a list")
    return {"organization": organization, "teams": teams, "jobs": jobs}


def parse_detail(payload: str, url: str) -> dict[str, Any]:
    data = _app_data(payload, url)
    posting = data.get("posting")
    if not isinstance(posting, dict):
        raise A.SchemaDrift(f"{url}: expected a posting object")

    soup = BeautifulSoup(payload, "html.parser")
    linked_data = None
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            candidate = json.loads(script.string or script.get_text())
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("@type") == "JobPosting":
            linked_data = candidate
            break
    if linked_data is None:
        raise A.SchemaDrift(f"{url}: expected JobPosting JSON-LD")
    return {"organization": data.get("organization"), "posting": posting,
            "linked_data": linked_data}


def clean_content(raw: str | None) -> str | None:
    if not raw:
        return None
    text = html.unescape(str(raw))
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip() or None


def native_id(board_token: str, job: dict) -> str | None:
    jid = job.get("id")
    return f"{board_token}/{jid}" if jid else None


def _period(unit: str | None) -> str | None:
    return {
        "YEAR": "annual", "MONTH": "monthly", "WEEK": "weekly",
        "DAY": "daily", "HOUR": "hourly",
    }.get(str(unit).upper()) if unit else None


def _salary(linked_data: dict) -> dict[str, Any]:
    base = linked_data.get("baseSalary") or {}
    if not isinstance(base, dict):
        return {}
    value = base.get("value") or {}
    if not isinstance(value, dict):
        return {}
    lo, hi = value.get("minValue"), value.get("maxValue")
    currency, unit = base.get("currency"), value.get("unitText")
    if lo is None and hi is None:
        return {}
    bounds = str(lo) if hi is None or hi == lo else f"{lo} - {hi}"
    return {
        "_salary_raw": " ".join(x for x in
                                [str(currency or ""), bounds,
                                 f"/{unit}" if unit else ""] if x),
        "_salary_min": lo,
        "_salary_max": hi if hi is not None else lo,
        "_salary_currency": currency,
        "_salary_period": _period(unit),
    }


def normalise(board_token: str, job: dict, detail: dict,
              company_name: str | None, team_name: str | None) -> dict[str, Any]:
    """Keep both live payload objects and add only cross-payload derivatives."""
    posting = detail["posting"]
    linked_data = detail["linked_data"]
    out = dict(job)
    badges = list(posting.get("teamNames") or [])
    for value in (posting.get("departmentExternalName"),
                  posting.get("departmentName"), team_name):
        if value and value not in badges:
            badges.append(value)
    secondary = posting.get("secondaryLocationNames")
    if secondary is None:
        secondary = [x.get("locationName") for x in
                     (job.get("secondaryLocations") or []) if x.get("locationName")]
    out.update({
        "_board_token": board_token,
        "_company_name": company_name,
        "_posting": posting,
        "_linked_data": linked_data,
        "_badges": badges,
        "_description_text": clean_content(
            posting.get("descriptionHtml") or linked_data.get("description")),
        "_remote_locations": secondary,
        "_apply_url": detail_url(board_token, str(job["id"])),
    })
    out.update(_salary(linked_data))
    return out


def expand_company(
    fetcher,
    conn,
    company_slug: str,
    board_token: str,
    keywords: list[str] | None = None,
    cutoff_ts: int | None = None,
    role: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    url = board_url(board_token)
    resp = fetcher.get(url, max_age=fetcher.listing_ttl)
    if not resp.ok:
        return {"source": NAME, "company_slug": company_slug,
                "board_token": board_token,
                "ended_reason": f"http_{resp.status}", "seen": 0, "new": 0,
                "stale_skipped": 0, "filtered_out": 0, "claimed": None}

    try:
        data = parse(resp.text, url)
    except BoardNotFound:
        # A cached token that has gone dead. Report it and move on; the run
        # must not lose every remaining company over one stale board.
        return {"source": NAME, "company_slug": company_slug,
                "board_token": board_token, "ended_reason": "board_not_found",
                "seen": 0, "new": 0, "stale_skipped": 0, "filtered_out": 0,
                "claimed": None, "filter_mode": "local"}
    jobs = data["jobs"]
    team_names = {
        t.get("id"): t.get("externalName") or t.get("name")
        for t in data["teams"] if isinstance(t, dict) and t.get("id")
    }
    company_name = data["organization"].get("name")
    seen = new = stale = filtered = 0

    for job in jobs:
        if not isinstance(job, dict):
            raise A.SchemaDrift(f"{url}: jobBoard.jobPostings contains a non-object")
        nid = native_id(board_token, job)
        if not nid:
            continue
        team_name = team_names.get(job.get("teamId"))
        categories = [team_name] if team_name else []
        if not matches(job.get("title"), categories, keywords):
            filtered += 1
            continue

        durl = detail_url(board_token, str(job["id"]))
        detail_resp = fetcher.get(durl)
        if not detail_resp.ok:
            continue
        detail = parse_detail(detail_resp.text, durl)
        date_posted = detail["linked_data"].get("datePosted")
        if cutoff_ts and date_posted:
            try:
                posted = datetime.fromisoformat(str(date_posted))
                if posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
                if int(posted.timestamp()) < cutoff_ts:
                    stale += 1
                    continue
            except ValueError:
                pass  # unknown age is not old (ADR-006)

        S.upsert_company(
            conn, company_slug,
            {**data["organization"], "board_token": board_token,
             "teams": data["teams"]},
            source=NAME,
        )
        raw = normalise(board_token, job, detail, company_name, team_name)
        if S.upsert_job(conn, nid, company_slug, raw, source=NAME):
            new += 1
        if role:
            S.add_provenance(conn, S.job_uid(NAME, nid), role, "board", 1)
        seen += 1
    conn.commit()

    return {"source": NAME, "company_slug": company_slug,
            "board_token": board_token, "ended_reason": "exhausted",
            "seen": seen, "new": new, "stale_skipped": stale,
            "filtered_out": filtered, "claimed": len(jobs),
            "filter_mode": "local"}


CORE_VIEW_SQL = """
SELECT
  'ashby'                                                 AS source,
  j.source_job_id                                         AS source_job_id,
  j.native_id                                             AS native_id,
  j.company_slug                                          AS company_slug,
  json_extract(j.raw_json,'$.title')                      AS title,
  COALESCE(json_extract(j.raw_json,'$._company_name'),
           json_extract(c.raw_json,'$.name'))             AS company,
  NULL                                                    AS company_size,
  NULL                                                    AS company_size_min,
  NULL                                                    AS high_concept,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$._badges')) AS badges,
  COALESCE(json_extract(j.raw_json,'$._posting.locationName'),
           json_extract(j.raw_json,'$.locationName'))     AS location_raw,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$._remote_locations')) AS remote_locations,
  CASE COALESCE(json_extract(j.raw_json,'$._posting.workplaceType'),
                json_extract(j.raw_json,'$.workplaceType'))
       WHEN 'OnSite' THEN 0 WHEN 'Remote' THEN 1 WHEN 'Hybrid' THEN 1 END AS remote,
  upper(COALESCE(json_extract(j.raw_json,'$._posting.workplaceType'),
                 json_extract(j.raw_json,'$.workplaceType'))) AS remote_config,
  CASE COALESCE(json_extract(j.raw_json,'$._posting.workplaceType'),
                json_extract(j.raw_json,'$.workplaceType'))
       WHEN 'OnSite' THEN 'Onsite'
       WHEN 'Remote' THEN 'Remote'
       WHEN 'Hybrid' THEN 'Onsite or Remote' END           AS remote_label,
  COALESCE(json_extract(j.raw_json,'$._posting.employmentType'),
           json_extract(j.raw_json,'$.employmentType'))    AS job_type,
  json_extract(j.raw_json,'$._salary_raw')                 AS salary_raw,
  CASE WHEN json_extract(j.raw_json,'$._salary_period') = 'annual'
       THEN json_extract(j.raw_json,'$._salary_min') END   AS salary_min_native,
  CASE WHEN json_extract(j.raw_json,'$._salary_period') = 'annual'
       THEN json_extract(j.raw_json,'$._salary_max') END   AS salary_max_native,
  json_extract(j.raw_json,'$._salary_currency')            AS salary_currency,
  json_extract(j.raw_json,'$._salary_period')              AS salary_period,
  CAST(strftime('%s',json_extract(j.raw_json,'$._linked_data.datePosted')) AS INTEGER) AS posted_ts,
  NULL                                                     AS expires_ts,
  json_extract(j.raw_json,'$._description_text')           AS description,
  'AtsIntegration::Ashby::Listing'                         AS ats_source,
  NULL                                                     AS primary_role_title,
  NULL                                                     AS auto_posted,
  json_extract(j.raw_json,'$._apply_url')                  AS apply_url,
  j.first_seen                                             AS first_seen,
  j.last_seen                                              AS last_seen
FROM job_raw j
LEFT JOIN company_raw c
  ON c.company_slug = j.company_slug AND c.source = 'ashby'
WHERE j.source = 'ashby'
"""

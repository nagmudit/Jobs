"""Greenhouse Job Board API — company-scoped, not searchable.

    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
    GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{id}

Docs (docs.greenhouse.io/job-board.html), verified 2026-09-04:
**"All endpoints require a specific `board_token` — there is no cross-board
search capability."** GET endpoints need no authentication; only application
submission does. No rate limit is stated.

## Why this source works differently from the others

Wellfound, RemoteOK and Himalayas answer "what jobs match this role". Greenhouse
answers "what is on this company's board". It cannot be role-searched at all, so
it is not fetched by role — it EXPANDS companies already in the corpus, and the
role filter is applied locally afterwards (ADR-010).

That expansion is the point: Wellfound shows at most 3 highlighted jobs per
company, and 264 companies in the corpus sit at exactly that cap. Measured on a
20-company sample: Astranis 81 jobs, Alpaca 59, Bitwarden 37 — all of which
appear on Wellfound as 3 or fewer.

## Shape notes, all verified live

* The whole board comes back in ONE request. No pagination; `meta.total` is the
  count.
* **No salary.** `pay_input_ranges` is absent from the list endpoint and was
  `null` on the single-job endpoint too. Greenhouse exposes it only when the
  employer fills it in, so treat salary as unavailable here rather than missing.
* `content` is HTML-ENTITY-ENCODED HTML (`&lt;p&gt;`), so it needs unescaping
  before tag-stripping. Storing it raw and unescaping in the view would double
  the work on every query, so it is normalised once at ingest -- the same
  exception `parse.py` already makes for Wellfound's `_badges`.
* `location.name` is free text and messy ("International ", "United States &
  EMEA"). `offices[].location` is cleaner where present.
* `first_published` / `updated_at` are ISO8601 with a UTC offset, which SQLite's
  `strftime('%s', ...)` parses correctly -- verified, so no injected epoch.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, Callable

from .. import assertions as A
from .. import store as S
from ..relevance import matches

NAME = "greenhouse"
BASE = "https://boards-api.greenhouse.io/v1/boards"


def board_url(token: str, content: bool = True) -> str:
    return f"{BASE}/{token}/jobs" + ("?content=true" if content else "")


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


def clean_content(raw: str | None) -> str | None:
    """Greenhouse double-encodes: entity-encoded HTML. Unescape, then strip."""
    if not raw:
        return None
    text = html.unescape(str(raw))
    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip() or None


def location_of(job: dict) -> str | None:
    """`offices[].location` where present -- it is structured. Fall back to the
    free-text `location.name`, which carries trailing spaces and ampersands."""
    offices = [o.get("location") or o.get("name") for o in (job.get("offices") or [])]
    offices = [str(o).strip() for o in offices if o]
    if offices:
        return ", ".join(dict.fromkeys(offices))
    name = (job.get("location") or {}).get("name")
    return str(name).strip() if name else None


def native_id(board_token: str, job: dict) -> str | None:
    jid = job.get("id")
    # Namespaced by board: Greenhouse ids look globally unique, but nothing in
    # the docs promises that, and a collision would silently merge two jobs.
    return f"{board_token}/{jid}" if jid else None


def normalise(board_token: str, job: dict) -> dict[str, Any]:
    """Store the payload with content decoded and location flattened.

    A deliberate, documented exception to raw-first (ADR-002): decoding
    double-encoded HTML on every query is wasteful, and the original is
    recoverable from the API at any time.
    """
    out = dict(job)
    out["_board_token"] = board_token
    out["_content_text"] = clean_content(job.get("content"))
    out["_location"] = location_of(job)
    out["_departments"] = [d.get("name") for d in (job.get("departments") or [])
                           if d.get("name")]
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
    """Pull one company's whole board. One request.

    `role` is recorded as provenance so expanded jobs appear in the same
    found_via_roles vocabulary as every other source -- without it they are
    invisible to that facet.
    """
    url = board_url(board_token)
    resp = fetcher.get(url, max_age=fetcher.listing_ttl)
    if not resp.ok:
        return {"source": NAME, "company_slug": company_slug, "board_token": board_token,
                "ended_reason": f"http_{resp.status}", "seen": 0, "new": 0,
                "stale_skipped": 0, "filtered_out": 0, "claimed": None}

    data = parse(resp.text, url)
    jobs = data["jobs"]
    seen = new = stale = filtered = 0

    for j in jobs:
        nid = native_id(board_token, j)
        if not nid:
            continue
        # Ages come from first_published; a board with no date is kept, since
        # unknown age is not old (ADR-006).
        ts = j.get("first_published") or j.get("updated_at")
        if cutoff_ts and ts:
            try:
                from datetime import datetime

                if int(datetime.fromisoformat(str(ts)).timestamp()) < cutoff_ts:
                    stale += 1
                    continue
            except ValueError:
                pass  # unparseable date -> keep, do not guess
        if not matches(j.get("title"), (j.get("departments") or []) and
                       [d.get("name") for d in j["departments"]], keywords):
            filtered += 1
            continue
        S.upsert_company(conn, company_slug,
                         {"name": j.get("company_name"), "board_token": board_token},
                         source=NAME)
        if S.upsert_job(conn, nid, company_slug, normalise(board_token, j), source=NAME):
            new += 1
        if role:
            S.add_provenance(conn, S.job_uid(NAME, nid), role, "board", 1)
        seen += 1
    conn.commit()

    return {"source": NAME, "company_slug": company_slug, "board_token": board_token,
            "ended_reason": "exhausted", "seen": seen, "new": new,
            "stale_skipped": stale, "filtered_out": filtered,
            "claimed": (data.get("meta") or {}).get("total", len(jobs)),
            "filter_mode": "local"}


CORE_VIEW_SQL = """
SELECT
  'greenhouse'                                             AS source,
  j.source_job_id                                          AS source_job_id,
  j.native_id                                              AS native_id,
  j.company_slug                                           AS company_slug,
  json_extract(j.raw_json,'$.title')                       AS title,
  COALESCE(json_extract(j.raw_json,'$.company_name'),
           json_extract(c.raw_json,'$.name'))              AS company,
  NULL                                                     AS company_size,
  NULL                                                     AS company_size_min,
  NULL                                                     AS high_concept,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$._departments')) AS badges,
  NULLIF(json_extract(j.raw_json,'$._location'),'')        AS location_raw,
  NULL                                                     AS remote_locations,
  -- Greenhouse has no remote flag; infer only from an explicit location word,
  -- and leave it NULL rather than guessing "onsite".
  CASE WHEN lower(COALESCE(json_extract(j.raw_json,'$._location'),'')) LIKE '%remote%'
       THEN 1 END                                          AS remote,
  NULL                                                     AS remote_config,
  CASE WHEN lower(COALESCE(json_extract(j.raw_json,'$._location'),'')) LIKE '%remote%'
       THEN 'Remote' END                                   AS remote_label,
  NULL                                                     AS job_type,
  -- No salary: pay_input_ranges is absent from the list endpoint and was null
  -- on the single-job endpoint too.
  NULL                                                     AS salary_raw,
  NULL                                                     AS salary_min_native,
  NULL                                                     AS salary_max_native,
  NULL                                                     AS salary_currency,
  NULL                                                     AS salary_period,
  -- ISO8601 with a UTC offset; SQLite parses it correctly (verified).
  CAST(strftime('%s', json_extract(j.raw_json,'$.first_published')) AS INTEGER) AS posted_ts,
  NULL                                                     AS expires_ts,
  json_extract(j.raw_json,'$._content_text')               AS description,
  'AtsIntegration::Greenhouse::Listing'                    AS ats_source,
  NULL                                                     AS primary_role_title,
  NULL                                                     AS auto_posted,
  json_extract(j.raw_json,'$.absolute_url')                AS apply_url,
  j.first_seen                                             AS first_seen,
  j.last_seen                                              AS last_seen
FROM job_raw j
LEFT JOIN company_raw c
  ON c.company_slug = j.company_slug AND c.source = 'greenhouse'
WHERE j.source = 'greenhouse'
"""

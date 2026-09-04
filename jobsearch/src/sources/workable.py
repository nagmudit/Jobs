"""Workable job board API — company-scoped, keyless, not searchable.

    GET https://apply.workable.com/api/v1/widget/accounts/{token}
    GET https://apply.workable.com/api/v1/widget/accounts/{token}?details=true

Probed live 2026-09-05. No authentication, no API key, no stated rate limit.
Like Greenhouse and Ashby this is company-scoped with no cross-board search, so
it EXPANDS companies the role already touched rather than being role-fetched
(ADR-010).

## Two origins, and only one of them may be touched

    apply.workable.com   robots.txt: 0 rules -- everything allowed
    www.workable.com     robots.txt: DISALLOWS /j/
    jobs.workable.com    robots.txt: disallows /search

`www.workable.com/j/{shortcode}` and `apply.workable.com/j/{shortcode}` are the
same path shape on different hosts, and only the second is permitted. This is
the same two-origin trap Ashby set (ADR-010), and it is why `Fetcher` caches
robots per origin (ADR-007). Everything here is built from `BASE`, which is
`apply.workable.com`, and nothing in this module may reach for `www.`.

## Shape notes, all verified live

* **The whole board comes back in ONE request**, and `?details=true` includes the
  full `description` inline -- 55 jobs / 635 KB for `lawnstarter`, description
  non-empty on 55/55. Descriptions therefore cost NOTHING extra here, unlike
  Ashby, where the board summary carries no date and forces a fetch per posting.
* **`published_on` is on every job** (55/55), as a plain `YYYY-MM-DD`. SQLite's
  `strftime('%s', ...)` parses it, so no epoch is injected.
* **A missing board is an honest 404.** `thisboardshouldnotexist99z` returns
  HTTP 404 `Not Found`. Ashby's silent-200-with-nulls problem does not exist
  here, so this module needs no `BoardNotFound` equivalent.
* **An empty board is a 200 carrying the right name**:
  `{"name":"OptiSigns","description":null,"jobs":[]}`. That is a real resolution
  -- the company simply has nothing open -- and must not be read as a miss.
* `telecommuting` is a real boolean, so `remote_label` can honestly say Onsite.
  Greenhouse has to leave that NULL because it only has free text to go on.
* **No salary of any kind.** Absent from the payload, so the salary columns are
  NULL rather than guessed.
* `description` is plain HTML (`<h3>About ...`), NOT entity-encoded. It needs a
  single tag-strip -- Greenhouse's unescape-then-strip would be wrong here.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from .. import assertions as A
from .. import store as S
from ..relevance import matches

NAME = "workable"
# apply.workable.com ONLY. See the two-origin note above.
BASE = "https://apply.workable.com/api/v1/widget/accounts"


def board_url(token: str, content: bool = True) -> str:
    """`content=False` is the cheap 37 KB summary used for token resolution;
    `content=True` is the 635 KB payload with descriptions, used for expansion.
    Mirrors Greenhouse's flag so `ats.resolve` needs no special case."""
    return f"{BASE}/{token}" + ("?details=true" if content else "")


def parse(payload: str, url: str) -> dict[str, Any]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise A.SchemaDrift(f"{url}: response is not JSON: {e}") from e
    if not isinstance(data, dict) or "jobs" not in data:
        keys = sorted(data)[:12] if isinstance(data, dict) else type(data).__name__
        raise A.SchemaDrift(f"{url}: expected an object with 'jobs'; got {keys}")
    if not isinstance(data["jobs"], list):
        raise A.SchemaDrift(
            f"{url}: 'jobs' is {type(data['jobs']).__name__}, not a list")
    return data


def clean_content(raw: str | None) -> str | None:
    """Plain HTML, single strip. Workable does NOT entity-encode its markup, so
    unescaping first (as Greenhouse needs) would corrupt any literal &lt; the
    employer actually wrote."""
    if not raw:
        return None
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", str(raw), flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip() or None


def location_of(job: dict) -> str | None:
    """`locations[]` is structured; the flat city/state/country fields are the
    fallback and are often empty strings rather than null."""
    parts: list[str] = []
    for loc in job.get("locations") or []:
        if not isinstance(loc, dict) or loc.get("hidden"):
            continue
        bits = [str(loc.get(k)).strip() for k in ("city", "region", "country")
                if loc.get(k)]
        if bits:
            parts.append(", ".join(bits))
    if parts:
        return "; ".join(dict.fromkeys(parts))
    flat = [str(job.get(k)).strip() for k in ("city", "state", "country")
            if str(job.get(k) or "").strip()]
    return ", ".join(flat) or None


def collapse(jobs: list[dict]) -> list[dict]:
    """One entry per posting, not one per location.

    Workable emits a SEPARATE array entry for every location a posting is open
    in, all carrying the same `shortcode`. lawnstarter's board returns 55 entries
    for 10 distinct postings. Counting the entries inflates every telemetry
    number 5.5x, and letting the last one win silently discards the other
    locations -- so they are merged instead.
    """
    out: dict[str, dict] = {}
    for j in jobs:
        sc = j.get("shortcode")
        if not sc:
            continue
        if sc not in out:
            out[sc] = dict(j)
            out[sc]["locations"] = list(j.get("locations") or [])
            continue
        # Same posting, another location. Keep the first entry's fields; append
        # the location so nothing is lost.
        out[sc]["locations"].extend(j.get("locations") or [])
        for k in ("city", "state", "country"):
            if not str(out[sc].get(k) or "").strip():
                out[sc][k] = j.get(k)
    return list(out.values())


def posted_ts_of(job: dict) -> int | None:
    """Unix seconds for a posting, or None when it cannot be dated.

    `published_on` is a bare `YYYY-MM-DD`. Two traps, both hit live:

    * `datetime.fromisoformat` returns a NAIVE datetime and `.timestamp()` then
      reads it in the machine's local zone, so a board would go stale at
      different moments in different timezones. The `jobs` view uses
      `strftime('%s', ...)`, which is UTC; ingest must agree with the view.
    * A bare date has an unknown time of day. Reading it as midnight makes a job
      posted that afternoon look up to 24 h older than it is. Unknown age is not
      old (ADR-006), so an unknown time resolves to the END of the day.
    """
    raw = job.get("published_on") or job.get("created_at")
    if not raw:
        return None
    from datetime import datetime, time as _time, timezone as _tz

    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None                    # unparseable -> undatable, never guessed
    if parsed.timetz() == _time(0, 0) and len(str(raw)) == 10:
        parsed = datetime.combine(parsed.date(), _time(23, 59, 59))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_tz.utc)
    return int(parsed.timestamp())


def native_id(board_token: str, job: dict) -> str | None:
    sc = job.get("shortcode")
    # Namespaced by board, as with Greenhouse: shortcodes look globally unique
    # but nothing documents that, and a collision would silently merge two
    # companies' jobs.
    return f"{board_token}/{sc}" if sc else None


def verify_account(data: dict, company_name: str | None) -> bool:
    """Does this board actually belong to the company we asked about?

    `ats.resolve` binds the FIRST candidate token that returns 200, so a token
    collision would silently attach another company's whole board to ours.
    Workable is the only provider that returns the account name, so it is the
    only one that can check. Absent a name to compare against, accept -- this
    guard exists to catch a wrong match, not to reject an unverifiable one.
    """
    account = str((data or {}).get("name") or "").strip()
    if not account or not company_name:
        return True
    a, b = _norm(account), _norm(str(company_name))
    return a == b or a.startswith(b) or b.startswith(a)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def normalise(board_token: str, job: dict) -> dict[str, Any]:
    """Store the payload with the description decoded and location flattened.

    The same documented exception to raw-first that Greenhouse makes (ADR-002):
    stripping HTML on every query is wasteful and the original is re-fetchable.
    """
    out = dict(job)
    out["_board_token"] = board_token
    out["_content_text"] = clean_content(job.get("description"))
    out["_location"] = location_of(job)
    # department and function are often the same word ("Engineering",
    # "Engineering"); dict.fromkeys dedupes while keeping order.
    out["_tags"] = list(dict.fromkeys(
        v for v in (job.get("department"), job.get("function")) if v))
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
    """Pull one company's whole board, descriptions included. ONE request.

    `role` is recorded as provenance so expanded jobs join the same
    found_via_roles vocabulary as every other source (ADR-009).
    """
    url = board_url(board_token)
    resp = fetcher.get(url, max_age=fetcher.listing_ttl)
    if not resp.ok:
        # 404 here means the cached token has gone dead. Report it and let the
        # run continue -- one dead board must not abort the remaining companies.
        return {"source": NAME, "company_slug": company_slug,
                "board_token": board_token,
                "ended_reason": f"http_{resp.status}", "seen": 0, "new": 0,
                "stale_skipped": 0, "filtered_out": 0, "claimed": None,
                "filter_mode": "local"}

    data = parse(resp.text, url)
    # One entry per LOCATION comes back; collapse to one per posting first, or
    # every count below is inflated several-fold.
    jobs = collapse(data["jobs"])
    seen = new = stale = filtered = 0

    for j in jobs:
        nid = native_id(board_token, j)
        if not nid:
            continue
        # A posting with no usable date is KEPT: unknown age is not old (ADR-006).
        ts = posted_ts_of(j)
        if cutoff_ts and ts is not None:
            if ts < cutoff_ts:
                stale += 1
                continue
        # Title plus department/function only. Never the description: it names
        # every technology the company uses and would match everything.
        if not matches(j.get("title"),
                       [v for v in (j.get("department"), j.get("function")) if v],
                       keywords):
            filtered += 1
            continue
        S.upsert_company(conn, company_slug,
                         {"name": data.get("name"), "board_token": board_token},
                         source=NAME)
        if S.upsert_job(conn, nid, company_slug, normalise(board_token, j),
                        source=NAME):
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
  'workable'                                               AS source,
  j.source_job_id                                          AS source_job_id,
  j.native_id                                              AS native_id,
  j.company_slug                                           AS company_slug,
  json_extract(j.raw_json,'$.title')                       AS title,
  json_extract(c.raw_json,'$.name')                        AS company,
  NULL                                                     AS company_size,
  NULL                                                     AS company_size_min,
  NULL                                                     AS high_concept,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$._tags')) AS badges,
  NULLIF(json_extract(j.raw_json,'$._location'),'')        AS location_raw,
  NULL                                                     AS remote_locations,
  -- telecommuting is a real boolean, so unlike Greenhouse this is never a guess.
  CASE WHEN json_extract(j.raw_json,'$.telecommuting') THEN 1 ELSE 0 END AS remote,
  NULL                                                     AS remote_config,
  CASE WHEN json_extract(j.raw_json,'$.telecommuting') THEN 'Remote'
       ELSE 'Onsite' END                                   AS remote_label,
  NULLIF(json_extract(j.raw_json,'$.employment_type'),'')  AS job_type,
  -- Workable publishes no salary at all; NULL beats a guess.
  NULL                                                     AS salary_raw,
  NULL                                                     AS salary_min_native,
  NULL                                                     AS salary_max_native,
  NULL                                                     AS salary_currency,
  NULL                                                     AS salary_period,
  -- published_on is a bare YYYY-MM-DD, so the time of day is unknown and
  -- resolves to the END of the day -- unknown age is not old (ADR-006). This
  -- MUST match posted_ts_of(), which decides what ingest keeps: if the view
  -- dated a posting at midnight instead, a job could be stored and then
  -- immediately hidden by the UI's age filter with nothing to explain it.
  CAST(strftime('%s',
    CASE WHEN length(json_extract(j.raw_json,'$.published_on')) = 10
         THEN json_extract(j.raw_json,'$.published_on') || 'T23:59:59'
         ELSE json_extract(j.raw_json,'$.published_on') END) AS INTEGER) AS posted_ts,
  NULL                                                     AS expires_ts,
  json_extract(j.raw_json,'$._content_text')               AS description,
  'AtsIntegration::Workable::Listing'                      AS ats_source,
  NULL                                                     AS primary_role_title,
  NULL                                                     AS auto_posted,
  COALESCE(json_extract(j.raw_json,'$.application_url'),
           json_extract(j.raw_json,'$.url'))               AS apply_url,
  j.first_seen                                             AS first_seen,
  j.last_seen                                              AS last_seen
FROM job_raw j
LEFT JOIN company_raw c
  ON c.company_slug = j.company_slug AND c.source = 'workable'
WHERE j.source = 'workable'
"""

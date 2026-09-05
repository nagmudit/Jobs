"""Lever Postings API — company-scoped, keyless, not searchable.

    GET https://api.lever.co/v0/postings/{site}?mode=json

Probed live 2026-09-06 against the official `lever/postings-api` documentation.
No authentication for public listings. Like Greenhouse, Ashby and Workable this
is company-scoped with no cross-board search, so it EXPANDS companies the role
already touched rather than being role-fetched (ADR-010).

## Conduct

`api.lever.co/robots.txt` is readable and allows `/`. `jobs.lever.co` (which
`hostedUrl` and `applyUrl` point at, and which we only ever link to) likewise
allows `/`. **`www.lever.co` disallows `/api/`** — a third host with the same
path shape, and one this module must never touch. Same two-origin trap as Ashby
and Workable; everything here is built from `BASE`.

**Lever is the first provider to publish a rate limit: 2 requests per second.**
The mandated 3-5 s spacing is roughly an order of magnitude under it, so no
special handling is needed — but it is recorded here so nobody later "optimises"
toward that documented ceiling and away from this repo's own, stricter rule.

## Shape notes, all verified live

* **The whole board comes back in ONE request** with descriptions inline. No
  pagination is needed at these sizes (`skip`/`limit` exist; the largest corpus
  board was 59 postings).
* **Success is a JSON ARRAY. An error is a JSON OBJECT** —
  `{"ok":false,"error":"Document not found"}` with HTTP 404 for an unknown site.
  Both parse as valid JSON, so `parse` requires a list; checking only that the
  body is JSON would read an error as a board.
* **`createdAt` is epoch MILLISECONDS** (`1605753685375` -> 2020-11-19). Treated
  as seconds it lands 50,000 years in the future and every job looks fresh
  forever; handed to SQLite's `strftime('%s', ...)` it does not parse at all.
* **`createdAt` is a creation date, not a publish date.** Boards keep old
  postings: metabase's 18 postings span 2020-05 to 2026-07 with a median age of
  319 days and **nothing** inside the 30-day cutoff. That is genuine — those
  requisitions really are old — so the cutoff does most of the filtering here,
  exactly as it does on Ashby.
* **`descriptionPlain` is already plain text**, unlike every other source. The
  full posting is `descriptionPlain` + the `lists` blocks (whose `content` IS
  HTML) + `additionalPlain`.
* **Salary is structured and states its interval**:
  `{min, max, currency, interval}` with `interval: "per-year-salary"`. Numeric
  values are emitted only for a yearly interval, per the repo-wide rule. It is
  rare — 1 of 18 postings on metabase.
* **`workplaceType` is explicit**: `onsite` / `remote` / `hybrid` /
  `unspecified`. The docs spell the first `on-site`; live data sends `onsite`.
  Both are accepted.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from .. import assertions as A
from .. import store as S
from ..relevance import matches

NAME = "lever"
# api.lever.co ONLY. Never www.lever.co, which disallows /api/.
BASE = "https://api.lever.co/v0/postings"

# Lever's own vocabulary -> the canonical labels used across every source.
_WORKPLACE = {
    "onsite": "Onsite",
    "on-site": "Onsite",
    "remote": "Remote",
    "hybrid": "Onsite or Remote",
}


def board_url(token: str, content: bool = True) -> str:
    """`content` is accepted for interface parity with the other ATS modules and
    ignored: Lever has no cheap summary variant, so resolution and expansion
    fetch the same URL. That is fine because `ats.resolve` caches the answer."""
    return f"{BASE}/{token}?mode=json"


def parse(payload: str, url: str) -> dict[str, Any]:
    """Postings, in the registry's shape. Raises on a shape change.

    Lever's wire format is a bare ARRAY, but every other source's `parse`
    returns an object carrying `jobs`, and `ats.resolve` counts `data["jobs"]`
    for all of them. Adapting here keeps the resolver free of per-provider
    special cases -- returning the raw list makes `resolve` die with
    `AttributeError: 'list' object has no attribute 'get'` and takes the whole
    run down with it.

    A LIST is required on the wire, not merely valid JSON: Lever's 404 body is a
    perfectly well-formed object, so accepting any JSON would turn "no such
    board" into "a board with strange fields".
    """
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise A.SchemaDrift(f"{url}: response is not JSON: {e}") from e
    if isinstance(data, dict):
        err = data.get("error") or sorted(data)[:8]
        raise A.SchemaDrift(
            f"{url}: expected a list of postings, got an object ({err!r})")
    if not isinstance(data, list):
        raise A.SchemaDrift(
            f"{url}: expected a list of postings, got {type(data).__name__}")
    return {"jobs": data}


def clean_content(raw: str | None) -> str | None:
    if not raw:
        return None
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", str(raw), flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip() or None


def description_of(job: dict) -> str | None:
    """The whole posting, assembled.

    `descriptionPlain` is only the opening; the requirements and benefits live
    in `lists`, whose `content` is HTML even though the description is not.
    """
    parts = [str(job.get("descriptionPlain") or "").strip()]
    for block in job.get("lists") or []:
        if not isinstance(block, dict):
            continue
        head = str(block.get("text") or "").strip()
        body = clean_content(block.get("content")) or ""
        if head or body:
            parts.append(f"{head}\n{body}".strip())
    parts.append(str(job.get("additionalPlain") or "").strip())
    return "\n\n".join(p for p in parts if p) or None


def location_of(job: dict) -> str | None:
    cats = job.get("categories") or {}
    all_locs = [str(x).strip() for x in (cats.get("allLocations") or []) if x]
    if all_locs:
        return "; ".join(dict.fromkeys(all_locs))
    loc = cats.get("location")
    return str(loc).strip() if loc else None


def posted_ts_of(job: dict) -> int | None:
    """`createdAt` is epoch MILLISECONDS. Seconds would put it ~50,000 years out
    and make every posting permanently fresh."""
    ms = job.get("createdAt")
    if ms is None:
        return None
    try:
        return int(float(ms) / 1000)
    except (TypeError, ValueError):
        return None


def salary_of(job: dict) -> dict[str, Any]:
    """Structured salary, numeric ONLY for a yearly interval.

    Lever states the interval (`per-year-salary`), so unlike Greenhouse there is
    no guessing -- and unlike Himalayas there is no risk of an hourly figure
    landing in an annual column.
    """
    sr = job.get("salaryRange") or {}
    if not isinstance(sr, dict) or sr.get("min") is None:
        return {"raw": None, "min": None, "max": None,
                "currency": None, "period": None}
    interval = str(sr.get("interval") or "").lower()
    period = ("annual" if "year" in interval
              else "hourly" if "hour" in interval
              else interval or None)
    cur = sr.get("currency")
    lo, hi = sr.get("min"), sr.get("max")
    raw = f"{cur or ''} {lo:,}".strip() + (f" – {hi:,}" if hi else "")
    annual = period == "annual"
    return {"raw": raw + ("" if annual else f" ({period})"),
            # Non-annual keeps the readable string and NULL numerics: mixing
            # periods in one sortable column is silently wrong.
            "min": lo if annual else None,
            "max": hi if annual else None,
            "currency": cur, "period": period}


def native_id(board_token: str, job: dict) -> str | None:
    jid = job.get("id")
    # Namespaced by board like every other ATS source: the ids are UUIDs and
    # look globally unique, but nothing documents that.
    return f"{board_token}/{jid}" if jid else None


def normalise(board_token: str, job: dict) -> dict[str, Any]:
    """Store the payload with description assembled and fields flattened.

    The same documented exception to raw-first the other ATS modules make
    (ADR-002): re-deriving on every query is wasteful and the original is
    re-fetchable.
    """
    cats = job.get("categories") or {}
    sal = salary_of(job)
    out = dict(job)
    out["_board_token"] = board_token
    out["_content_text"] = description_of(job)
    out["_location"] = location_of(job)
    out["_tags"] = list(dict.fromkeys(
        v for v in (cats.get("team"), cats.get("department")) if v))
    out["_posted_ts"] = posted_ts_of(job)
    out["_salary"] = sal
    out["_remote_label"] = _WORKPLACE.get(
        str(job.get("workplaceType") or "").lower())
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
    """Pull one company's whole board, descriptions included. ONE request."""
    url = board_url(board_token)
    resp = fetcher.get(url, max_age=fetcher.listing_ttl)
    if not resp.ok:
        # 404 means the cached token has gone dead. Report and continue -- one
        # dead board must not abort the remaining companies.
        return {"source": NAME, "company_slug": company_slug,
                "board_token": board_token,
                "ended_reason": f"http_{resp.status}", "seen": 0, "new": 0,
                "stale_skipped": 0, "filtered_out": 0, "claimed": None,
                "filter_mode": "local"}

    jobs = parse(resp.text, url)["jobs"]
    seen = new = stale = filtered = 0

    for j in jobs:
        nid = native_id(board_token, j)
        if not nid:
            continue
        # A posting with no usable date is KEPT: unknown age is not old (ADR-006).
        ts = posted_ts_of(j)
        if cutoff_ts and ts is not None and ts < cutoff_ts:
            stale += 1
            continue
        cats = j.get("categories") or {}
        # Title plus team/department only -- never the description, which names
        # every technology the company uses.
        if not matches(j.get("text"),
                       [v for v in (cats.get("team"), cats.get("department")) if v],
                       keywords):
            filtered += 1
            continue
        S.upsert_company(conn, company_slug,
                         {"name": None, "board_token": board_token}, source=NAME)
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
  'lever'                                                  AS source,
  j.source_job_id                                          AS source_job_id,
  j.native_id                                              AS native_id,
  j.company_slug                                           AS company_slug,
  json_extract(j.raw_json,'$.text')                        AS title,
  -- Lever's payload carries no company name; the board token is the only
  -- identifier it gives us, so the Wellfound row's name is preferred.
  COALESCE(json_extract(c.raw_json,'$.name'),
           json_extract(j.raw_json,'$._board_token'))      AS company,
  NULL                                                     AS company_size,
  NULL                                                     AS company_size_min,
  NULL                                                     AS high_concept,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$._tags')) AS badges,
  NULLIF(json_extract(j.raw_json,'$._location'),'')        AS location_raw,
  NULL                                                     AS remote_locations,
  CASE json_extract(j.raw_json,'$._remote_label')
       WHEN 'Remote' THEN 1 WHEN 'Onsite' THEN 0 END       AS remote,
  NULLIF(json_extract(j.raw_json,'$.workplaceType'),'')    AS remote_config,
  json_extract(j.raw_json,'$._remote_label')               AS remote_label,
  NULLIF(json_extract(j.raw_json,'$.categories.commitment'),'') AS job_type,
  json_extract(j.raw_json,'$._salary.raw')                 AS salary_raw,
  json_extract(j.raw_json,'$._salary.min')                 AS salary_min_native,
  json_extract(j.raw_json,'$._salary.max')                 AS salary_max_native,
  json_extract(j.raw_json,'$._salary.currency')            AS salary_currency,
  json_extract(j.raw_json,'$._salary.period')              AS salary_period,
  -- createdAt is epoch MILLISECONDS; the division happens at ingest and the
  -- result is stored, so the view must NOT divide again.
  json_extract(j.raw_json,'$._posted_ts')                  AS posted_ts,
  NULL                                                     AS expires_ts,
  json_extract(j.raw_json,'$._content_text')               AS description,
  'AtsIntegration::Lever::Listing'                         AS ats_source,
  NULL                                                     AS primary_role_title,
  NULL                                                     AS auto_posted,
  COALESCE(json_extract(j.raw_json,'$.applyUrl'),
           json_extract(j.raw_json,'$.hostedUrl'))         AS apply_url,
  j.first_seen                                             AS first_seen,
  j.last_seen                                              AS last_seen
FROM job_raw j
LEFT JOIN company_raw c
  ON c.company_slug = j.company_slug AND c.source = 'lever'
WHERE j.source = 'lever'
"""

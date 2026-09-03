"""SQLite store: persist raw Apollo nodes verbatim, derive columns on top.

Phase 1 normalized into a 12-field Job at ingest and threw the rest away --
companySize, badges, highConcept, jobType, remoteConfig, liveStartAt, atsSource,
primaryRoleTitle were all sitting in the Apollo cache and were discarded.

The rule here is the inverse: store the raw node untouched, and express every
filterable column as a VIEW over json_extract. Adding a filter dimension six
weeks from now is then a view change, not a re-crawl of thousands of pages.

Everything is resumable. Stages checkpoint after each unit of work, so a halt
(including a deliberate mitigation halt) costs minutes rather than hours.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "wellfound.db"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------- raw layer: never lossy, never reshaped ----------

CREATE TABLE IF NOT EXISTS job_raw (
  source_job_id TEXT PRIMARY KEY,
  company_slug  TEXT NOT NULL,
  raw_json      TEXT NOT NULL,      -- JobListingSearchResult node, untouched
  origin        TEXT NOT NULL,      -- 'search' | 'company' | 'detail'
  query_key     TEXT,               -- GraphQL cache key that produced it
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_raw (
  company_slug TEXT PRIMARY KEY,
  raw_json     TEXT NOT NULL,
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS detail_raw (
  source_job_id   TEXT PRIMARY KEY,
  json_ld         TEXT,
  rendered_fields TEXT,
  fetched_at      TEXT NOT NULL
);

-- ---------- provenance: one job can be found by many queries ----------

CREATE TABLE IF NOT EXISTS job_provenance (
  source_job_id TEXT NOT NULL,
  query_key     TEXT NOT NULL,
  role_slug     TEXT,
  location_slug TEXT,
  page          INTEGER,
  origin        TEXT NOT NULL,
  seen_at       TEXT NOT NULL,
  PRIMARY KEY (source_job_id, query_key, origin)
);

-- ---------- stage A: the role catalogue ----------

CREATE TABLE IF NOT EXISTS role_slug (
  role_slug       TEXT NOT NULL,
  location_slug   TEXT NOT NULL,    -- '' means the remote (/role/r/) variant
  valid           INTEGER NOT NULL, -- 1 = server applied the role filter
  total_job_count INTEGER,
  page_count      INTEGER,
  reason          TEXT,
  page_type       TEXT,
  checked_at      TEXT NOT NULL,
  PRIMARY KEY (role_slug, location_slug)
);

CREATE TABLE IF NOT EXISTS role_candidate (
  role_slug  TEXT PRIMARY KEY,
  found_via  TEXT,
  depth      INTEGER,
  seen_at    TEXT NOT NULL
);

-- ---------- stage E: ATS annotation (a filter dimension, NOT a data source) ----------

CREATE TABLE IF NOT EXISTS company_ats (
  company_slug   TEXT PRIMARY KEY,
  provider       TEXT,
  board_slug     TEXT,
  ats_board_url  TEXT,
  live_job_count INTEGER,
  confidence     TEXT,
  resolved       INTEGER NOT NULL,
  checked_at     TEXT NOT NULL
);

-- ---------- crawl bookkeeping: resumability + the mitigation log ----------

CREATE TABLE IF NOT EXISTS crawl_unit (
  stage      TEXT NOT NULL,
  unit_key   TEXT NOT NULL,   -- e.g. 'ai-engineer|bangalore|3'
  status     TEXT NOT NULL,   -- 'done' | 'exhausted' | 'failed'
  detail     TEXT,
  n_items    INTEGER,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (stage, unit_key)
);

CREATE TABLE IF NOT EXISTS request_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  stage         TEXT,
  url           TEXT NOT NULL,
  status        INTEGER,
  cf_ray        TEXT,
  cf_mitigated  TEXT,
  bytes         INTEGER,
  elapsed_ms    INTEGER,
  from_cache    INTEGER,
  fetched_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_job_company   ON job_raw(company_slug);
CREATE INDEX IF NOT EXISTS ix_job_origin    ON job_raw(origin);
CREATE INDEX IF NOT EXISTS ix_prov_job      ON job_provenance(source_job_id);
CREATE INDEX IF NOT EXISTS ix_prov_role     ON job_provenance(role_slug);
CREATE INDEX IF NOT EXISTS ix_req_mitigated ON request_log(cf_mitigated);
"""

# The derived layer. Everything filterable is expressed here, over the raw JSON,
# so a new filter dimension costs a view rebuild rather than a crawl.
VIEWS = """
DROP VIEW IF EXISTS jobs;
CREATE VIEW jobs AS
SELECT
  j.source_job_id                                              AS source_job_id,
  j.company_slug                                               AS company_slug,
  json_extract(c.raw_json, '$.name')                           AS company,
  json_extract(c.raw_json, '$.companySize')                    AS company_size,
  json_extract(c.raw_json, '$.highConcept')                    AS company_pitch,
  json_extract(j.raw_json, '$.title')                          AS title,
  json_extract(j.raw_json, '$.primaryRoleTitle')               AS primary_role,
  json_extract(j.raw_json, '$.jobType')                        AS job_type,
  json_extract(j.raw_json, '$.compensation')                   AS salary_raw,
  json_extract(d.rendered_fields, '$.equity_raw')              AS equity_raw,
  COALESCE(
    json_extract(d.rendered_fields, '$.salary_raw'),
    json_extract(j.raw_json, '$.compensation')
  )                                                            AS salary_best,
  json_extract(j.raw_json, '$.remote')                         AS remote,
  json_extract(j.raw_json, '$.remoteConfig.kind')              AS remote_kind,
  json_extract(j.raw_json, '$.remoteConfig.wfhFlexible')       AS wfh_flexible,
  (SELECT group_concat(value, ', ')
     FROM json_each(j.raw_json, '$.locationNames'))            AS location_raw,
  (SELECT group_concat(value, ', ')
     FROM json_each(j.raw_json, '$.acceptedRemoteLocationNames')) AS remote_locations,
  json_extract(j.raw_json, '$.liveStartAt')                    AS live_start_ts,
  datetime(json_extract(j.raw_json, '$.liveStartAt'), 'unixepoch') AS posted_at,
  json_extract(j.raw_json, '$.atsSource')                      AS ats_source,
  json_extract(j.raw_json, '$.autoPosted')                     AS auto_posted,
  json_extract(j.raw_json, '$.description')                    AS description,
  'https://wellfound.com/jobs/' || j.source_job_id || '-' ||
      COALESCE(json_extract(j.raw_json, '$.slug'), '')         AS apply_url,
  a.ats_board_url                                              AS ats_board_url,
  a.provider                                                   AS ats_provider,
  a.live_job_count                                             AS ats_live_jobs,
  j.origin                                                     AS origin,
  (SELECT COUNT(*) FROM job_provenance p
     WHERE p.source_job_id = j.source_job_id)                  AS n_provenance,
  (SELECT group_concat(DISTINCT p.role_slug) FROM job_provenance p
     WHERE p.source_job_id = j.source_job_id)                  AS found_via_roles,
  (d.source_job_id IS NOT NULL)                                AS has_detail,
  j.first_seen                                                 AS first_seen,
  j.last_seen                                                  AS last_seen
FROM job_raw j
LEFT JOIN company_raw c ON c.company_slug = j.company_slug
LEFT JOIN detail_raw  d ON d.source_job_id = j.source_job_id
LEFT JOIN company_ats a ON a.company_slug = j.company_slug;

DROP VIEW IF EXISTS companies;
CREATE VIEW companies AS
SELECT
  c.company_slug,
  json_extract(c.raw_json, '$.name')        AS name,
  json_extract(c.raw_json, '$.companySize') AS company_size,
  json_extract(c.raw_json, '$.highConcept') AS pitch,
  (SELECT COUNT(*) FROM job_raw j WHERE j.company_slug = c.company_slug) AS n_jobs,
  a.provider, a.ats_board_url, a.live_job_count, a.confidence
FROM company_raw c
LEFT JOIN company_ats a ON a.company_slug = c.company_slug;
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executescript(VIEWS)
    conn.commit()
    return conn


# --- writers ------------------------------------------------------------------


def log_request(conn: sqlite3.Connection, stage: str, resp: Any) -> None:
    """Every response, unconditionally. cf-ray/cf-mitigated on all of them --
    this table is the evidence base for 'did we ever get mitigated'."""
    h = resp.headers or {}
    conn.execute(
        "INSERT INTO request_log (stage,url,status,cf_ray,cf_mitigated,bytes,"
        "elapsed_ms,from_cache,fetched_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            stage, resp.url, resp.status, h.get("cf-ray"), h.get("cf-mitigated"),
            len(resp.text or ""), resp.elapsed_ms, int(bool(resp.from_cache)),
            resp.fetched_at,
        ),
    )


def upsert_job(
    conn: sqlite3.Connection,
    job_id: str,
    company_slug: str,
    raw: dict,
    origin: str,
    query_key: str | None,
) -> bool:
    """Returns True if this row is new. Re-seeing a job updates last_seen and,
    when the payload is richer (company/detail origin), replaces raw_json."""
    ts = now()
    cur = conn.execute("SELECT origin FROM job_raw WHERE source_job_id=?", (job_id,))
    row = cur.fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO job_raw (source_job_id,company_slug,raw_json,origin,"
            "query_key,first_seen,last_seen) VALUES (?,?,?,?,?,?,?)",
            (job_id, company_slug, json.dumps(raw, sort_keys=True), origin, query_key, ts, ts),
        )
        return True
    # A company-page node supersedes a search node: same shape, but it is the
    # authoritative per-company listing rather than a highlighted subset.
    if origin == "company" and row["origin"] == "search":
        conn.execute(
            "UPDATE job_raw SET raw_json=?, origin=?, last_seen=? WHERE source_job_id=?",
            (json.dumps(raw, sort_keys=True), origin, ts, job_id),
        )
    else:
        conn.execute("UPDATE job_raw SET last_seen=? WHERE source_job_id=?", (ts, job_id))
    return False


def add_provenance(
    conn: sqlite3.Connection,
    job_id: str,
    query_key: str,
    role_slug: str | None,
    location_slug: str | None,
    page: int | None,
    origin: str,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO job_provenance (source_job_id,query_key,role_slug,"
        "location_slug,page,origin,seen_at) VALUES (?,?,?,?,?,?,?)",
        (job_id, query_key, role_slug, location_slug, page, origin, now()),
    )


def upsert_company(conn: sqlite3.Connection, slug: str, raw: dict) -> bool:
    ts = now()
    cur = conn.execute("SELECT 1 FROM company_raw WHERE company_slug=?", (slug,))
    if cur.fetchone() is None:
        conn.execute(
            "INSERT INTO company_raw (company_slug,raw_json,first_seen,last_seen) "
            "VALUES (?,?,?,?)",
            (slug, json.dumps(raw, sort_keys=True), ts, ts),
        )
        return True
    conn.execute(
        "UPDATE company_raw SET raw_json=?, last_seen=? WHERE company_slug=?",
        (json.dumps(raw, sort_keys=True), ts, slug),
    )
    return False


def mark_unit(
    conn: sqlite3.Connection, stage: str, unit_key: str, status: str,
    detail: str | None = None, n_items: int | None = None,
) -> None:
    conn.execute(
        "INSERT INTO crawl_unit (stage,unit_key,status,detail,n_items,updated_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(stage,unit_key) DO UPDATE SET "
        "status=excluded.status, detail=excluded.detail, n_items=excluded.n_items, "
        "updated_at=excluded.updated_at",
        (stage, unit_key, status, detail, n_items, now()),
    )


def unit_done(conn: sqlite3.Connection, stage: str, unit_key: str) -> bool:
    cur = conn.execute(
        "SELECT status FROM crawl_unit WHERE stage=? AND unit_key=?", (stage, unit_key)
    )
    row = cur.fetchone()
    return row is not None and row["status"] in ("done", "exhausted")


def mitigation_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM request_log WHERE cf_mitigated IS NOT NULL "
        "OR status IN (403,429,503) ORDER BY id"
    ).fetchall()


def counts(conn: sqlite3.Connection) -> dict[str, Any]:
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "jobs": q("SELECT COUNT(*) FROM job_raw"),
        "companies": q("SELECT COUNT(*) FROM company_raw"),
        "details": q("SELECT COUNT(*) FROM detail_raw"),
        "provenance_rows": q("SELECT COUNT(*) FROM job_provenance"),
        "requests": q("SELECT COUNT(*) FROM request_log"),
        "network_requests": q("SELECT COUNT(*) FROM request_log WHERE from_cache=0"),
        "mitigations": q(
            "SELECT COUNT(*) FROM request_log WHERE cf_mitigated IS NOT NULL "
            "OR status IN (403,429,503)"
        ),
    }

"""SQLite schema + derived views.

Raw first: the Apollo node is persisted verbatim and every filterable column is
expressed as a VIEW over json_extract. Adding a filter dimension later is a view
change, not a re-crawl.

user_state is keyed separately from job_raw on purpose -- shortlist/applied/
hidden must survive re-crawls that rewrite job rows.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS job_raw (
  source_job_id TEXT PRIMARY KEY,
  company_slug  TEXT NOT NULL,
  raw_json      TEXT NOT NULL,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_raw (
  company_slug TEXT PRIMARY KEY,
  raw_json     TEXT NOT NULL,
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_provenance (
  source_job_id TEXT NOT NULL,
  role_slug     TEXT NOT NULL,
  location      TEXT NOT NULL,
  page          INTEGER,
  PRIMARY KEY (source_job_id, role_slug, location)
);

CREATE TABLE IF NOT EXISTS job_detail (
  source_job_id    TEXT PRIMARY KEY,
  json_ld          TEXT,
  equity_raw       TEXT,
  salary_raw       TEXT,
  description_full TEXT,
  fetched_at       TEXT
);

CREATE TABLE IF NOT EXISTS user_state (
  source_job_id TEXT PRIMARY KEY,
  status        TEXT,
  note          TEXT,
  updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS slice_stats (
  role_slug         TEXT, location TEXT,
  total_claimed     INTEGER, companies_claimed INTEGER,
  pages_walked      INTEGER, jobs_recovered INTEGER,
  ended_reason      TEXT, run_at TEXT,
  PRIMARY KEY (role_slug, location, run_at)
);

CREATE TABLE IF NOT EXISTS role_slug (
  role_slug       TEXT PRIMARY KEY,
  valid           INTEGER NOT NULL,
  total_job_count INTEGER,
  location_probed TEXT,
  reason          TEXT,
  checked_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_unit (
  slice_key  TEXT NOT NULL,
  page       INTEGER NOT NULL,
  status     TEXT NOT NULL,
  n_jobs     INTEGER,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (slice_key, page)
);

CREATE TABLE IF NOT EXISTS request_log (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  url          TEXT NOT NULL,
  status       INTEGER,
  cf_ray       TEXT,
  cf_mitigated TEXT,
  bytes        INTEGER,
  elapsed_ms   INTEGER,
  from_cache   INTEGER,
  fetched_at   TEXT NOT NULL
);

-- Derived from job_raw, never crawled. locationNames is a JSON array, and the
-- UI needs to filter and facet on the individual places inside it (Pune, San
-- Francisco, ...), not on the crawl slice token. Materialised rather than a
-- view because faceting hits it on every keystroke; rebuild_locations() is a
-- single pass over job_raw and needs no network.
CREATE TABLE IF NOT EXISTS job_location (
  source_job_id TEXT NOT NULL,
  location      TEXT NOT NULL,
  kind          TEXT NOT NULL,   -- 'onsite' | 'remote'
  PRIMARY KEY (source_job_id, location, kind)
);

CREATE INDEX IF NOT EXISTS ix_jobloc_loc ON job_location(location);
CREATE INDEX IF NOT EXISTS ix_jobloc_job ON job_location(source_job_id);
CREATE INDEX IF NOT EXISTS ix_job_company ON job_raw(company_slug);
CREATE INDEX IF NOT EXISTS ix_prov_job    ON job_provenance(source_job_id);
CREATE INDEX IF NOT EXISTS ix_prov_role   ON job_provenance(role_slug);
CREATE INDEX IF NOT EXISTS ix_prov_loc    ON job_provenance(location);
CREATE INDEX IF NOT EXISTS ix_state       ON user_state(status);
"""

# Derived columns call into src/derive.py (registered on the connection), so
# improving a parser re-derives every row for free. The raw string is ALWAYS
# kept alongside -- an unparsed value beats a silently wrong one.
VIEWS = """
DROP VIEW IF EXISTS jobs;
CREATE VIEW jobs AS
WITH prov AS (
  SELECT source_job_id,
         group_concat(DISTINCT role_slug) AS found_via_roles,
         group_concat(DISTINCT location)  AS found_via_locations,
         COUNT(*)                          AS n_slices
  FROM job_provenance GROUP BY source_job_id
),
sal AS (
  -- Wellfound sends '' rather than null for "no salary listed". Normalising to
  -- NULL here is what makes "has salary" filters and fill rates honest.
  SELECT j.source_job_id,
         NULLIF(COALESCE(NULLIF(d.salary_raw,''),
                         NULLIF(json_extract(j.raw_json,'$.compensation'),'')),'') AS s
  FROM job_raw j LEFT JOIN job_detail d ON d.source_job_id = j.source_job_id
)
SELECT
  j.source_job_id,
  json_extract(j.raw_json,'$.title')                       AS title,
  json_extract(c.raw_json,'$.name')                        AS company,
  j.company_slug                                           AS company_slug,
  size_label(json_extract(c.raw_json,'$.companySize'))     AS company_size,
  size_min(json_extract(c.raw_json,'$.companySize'))       AS company_size_min,
  NULLIF(json_extract(c.raw_json,'$.highConcept'),'')      AS high_concept,
  (SELECT group_concat(value,', ') FROM json_each(c.raw_json,'$._badges')) AS badges,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$.locationNames')) AS location_raw,
  (SELECT group_concat(value,', ') FROM json_each(j.raw_json,'$.acceptedRemoteLocationNames')) AS remote_locations,
  json_extract(j.raw_json,'$.remote')                      AS remote,
  json_extract(j.raw_json,'$.remoteConfig.kind')           AS remote_config,
  remote_label(json_extract(j.raw_json,'$.remoteConfig.kind'),
               json_extract(j.raw_json,'$.remote'))        AS remote_label,
  NULLIF(json_extract(j.raw_json,'$.jobType'),'')          AS job_type,
  sal.s                                                    AS salary_raw,
  -- numeric bounds via src/derive.py; NULL when the shape is unrecognised,
  -- never a guess. Improving the parser re-derives every row for free.
  salary_min(sal.s)                                        AS salary_min,
  salary_max(sal.s)                                        AS salary_max,
  NULLIF(d.equity_raw,'')                                  AS equity_raw,
  equity_max(d.equity_raw)                                 AS equity_max,
  json_extract(j.raw_json,'$.liveStartAt')                 AS posted_ts,
  datetime(json_extract(j.raw_json,'$.liveStartAt'),'unixepoch') AS posted_at,
  CAST((julianday('now') - julianday(datetime(json_extract(j.raw_json,'$.liveStartAt'),'unixepoch'))) AS INTEGER) AS days_old,
  COALESCE(d.description_full, json_extract(j.raw_json,'$.description')) AS description,
  json_extract(j.raw_json,'$.atsSource')                   AS ats_source,
  json_extract(j.raw_json,'$.primaryRoleTitle')            AS primary_role_title,
  json_extract(j.raw_json,'$.autoPosted')                  AS auto_posted,
  p.found_via_roles                                        AS found_via_roles,
  p.found_via_locations                                    AS found_via_locations,
  p.n_slices                                               AS n_slices,
  'https://wellfound.com/jobs/' || j.source_job_id || '-' ||
      COALESCE(json_extract(j.raw_json,'$.slug'),'')       AS apply_url,
  COALESCE(u.status,'new')                                 AS status,
  u.note                                                   AS note,
  (d.source_job_id IS NOT NULL)                            AS enriched,
  j.first_seen, j.last_seen
FROM job_raw j
LEFT JOIN company_raw c ON c.company_slug = j.company_slug
LEFT JOIN job_detail  d ON d.source_job_id = j.source_job_id
LEFT JOIN user_state  u ON u.source_job_id = j.source_job_id
LEFT JOIN prov        p ON p.source_job_id = j.source_job_id
LEFT JOIN sal           ON sal.source_job_id = j.source_job_id;
"""


def connect(path: Path | str) -> sqlite3.Connection:
    from . import derive

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Must be registered BEFORE the view is created -- the view calls them.
    derive.register(conn)
    conn.executescript(SCHEMA)
    conn.executescript(VIEWS)
    conn.commit()
    return conn


# --- writers ------------------------------------------------------------------


def log_request(conn: sqlite3.Connection, resp: Any) -> None:
    conn.execute(
        "INSERT INTO request_log (url,status,cf_ray,cf_mitigated,bytes,elapsed_ms,"
        "from_cache,fetched_at) VALUES (?,?,?,?,?,?,?,?)",
        (resp.url, resp.status, resp.headers.get("cf-ray"),
         resp.headers.get("cf-mitigated"), len(resp.text or ""), resp.elapsed_ms,
         int(bool(resp.from_cache)), resp.fetched_at),
    )


def upsert_job(conn: sqlite3.Connection, job_id: str, company_slug: str, raw: dict) -> bool:
    ts = now()
    row = conn.execute(
        "SELECT 1 FROM job_raw WHERE source_job_id=?", (job_id,)
    ).fetchone()
    payload = json.dumps(raw, sort_keys=True)
    if row is None:
        conn.execute(
            "INSERT INTO job_raw (source_job_id,company_slug,raw_json,first_seen,last_seen)"
            " VALUES (?,?,?,?,?)", (job_id, company_slug, payload, ts, ts))
        return True
    conn.execute(
        "UPDATE job_raw SET raw_json=?, company_slug=?, last_seen=? WHERE source_job_id=?",
        (payload, company_slug, ts, job_id))
    return False


def upsert_company(conn: sqlite3.Connection, slug: str, raw: dict) -> bool:
    ts = now()
    row = conn.execute(
        "SELECT 1 FROM company_raw WHERE company_slug=?", (slug,)).fetchone()
    payload = json.dumps(raw, sort_keys=True)
    if row is None:
        conn.execute(
            "INSERT INTO company_raw (company_slug,raw_json,first_seen,last_seen)"
            " VALUES (?,?,?,?)", (slug, payload, ts, ts))
        return True
    conn.execute("UPDATE company_raw SET raw_json=?, last_seen=? WHERE company_slug=?",
                 (payload, ts, slug))
    return False


def add_provenance(conn, job_id: str, role: str, location: str, page: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO job_provenance (source_job_id,role_slug,location,page)"
        " VALUES (?,?,?,?)", (job_id, role, location, page))


def record_slice(conn, role: str, location: str, total_claimed: int | None,
                 companies_claimed: int | None, pages: int, recovered: int,
                 reason: str, run_at: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO slice_stats (role_slug,location,total_claimed,"
        "companies_claimed,pages_walked,jobs_recovered,ended_reason,run_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (role, location, total_claimed, companies_claimed, pages, recovered, reason, run_at))


def mark_page(conn, slice_key: str, page: int, status: str, n_jobs: int) -> None:
    conn.execute(
        "INSERT INTO crawl_unit (slice_key,page,status,n_jobs,updated_at) VALUES (?,?,?,?,?)"
        " ON CONFLICT(slice_key,page) DO UPDATE SET status=excluded.status,"
        " n_jobs=excluded.n_jobs, updated_at=excluded.updated_at",
        (slice_key, page, status, n_jobs, now()))


def page_done(conn, slice_key: str, page: int) -> bool:
    r = conn.execute("SELECT status FROM crawl_unit WHERE slice_key=? AND page=?",
                     (slice_key, page)).fetchone()
    return r is not None and r["status"] == "done"


def set_status(conn, job_id: str, status: str, note: str | None = None) -> None:
    conn.execute(
        "INSERT INTO user_state (source_job_id,status,note,updated_at) VALUES (?,?,?,?)"
        " ON CONFLICT(source_job_id) DO UPDATE SET status=excluded.status,"
        " note=COALESCE(excluded.note,user_state.note), updated_at=excluded.updated_at",
        (job_id, status, note, now()))


def rebuild_locations(conn) -> int:
    """Re-derive job_location from job_raw. Idempotent, offline, one pass.

    This is the ADR-002 promise in practice: a new filter dimension is a
    re-derivation, not a re-crawl. Safe to run any time.
    """
    conn.execute("DELETE FROM job_location")
    conn.execute(
        """
        INSERT OR IGNORE INTO job_location (source_job_id, location, kind)
        SELECT j.source_job_id, TRIM(je.value), 'onsite'
        FROM job_raw j, json_each(j.raw_json, '$.locationNames') je
        WHERE TRIM(je.value) <> ''
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO job_location (source_job_id, location, kind)
        SELECT j.source_job_id, TRIM(je.value), 'remote'
        FROM job_raw j, json_each(j.raw_json, '$.acceptedRemoteLocationNames') je
        WHERE TRIM(je.value) <> ''
        """
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM job_location").fetchone()[0]


def counts(conn) -> dict[str, Any]:
    q = lambda s: conn.execute(s).fetchone()[0]
    return {
        "jobs": q("SELECT COUNT(*) FROM job_raw"),
        "companies": q("SELECT COUNT(*) FROM company_raw"),
        "enriched": q("SELECT COUNT(*) FROM job_detail"),
        "provenance": q("SELECT COUNT(*) FROM job_provenance"),
        "requests": q("SELECT COUNT(*) FROM request_log"),
        "network": q("SELECT COUNT(*) FROM request_log WHERE from_cache=0"),
        "mitigations": q("SELECT COUNT(*) FROM request_log WHERE cf_mitigated IS NOT NULL"
                         " OR status IN (403,429,503)"),
    }

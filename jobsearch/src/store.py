"""SQLite schema, migrations, and the layered views.

Raw first: each source's payload is persisted verbatim in `job_raw.raw_json`,
and every filterable column is derived on top. Adding a source is a new module
in `src/sources/` plus a view — storage does not change shape (ADR-002).

Three layers:

    job_raw / company_raw     raw payloads, one row per job, namespaced by source
    jobs_core                 UNION ALL of each source's core view (ADR-007)
    jobs                      jobs_core + provenance, detail, user_state, derived

IDs are namespaced as `"<source>:<native_id>"`. RemoteOK id 1137286 and a
Wellfound id would otherwise collide and silently overwrite each other. Keeping
a single-column primary key (rather than a composite) means every dependent
table — provenance, detail, location, user_state — keeps its existing shape.

user_state is keyed separately from job_raw on purpose: shortlist/applied/hidden
must survive re-crawls that rewrite job rows.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_uid(source: str, native_id: str) -> str:
    return f"{source}:{native_id}"


def slugify_company(name: str) -> str:
    """For sources with no company entity of their own (RemoteOK gives a name
    only). Keeps the company facet working; the raw name stays in raw_json."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = re.sub(r"[^\w\s-]", " ", n.lower())
    return "-".join(t for t in re.split(r"[\s_-]+", n) if t)[:120]


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS job_raw (
  source_job_id TEXT PRIMARY KEY,     -- "<source>:<native_id>"
  source        TEXT NOT NULL DEFAULT 'wellfound',
  native_id     TEXT,
  company_slug  TEXT NOT NULL,
  raw_json      TEXT NOT NULL,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_raw (
  company_slug TEXT NOT NULL,
  source       TEXT NOT NULL DEFAULT 'wellfound',
  raw_json     TEXT NOT NULL,
  first_seen   TEXT NOT NULL,
  last_seen    TEXT NOT NULL,
  PRIMARY KEY (source, company_slug)
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

CREATE TABLE IF NOT EXISTS job_location (
  source_job_id TEXT NOT NULL,
  location      TEXT NOT NULL,
  kind          TEXT NOT NULL,   -- 'onsite' | 'remote'
  PRIMARY KEY (source_job_id, location, kind)
);

CREATE TABLE IF NOT EXISTS slice_stats (
  role_slug         TEXT, location TEXT,
  source            TEXT DEFAULT 'wellfound',
  total_claimed     INTEGER, companies_claimed INTEGER,
  pages_walked      INTEGER, jobs_recovered INTEGER,
  stale_skipped     INTEGER DEFAULT 0,
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

-- Resolution of a company to its ATS board. ATS APIs are company-scoped with
-- no cross-board search, so the board token has to be discovered per company
-- and is worth caching: a miss costs several requests to re-establish.
CREATE TABLE IF NOT EXISTS company_ats (
  company_slug TEXT NOT NULL,
  provider     TEXT NOT NULL,
  board_token  TEXT,
  resolved     INTEGER NOT NULL DEFAULT 0,
  n_jobs       INTEGER,
  attempts     TEXT,
  checked_at   TEXT NOT NULL,
  PRIMARY KEY (company_slug, provider)
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

"""

# Indexes run AFTER migrate(): on a pre-existing database an index over a column
# that migrate() is about to add would fail with "no such column".
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_job_company ON job_raw(company_slug);
CREATE INDEX IF NOT EXISTS ix_job_source  ON job_raw(source);
CREATE INDEX IF NOT EXISTS ix_prov_job    ON job_provenance(source_job_id);
CREATE INDEX IF NOT EXISTS ix_prov_role   ON job_provenance(role_slug);
CREATE INDEX IF NOT EXISTS ix_prov_loc    ON job_provenance(location);
CREATE INDEX IF NOT EXISTS ix_state       ON user_state(status);
CREATE INDEX IF NOT EXISTS ix_jobloc_loc  ON job_location(location);
CREATE INDEX IF NOT EXISTS ix_jobloc_job  ON job_location(source_job_id);
"""

# Additive column adds. CREATE TABLE IF NOT EXISTS does not alter an existing
# table, so a database created before a column existed keeps working right up
# until a query names it.
ADD_COLUMNS: list[tuple[str, str, str]] = [
    ("slice_stats", "stale_skipped", "INTEGER DEFAULT 0"),
    ("slice_stats", "source", "TEXT DEFAULT 'wellfound'"),
    ("job_raw", "source", "TEXT NOT NULL DEFAULT 'wellfound'"),
    ("job_raw", "native_id", "TEXT"),
    ("company_raw", "source", "TEXT NOT NULL DEFAULT 'wellfound'"),
]

# The outer view. Everything source-specific lives in jobs_core; this layer adds
# provenance, on-demand detail, user state, and the derived scalars.
OUTER_VIEW = """
DROP VIEW IF EXISTS jobs;
CREATE VIEW jobs AS
-- The outer layer exists only to add the cross-currency sort keys, which have
-- to be computed from `salary_min`/`salary_currency` after those are themselves
-- derived. Approximate and for ORDERING ONLY -- the native figure and the raw
-- string travel alongside and are what the UI displays.
SELECT *,
       salary_usd(salary_min, salary_currency) AS salary_usd_min,
       salary_usd(salary_max, salary_currency) AS salary_usd_max
FROM (
WITH prov AS (
  SELECT source_job_id,
         group_concat(DISTINCT role_slug) AS found_via_roles,
         group_concat(DISTINCT location)  AS found_via_locations,
         COUNT(*)                          AS n_slices
  FROM job_provenance GROUP BY source_job_id
)
SELECT
  k.source, k.source_job_id, k.native_id, k.company_slug,
  k.title, k.company, k.company_size, k.company_size_min, k.high_concept, k.badges,
  k.location_raw, k.remote_locations, k.remote, k.remote_config, k.remote_label,
  k.job_type,
  -- Detail-page salary (Wellfound enrichment) wins over the search-page value.
  NULLIF(COALESCE(NULLIF(d.salary_raw,''), NULLIF(k.salary_raw,'')),'') AS salary_raw,
  -- Sources that hand us numbers directly are trusted as-is; otherwise parse
  -- the string. NULL beats a guess either way.
  CASE WHEN k.salary_period IS NOT NULL AND k.salary_period <> 'annual' THEN NULL
       ELSE COALESCE(k.salary_min_native,
            salary_min(COALESCE(NULLIF(d.salary_raw,''), k.salary_raw))) END AS salary_min,
  CASE WHEN k.salary_period IS NOT NULL AND k.salary_period <> 'annual' THEN NULL
       ELSE COALESCE(k.salary_max_native,
            salary_max(COALESCE(NULLIF(d.salary_raw,''), k.salary_raw))) END AS salary_max,
  -- A source that STATES its currency wins; Wellfound states none, so it is
  -- recovered from the symbol in the raw string. Leaving it NULL is what put
  -- 3,000,000 INR and 200,000 USD in one sortable column with nothing to tell
  -- them apart.
  COALESCE(k.salary_currency,
           salary_currency(COALESCE(NULLIF(d.salary_raw,''), k.salary_raw))
  )                                                        AS salary_currency,
  k.salary_period,
  NULLIF(d.equity_raw,'')                                  AS equity_raw,
  equity_max(d.equity_raw)                                 AS equity_max,
  k.posted_ts,
  datetime(k.posted_ts,'unixepoch')                        AS posted_at,
  CAST((julianday('now') - julianday(datetime(k.posted_ts,'unixepoch'))) AS INTEGER) AS days_old,
  k.expires_ts,
  datetime(k.expires_ts,'unixepoch')                       AS expires_at,
  -- Only meaningful where the source publishes an expiry (Himalayas does,
  -- Wellfound does not). NULL means unknown, never "not expired".
  -- CAST is load-bearing: strftime returns TEXT, and in SQLite's type ordering
  -- every INTEGER sorts before every TEXT. Without it this comparison is
  -- unconditionally true and every job reads as expired.
  CASE WHEN k.expires_ts IS NULL THEN NULL
       WHEN CAST(k.expires_ts AS INTEGER) < CAST(strftime('%s','now') AS INTEGER)
       THEN 1 ELSE 0 END                                     AS expired,
  COALESCE(d.description_full, k.description)              AS description,
  k.ats_source, k.primary_role_title, k.auto_posted,
  p.found_via_roles, p.found_via_locations, p.n_slices,
  k.apply_url,
  COALESCE(u.status,'new')                                 AS status,
  u.note                                                   AS note,
  (d.source_job_id IS NOT NULL)                            AS enriched,
  k.first_seen, k.last_seen
FROM jobs_core k
LEFT JOIN job_detail  d ON d.source_job_id = k.source_job_id
LEFT JOIN user_state  u ON u.source_job_id = k.source_job_id
LEFT JOIN prov        p ON p.source_job_id = k.source_job_id
);
"""


def _user_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Additive column adds, then versioned data migrations."""
    applied: list[str] = []

    for table, column, decl in ADD_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols:
            continue
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            applied.append(f"+{table}.{column}")
    conn.commit()

    if _user_version(conn) < 2:
        # v1 -> v2: namespace every id with its source. Pre-v2 rows are all
        # Wellfound by definition -- it was the only source. Guarded on the
        # prefix so a partial run is safe to repeat.
        n = conn.execute(
            "SELECT COUNT(*) FROM job_raw WHERE source_job_id NOT LIKE '%:%'"
        ).fetchone()[0]
        if n:
            for t in ("job_raw", "job_provenance", "job_detail", "user_state",
                      "job_location"):
                conn.execute(
                    f"UPDATE {t} SET source_job_id = 'wellfound:' || source_job_id "
                    f"WHERE source_job_id NOT LIKE '%:%'")
            applied.append(f"v2:namespaced {n} ids")
        conn.execute(
            "UPDATE job_raw SET native_id = substr(source_job_id, 11) "
            "WHERE native_id IS NULL AND source_job_id LIKE 'wellfound:%'")

        # company_raw's primary key must become (source, company_slug): two
        # sources can legitimately use the same slug. SQLite cannot ALTER a
        # primary key, so rebuild the table. It is small and fully regenerable.
        pk = [r["name"] for r in conn.execute("PRAGMA table_info(company_raw)")
              if r["pk"]]
        if pk != ["source", "company_slug"]:
            conn.executescript("""
                CREATE TABLE company_raw_v2 (
                  company_slug TEXT NOT NULL,
                  source       TEXT NOT NULL DEFAULT 'wellfound',
                  raw_json     TEXT NOT NULL,
                  first_seen   TEXT NOT NULL,
                  last_seen    TEXT NOT NULL,
                  PRIMARY KEY (source, company_slug)
                );
                INSERT OR IGNORE INTO company_raw_v2
                  (company_slug, source, raw_json, first_seen, last_seen)
                  SELECT company_slug, COALESCE(source,'wellfound'), raw_json,
                         first_seen, last_seen FROM company_raw;
                DROP TABLE company_raw;
                ALTER TABLE company_raw_v2 RENAME TO company_raw;
            """)
            applied.append("v2:company_raw composite pk")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    return applied


def connect(path: Path | str) -> sqlite3.Connection:
    from . import derive, sources

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # Registered BEFORE the views are created -- the views call them.
    derive.register(conn)
    conn.executescript(SCHEMA)
    # Views must go BEFORE migrate(): a migration that rebuilds a table cannot
    # DROP it while a view still references it ("error in view jobs: no such
    # table"). They are recreated from the source registry below.
    conn.execute("DROP VIEW IF EXISTS jobs")
    conn.execute("DROP VIEW IF EXISTS jobs_core")
    migrate(conn)
    conn.executescript(INDEXES)
    conn.execute(sources.core_view_sql())
    sources.assert_core_views(conn)
    conn.executescript(OUTER_VIEW)
    conn.commit()
    return conn


# --- writers ------------------------------------------------------------------


def log_request(conn: sqlite3.Connection, resp: Any) -> None:
    conn.execute(
        "INSERT INTO request_log (url,status,cf_ray,cf_mitigated,bytes,elapsed_ms,"
        "from_cache,fetched_at) VALUES (?,?,?,?,?,?,?,?)",
        (resp.url, resp.status, resp.headers.get("cf-ray"),
         resp.headers.get("cf-mitigated"), len(resp.text or ""), resp.elapsed_ms,
         int(bool(resp.from_cache)), resp.fetched_at))


def upsert_job(conn, native: str, company_slug: str, raw: dict,
               source: str = "wellfound") -> bool:
    """Returns True if the row is new. `native` is the source's own id; the
    stored key is namespaced."""
    uid = job_uid(source, native)
    ts = now()
    payload = json.dumps(raw, sort_keys=True)
    row = conn.execute(
        "SELECT 1 FROM job_raw WHERE source_job_id=?", (uid,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO job_raw (source_job_id,source,native_id,company_slug,"
            "raw_json,first_seen,last_seen) VALUES (?,?,?,?,?,?,?)",
            (uid, source, native, company_slug, payload, ts, ts))
        return True
    conn.execute(
        "UPDATE job_raw SET raw_json=?, company_slug=?, source=?, native_id=?, "
        "last_seen=? WHERE source_job_id=?",
        (payload, company_slug, source, native, ts, uid))
    return False


def upsert_company(conn, slug: str, raw: dict, source: str = "wellfound") -> bool:
    ts = now()
    payload = json.dumps(raw, sort_keys=True)
    row = conn.execute(
        "SELECT 1 FROM company_raw WHERE company_slug=? AND source=?",
        (slug, source)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO company_raw (company_slug,source,raw_json,first_seen,last_seen)"
            " VALUES (?,?,?,?,?)", (slug, source, payload, ts, ts))
        return True
    conn.execute(
        "UPDATE company_raw SET raw_json=?, last_seen=? WHERE company_slug=? AND source=?",
        (payload, ts, slug, source))
    return False


def add_provenance(conn, uid: str, role: str, location: str, page: int) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO job_provenance (source_job_id,role_slug,location,page)"
        " VALUES (?,?,?,?)", (uid, role, location, page))


def record_slice(conn, role: str, location: str, total_claimed: int | None,
                 companies_claimed: int | None, pages: int, recovered: int,
                 reason: str, run_at: str, stale_skipped: int = 0,
                 source: str = "wellfound") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO slice_stats (role_slug,location,source,total_claimed,"
        "companies_claimed,pages_walked,jobs_recovered,stale_skipped,ended_reason,run_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (role, location, source, total_claimed, companies_claimed, pages,
         recovered, stale_skipped, reason, run_at))


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
    """Re-derive job_location. Idempotent, offline, one pass.

    Reads `jobs_core`, not `job_raw`, so every source contributes through its own
    field mapping rather than this function knowing each payload shape.
    """
    conn.execute("DELETE FROM job_location")
    for kind, col in (("onsite", "location_raw"), ("remote", "remote_locations")):
        rows = conn.execute(
            f"SELECT source_job_id, {col} v FROM jobs_core "
            f"WHERE {col} IS NOT NULL AND {col} <> ''").fetchall()
        conn.executemany(
            "INSERT OR IGNORE INTO job_location (source_job_id,location,kind)"
            " VALUES (?,?,?)",
            [(r["source_job_id"], part.strip(), kind)
             for r in rows for part in str(r["v"]).split(",") if part.strip()])
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM job_location").fetchone()[0]


def prune_stale(conn, max_age_days: int, dry_run: bool = True) -> dict[str, Any]:
    """Remove jobs older than the cutoff. Dry-run by default; never automatic.

    Recoverable without network: the raw pages stay in cache/, so a wider cutoff
    can be re-ingested with `crawl --no-resume` and no requests.

    user_state is deliberately NOT deleted -- a job you marked applied should
    keep that mark if it returns on a later crawl.
    """
    ids = [r[0] for r in conn.execute(
        "SELECT source_job_id FROM jobs WHERE days_old > ?", (max_age_days,))]
    out = {"max_age_days": max_age_days, "would_remove": len(ids),
           "total": conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0],
           "dry_run": dry_run, "removed": 0}
    if dry_run or not ids:
        return out
    for t in ("job_raw", "job_provenance", "job_detail", "job_location"):
        conn.executemany(f"DELETE FROM {t} WHERE source_job_id=?", [(i,) for i in ids])
    conn.execute("DELETE FROM company_raw WHERE (source, company_slug) NOT IN "
                 "(SELECT DISTINCT source, company_slug FROM job_raw)")
    conn.commit()
    out["removed"] = len(ids)
    return out


# Sources whose `badges` are USER-SUPPLIED TAGS rather than a real taxonomy.
# RemoteOK's `?tag=engineer` returns Kitchen Technician, Joiner and JANITOR --
# all genuinely carrying that tag -- so matching a RemoteOK row against its own
# badges says a janitor is a software engineer. `remoteok.py` passes None for
# categories live, and anything reasoning about relevance must do the same.
UNTRUSTED_BADGE_SOURCES = {"remoteok"}

# The ONLY sources whose rows can have arrived without a role filter (ADR-009).
#
# Wellfound filters server-side at crawl time, so its rows are legitimate even
# under a slug that has since been retired from targets.yaml -- `ai-engineer`
# and `data-engineer` name real Wellfound role searches that WERE applied. ATS
# boards apply the current keywords locally at expand time. Re-judging either
# against our keyword list would delete correctly-filtered jobs using a cruder
# instrument than the one that filtered them.
LOCALLY_FILTERED_SOURCES = {"remoteok", "himalayas"}


def reconcile_roles(conn, role_keywords: dict[str, list[str]],
                    dry_run: bool = True) -> dict[str, Any]:
    """Apply the role filter retroactively to rows that predate it.

    Jobs ingested before ADR-009 carry provenance `all`, `dev` or `engineer` and
    were never role-filtered. Rows whose provenance is ENTIRELY outside the
    configured vocabulary are re-judged with the same rule their source uses
    live: matched ones gain the real role slug, unmatched ones are removed.

    Rows carrying a `user_state` are never deleted. That is the only
    non-regenerable data in the database; losing the record of having applied to
    a job is not recoverable by re-crawling.

    Dry-run by default, like `prune_stale`. Deleted rows are re-ingestable from
    `cache/` without network.
    """
    from .relevance import matches

    out: dict[str, Any] = {"scanned": 0, "would_retag": 0, "would_remove": 0,
                           "retagged": 0, "removed": 0, "protected": 0,
                           "dry_run": dry_run}
    # An empty keyword map means "no roles configured", which must never be read
    # as "nothing matches, delete everything".
    if not role_keywords:
        return out

    known = list(role_keywords)
    srcs = sorted(LOCALLY_FILTERED_SOURCES)
    rows = conn.execute(f"""
        SELECT j.source_job_id, j.title, j.badges, j.source
        FROM jobs j
        WHERE j.source IN ({",".join("?" * len(srcs))})
          AND NOT EXISTS (
            SELECT 1 FROM job_provenance p
            WHERE p.source_job_id = j.source_job_id
              AND p.role_slug IN ({",".join("?" * len(known))}))
    """, srcs + known).fetchall()
    out["scanned"] = len(rows)

    retag: list[tuple[str, str]] = []
    remove: list[str] = []
    acted_on = {r[0] for r in conn.execute(
        "SELECT source_job_id FROM user_state WHERE status IS NOT NULL "
        "AND status <> 'new'")}

    for r in rows:
        cats = None
        if r["source"] not in UNTRUSTED_BADGE_SOURCES:
            cats = [c.strip() for c in (r["badges"] or "").split(",") if c.strip()]
        hit = next((role for role in known
                    if matches(r["title"], cats, role_keywords[role])), None)
        if hit:
            retag.append((r["source_job_id"], hit))
        elif r["source_job_id"] in acted_on:
            out["protected"] += 1
        else:
            remove.append(r["source_job_id"])

    out["would_retag"], out["would_remove"] = len(retag), len(remove)
    if dry_run:
        return out

    for uid, role in retag:
        add_provenance(conn, uid, role, "backfill", 1)
    for t in ("job_raw", "job_provenance", "job_detail", "job_location"):
        conn.executemany(f"DELETE FROM {t} WHERE source_job_id=?",
                         [(i,) for i in remove])
    conn.execute("DELETE FROM company_raw WHERE (source, company_slug) NOT IN "
                 "(SELECT DISTINCT source, company_slug FROM job_raw)")
    conn.commit()
    out["retagged"], out["removed"] = len(retag), len(remove)
    return out


def counts(conn) -> dict[str, Any]:
    q = lambda s: conn.execute(s).fetchone()[0]
    out: dict[str, Any] = {
        "jobs": q("SELECT COUNT(*) FROM job_raw"),
        "companies": q("SELECT COUNT(*) FROM company_raw"),
        "enriched": q("SELECT COUNT(*) FROM job_detail"),
        "provenance": q("SELECT COUNT(*) FROM job_provenance"),
        "requests": q("SELECT COUNT(*) FROM request_log"),
        "network": q("SELECT COUNT(*) FROM request_log WHERE from_cache=0"),
        "mitigations": q("SELECT COUNT(*) FROM request_log WHERE cf_mitigated IS NOT NULL"
                         " OR status IN (403,429,503)"),
    }
    out["by_source"] = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) n FROM job_raw GROUP BY source ORDER BY n DESC")}
    return out

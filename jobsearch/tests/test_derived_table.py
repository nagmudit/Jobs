"""`job_derived`: the stored derivation behind the `jobs` view (ADR-015).

The table exists for speed only. Every test here pins one way it could stop
being a faithful copy of what the raw data derives to -- which, unlike a slow
query, returns HTTP 200 with plausible rows and is never noticed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import store as S
from src.config import Config
from src.parse import parse_search_page
from src.web.app import create_app

from . import fixtures as F

# The `jobs` view exactly as it was before ADR-015: everything derived per
# query, straight from jobs_core. Kept verbatim as the reference the stored
# table must reproduce. Do not "fix" it to match the new SQL -- its whole value
# is that it is the old derivation, written independently of the new one.
REFERENCE_VIEW = """CREATE TEMP VIEW jobs_reference AS
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
  NULLIF(COALESCE(NULLIF(d.salary_raw,''), NULLIF(k.salary_raw,'')),'') AS salary_raw,
  CASE WHEN k.salary_period IS NOT NULL AND k.salary_period <> 'annual' THEN NULL
       ELSE COALESCE(k.salary_min_native,
            salary_min(COALESCE(NULLIF(d.salary_raw,''), k.salary_raw))) END AS salary_min,
  CASE WHEN k.salary_period IS NOT NULL AND k.salary_period <> 'annual' THEN NULL
       ELSE COALESCE(k.salary_max_native,
            salary_max(COALESCE(NULLIF(d.salary_raw,''), k.salary_raw))) END AS salary_max,
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
)"""

# group_concat order was never specified by the old view; the new one sorts.
# Compared as sets so the test pins content, not an accident of scan order.
UNORDERED = ("found_via_roles", "found_via_locations")

HM_JOB = {
    "title": "Platform Eng", "companyName": "Acme", "companySlug": "acme",
    "description": "hm body", "employmentType": "Full Time",
    "minSalary": 45000, "maxSalary": 52000, "currency": "EUR",
    "salaryPeriod": "annual", "locationRestrictions": ["Germany"],
    "categories": ["Eng"], "pubDate": 1788451065, "expiryDate": 1793635064,
    "applicationLink": "https://himalayas.app/companies/acme/jobs/eng",
    "guid": "https://himalayas.app/companies/acme/jobs/eng",
}
RO_JOB = {"id": "77", "slug": "x", "position": "Dev", "company": "Acme",
          "epoch": 1788372677, "description": "ro body", "location": "Remote",
          "salary_min": 50000, "salary_max": 70000, "tags": ["dev"],
          "url": "https://remoteok.com/x"}


@pytest.fixture
def db(tmp_path):
    """Three sources, enrichment, several provenance rows, and a user mark --
    every input the derivation reads."""
    conn = S.connect(tmp_path / "t.db")
    page = parse_search_page(F.page(n_companies=2, jobs_per_company=2), "u")
    for startup, jobs in page.startups:
        S.upsert_company(conn, startup["slug"], startup)
        for jid, raw in jobs:
            S.upsert_job(conn, jid, startup["slug"], raw)
            uid = S.job_uid("wellfound", jid)
            S.add_provenance(conn, uid, "ai-engineer", "bangalore", 1)
            S.add_provenance(conn, uid, "data-engineer", "remote", 2)
    S.upsert_company(conn, "acme", {"name": "Acme"}, source="himalayas")
    S.upsert_job(conn, "hm1", "acme", HM_JOB, source="himalayas")
    S.upsert_job(conn, "77", "acme", RO_JOB, source="remoteok")
    S.add_provenance(conn, "remoteok:77", "ai-engineer", "anywhere", 1)
    first = conn.execute("SELECT source_job_id FROM job_raw "
                         "WHERE source='wellfound' ORDER BY 1").fetchone()[0]
    _enrich(conn, first)
    S.set_status(conn, first, "shortlisted", note="ping them")
    conn.commit()
    return conn


def _enrich(conn, uid: str, salary: str = "₹15L – ₹25L") -> None:
    conn.execute(
        "INSERT INTO job_detail (source_job_id,json_ld,equity_raw,salary_raw,"
        "description_full,fetched_at) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(source_job_id) DO UPDATE SET salary_raw=excluded.salary_raw",
        (uid, None, "0.1% – 0.5%", salary, "full detail body", S.now()))


def _rows(conn, view: str) -> list[dict]:
    out = []
    for r in conn.execute(f"SELECT * FROM {view} ORDER BY source_job_id"):
        d = dict(r)
        for k in UNORDERED:
            d[k] = sorted((d[k] or "").split(","))
        out.append(d)
    return out


def _cols(conn, view: str) -> list[str]:
    return [c[0] for c in conn.execute(f"SELECT * FROM {view} LIMIT 0").description]


def test_jobs_matches_the_old_per_query_derivation(db):
    """The guard that matters: same rows, same values, as deriving per query."""
    db.execute(REFERENCE_VIEW)
    got, want = _rows(db, "jobs"), _rows(db, "jobs_reference")
    assert len(got) == len(want) == 6
    assert got == want


def test_column_order_is_unchanged(db):
    """Callers index rows by name, but UNION ALL and `SELECT j.*` consumers
    align by position -- a reordered column is a silent wrong answer."""
    db.execute(REFERENCE_VIEW)
    assert _cols(db, "jobs") == _cols(db, "jobs_reference")


def test_the_table_stores_neither_user_state_nor_the_clock(db):
    """User state must not be stored: a click would need a rebuild, and the
    table ships in the published corpus, where marks must never appear.
    Clock-relative values must not be stored: they would be wrong by tomorrow."""
    stored = set(_cols(db, "job_derived"))
    assert not stored & {"status", "note", "days_old", "expired"}


def test_a_status_click_is_visible_without_a_rebuild(db):
    uid = db.execute("SELECT source_job_id FROM jobs WHERE status='new'").fetchone()[0]
    S.set_status(db, uid, "applied")
    db.commit()
    assert db.execute("SELECT status FROM jobs WHERE source_job_id=?",
                      (uid,)).fetchone()[0] == "applied"


def test_every_input_write_reaches_the_table_immediately(db):
    """One case per input the derivation reads. No rebuild is called anywhere
    here -- that is the point: the triggers keep the table exact."""
    db.execute(REFERENCE_VIEW)
    uid = db.execute("SELECT source_job_id, company_slug FROM job_raw "
                     "WHERE source='wellfound' ORDER BY 1 DESC").fetchone()
    jid, slug = uid

    def title():
        return db.execute("SELECT title, company, salary_raw, enriched, "
                          "found_via_roles FROM jobs WHERE source_job_id=?",
                          (jid,)).fetchone()

    # job_raw rewritten by a re-crawl
    S.upsert_job(db, jid.split(":", 1)[1], slug, {"title": "Renamed", "slug": "r"})
    assert title()["title"] == "Renamed"
    # company_raw: feeds every job of that company, not just one
    S.upsert_company(db, slug, {"name": "NewCo", "companySize": "SIZE_11_50"})
    assert title()["company"] == "NewCo"
    # job_detail: enrichment
    _enrich(db, jid, salary="$90k")
    assert title()["salary_raw"] == "$90k" and title()["enriched"] == 1
    # job_provenance: a new role
    S.add_provenance(db, jid, "ml-engineer", "pune", 1)
    assert "ml-engineer" in title()["found_via_roles"]
    # deletes: what prune_stale and reconcile_roles do
    for t in ("job_raw", "job_provenance", "job_detail"):
        db.execute(f"DELETE FROM {t} WHERE source_job_id=?", (jid,))
    assert title() is None
    db.commit()
    assert _rows(db, "jobs") == _rows(db, "jobs_reference")


def test_prune_and_reconcile_leave_no_orphans(db):
    """The real deleters, end to end."""
    db.execute(REFERENCE_VIEW)
    db.execute("UPDATE job_raw SET raw_json = json_set(raw_json,'$.liveStartAt',1)"
               " WHERE source='wellfound'")
    out = S.prune_stale(db, 30, dry_run=False)
    assert out["removed"] >= 1
    assert _rows(db, "jobs") == _rows(db, "jobs_reference")


def test_a_derivation_change_rebuilds_on_connect(db, tmp_path):
    """Triggers see data changes, not code changes. A new parser in derive.py
    must still re-derive every stored row -- the promise ADR-002 made."""
    db.execute("UPDATE job_derived SET title='stale'")
    db.commit()
    db.close()

    # The fingerprint still matches, so nothing is rebuilt: proof the check is
    # what drives the rebuild, and that a plain reconnect costs nothing.
    conn = S.connect(tmp_path / "t.db")
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE title='stale'").fetchone()[0] == 6
    conn.execute("UPDATE derived_meta SET v='built-by-older-code' WHERE k='derivation'")
    conn.commit()
    conn.close()

    conn = S.connect(tmp_path / "t.db")
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE title='stale'").fetchone()[0] == 0
    conn.execute(REFERENCE_VIEW)
    assert _rows(conn, "jobs") == _rows(conn, "jobs_reference")


def test_a_database_from_before_the_table_gets_one(db, tmp_path):
    """Upgrading: an existing jobs.db has no job_derived and no fingerprint."""
    db.execute("DROP VIEW jobs")
    db.execute("DROP TABLE job_derived")
    db.execute("DELETE FROM derived_meta")
    db.commit()
    db.close()
    conn = S.connect(tmp_path / "t.db")
    conn.execute(REFERENCE_VIEW)
    assert _rows(conn, "jobs") == _rows(conn, "jobs_reference")


# --- step 2: descriptions load on demand --------------------------------------


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSEARCH_DB", str(tmp_path / "t.db"))
    return TestClient(create_app(Config.load()))


def test_the_row_list_does_not_ship_descriptions(client):
    """Descriptions were ~90% of every /api/jobs payload and are only read when
    a row is expanded."""
    rows = client.post("/api/jobs", json={"limit": 50, "offset": 0}).json()["rows"]
    assert rows and all("description" not in r for r in rows)
    assert all("title" in r and "apply_url" in r for r in rows)


def test_one_description_is_served_by_id(client):
    r = client.get("/api/job", params={"id": "himalayas:hm1"})
    assert r.status_code == 200
    assert r.json() == {"source_job_id": "himalayas:hm1", "description": "hm body"}


def test_enriched_description_wins(client, db):
    uid = db.execute("SELECT source_job_id FROM job_detail").fetchone()[0]
    assert client.get("/api/job", params={"id": uid}).json()["description"] == \
        "full detail body"


def test_an_unknown_id_is_a_404(client):
    assert client.get("/api/job", params={"id": "wellfound:nope"}).status_code == 404


def test_search_still_matches_description_text(client):
    """Not shipping the text must not stop the filter reading it."""
    res = client.post("/api/jobs", json={"q": "ro body", "limit": 50, "offset": 0}).json()
    assert [r["source_job_id"] for r in res["rows"]] == ["remoteok:77"]


# --- facets match the search text once, not once per dimension ----------------


@pytest.mark.parametrize("hide_expired", [True, False])
def test_facets_under_a_search_agree_with_the_row_list(client, db, hide_expired):
    """The search is matched once into a temp table and reused. It must stay
    exactly the text clause: folding hide_expired or the hidden-status default
    into it would drop rows the user asked to see."""
    db.execute("UPDATE job_raw SET raw_json = json_set(raw_json,'$.expiryDate',1)"
               " WHERE source='himalayas'")          # now expired
    db.commit()
    body = {"q": "body", "limit": 50, "offset": 0, "hide_expired": hide_expired}
    listed = client.post("/api/jobs", json=body).json()["total"]
    f = client.post("/api/facets", json=body).json()
    assert f["total"] == listed
    assert sum(x["count"] for x in f["sources"]) == listed
    assert ("himalayas" in [x["value"] for x in f["sources"]]) is (not hide_expired)


def test_a_second_search_replaces_the_first(client):
    """The temp table lives on a reused per-thread connection."""
    a = client.post("/api/facets", json={"q": "ro body"}).json()["total"]
    b = client.post("/api/facets", json={"q": "hm body"}).json()["total"]
    c = client.post("/api/facets", json={}).json()["total"]
    assert (a, b) == (1, 1) and c > 2

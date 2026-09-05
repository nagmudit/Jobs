"""Multi-source: the core-view contract, id namespacing, and per-source guards.

No network. Payloads are hand-built to the shapes measured on 2026-09-04.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from src import assertions as A
from src import sources as SRC
from src import store as S
from src.sources import himalayas, remoteok

DAY = 86400


def _ts(days_ago: int) -> int:
    return int(time.time()) - days_ago * DAY


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


# --- the contract -------------------------------------------------------------


def test_every_source_view_matches_the_column_contract(conn):
    """UNION ALL aligns by POSITION. A source that adds, drops or reorders a
    column still builds a valid view and silently returns another column's
    values, so this is checked at connect time."""
    SRC.assert_core_views(conn)  # raises on drift


def test_contract_drift_is_detected(conn):
    class Fake:
        CORE_VIEW_SQL = "SELECT 1 AS source, 2 AS source_job_id"

    real = SRC.registry
    SRC.registry = lambda: {"fake": Fake}
    try:
        with pytest.raises(RuntimeError, match="does not match CORE_COLUMNS"):
            SRC.assert_core_views(conn)
    finally:
        SRC.registry = real


def test_all_sources_registered():
    assert set(SRC.registry()) == {
        "wellfound", "remoteok", "himalayas",
        "greenhouse", "ashby", "workable", "lever",
    }


# --- id namespacing -----------------------------------------------------------


def test_same_native_id_across_sources_does_not_collide(conn):
    """The reason ids are namespaced: RemoteOK 1137286 and a Wellfound 1137286
    would otherwise be one row, silently overwriting each other."""
    S.upsert_job(conn, "1137286", "acme", {"title": "A", "slug": "a"}, source="wellfound")
    S.upsert_job(conn, "1137286", "acme", {"position": "B"}, source="remoteok")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == 2
    ids = {r[0] for r in conn.execute("SELECT source_job_id FROM job_raw")}
    assert ids == {"wellfound:1137286", "remoteok:1137286"}


def test_same_company_slug_across_sources_does_not_collide(conn):
    S.upsert_company(conn, "acme", {"name": "Acme WF"}, source="wellfound")
    S.upsert_company(conn, "acme", {"name": "Acme RO"}, source="remoteok")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM company_raw").fetchone()[0] == 2


def test_job_uid_shape():
    assert S.job_uid("remoteok", "42") == "remoteok:42"


# --- RemoteOK -----------------------------------------------------------------

RO_JOB = {"id": "1", "slug": "x", "position": "Dev", "company": "Acme",
          "epoch": 1788372677, "date": "2026-09-02T18:11:17+00:00",
          "description": "d", "location": "", "salary_min": 50000,
          "salary_max": 70000, "tags": ["dev"], "url": "https://remoteOK.com/x"}
RO_LEGAL = {"legal": "API Terms of Service: Please link back ...",
            "last_updated": 1788462746}


def test_remoteok_skips_the_legal_element_by_shape_not_position():
    """The first array element is a terms-of-service notice, not a job.
    Skipping index 0 would break the day they reorder it."""
    out = remoteok.parse(json.dumps([RO_LEGAL, RO_JOB]), "u")
    assert len(out) == 1 and out[0]["position"] == "Dev"
    # Same payload, notice moved to the end.
    assert len(remoteok.parse(json.dumps([RO_JOB, RO_LEGAL]), "u")) == 1


def test_remoteok_raises_when_no_element_looks_like_a_job():
    with pytest.raises(A.SchemaDrift, match="job shape changed|none carry"):
        remoteok.parse(json.dumps([RO_LEGAL, {"headline": "renamed"}]), "u")


def test_remoteok_raises_on_wrong_root_type():
    with pytest.raises(A.SchemaDrift, match="expected a JSON array"):
        remoteok.parse(json.dumps({"jobs": []}), "u")


def test_remoteok_raises_on_non_json():
    with pytest.raises(A.SchemaDrift, match="not JSON"):
        remoteok.parse("<html>nope</html>", "u")


def test_remoteok_empty_array_is_not_an_error():
    """An empty result is a legitimate response, not a shape change."""
    assert remoteok.parse(json.dumps([]), "u") == []


# --- Himalayas ----------------------------------------------------------------

HM_JOB = {
    "title": "Eng", "companyName": "Acme", "companySlug": "acme",
    "description": "d", "employmentType": "Full Time",
    "minSalary": 45000, "maxSalary": 52000, "currency": "USD",
    "salaryPeriod": "annual", "seniority": ["Mid-level"],
    "locationRestrictions": ["United States"], "categories": ["Eng"],
    "pubDate": 1788451065, "expiryDate": 1793635064,
    "applicationLink": "https://himalayas.app/companies/acme/jobs/eng",
    "guid": "https://himalayas.app/companies/acme/jobs/eng",
}


def test_himalayas_raises_without_jobs_key():
    with pytest.raises(A.SchemaDrift, match="expected an object with 'jobs'"):
        himalayas.parse(json.dumps({"results": []}), "u")


def test_himalayas_raises_when_jobs_is_not_a_list():
    with pytest.raises(A.SchemaDrift, match="not a list"):
        himalayas.parse(json.dumps({"jobs": {"a": 1}}), "u")


def test_himalayas_native_id_is_the_path_not_the_whole_url():
    nid = himalayas.native_id(HM_JOB)
    assert nid == "companies/acme/jobs/eng"
    assert "https://" not in nid


def test_himalayas_offset_mismatch_raises_pagewrap(conn):
    """If the server ever stops honouring offset the way Wellfound wraps pages,
    we would silently re-ingest page 1 forever."""
    class F:
        # Listings carry a freshness window; the fake ignores it but must
        # expose it, because production reads fetcher.listing_ttl.
        listing_ttl = None

        def get(self, url, refresh=False, max_age=None):
            class R:
                ok = True
                status = 200
                text = json.dumps({"jobs": [HM_JOB], "totalCount": 5, "offset": 0})
            return R()

    with pytest.raises(A.PageWrap, match="requested offset=40"):
        himalayas.ingest(F(), conn, {"offset": 40}, max_pages=1)


# --- salary correctness across sources ----------------------------------------


def test_hourly_salary_is_not_emitted_as_a_number(conn):
    """3 of 40 Himalayas rows sampled were hourly. An hourly 70 next to an
    annual 70000 sorts silently wrong, so numeric stays NULL for non-annual."""
    hourly = dict(HM_JOB, salaryPeriod="hourly", minSalary=70, maxSalary=70,
                  guid="https://himalayas.app/companies/acme/jobs/hourly")
    S.upsert_job(conn, himalayas.native_id(hourly), "acme", hourly, source="himalayas")
    S.upsert_job(conn, himalayas.native_id(HM_JOB), "acme", HM_JOB, source="himalayas")
    conn.commit()
    rows = {r["salary_period"]: r for r in conn.execute(
        "SELECT salary_period, salary_min, salary_max, salary_raw FROM jobs")}
    assert rows["hourly"]["salary_min"] is None
    assert rows["hourly"]["salary_raw"] is not None      # raw is still shown
    assert rows["annual"]["salary_min"] == 45000


def test_native_numeric_salary_is_used_without_reparsing(conn):
    S.upsert_job(conn, "1", "acme", RO_JOB, source="remoteok")
    conn.commit()
    r = conn.execute("SELECT salary_min, salary_max, salary_currency FROM jobs").fetchone()
    assert (r["salary_min"], r["salary_max"], r["salary_currency"]) == (50000, 70000, "USD")


# --- expiry -------------------------------------------------------------------


def test_expired_flag_is_not_always_true(conn):
    """SQLite orders every INTEGER before every TEXT, and strftime returns TEXT.
    Without a CAST, `expires_ts < strftime(...)` is unconditionally true and
    every job with an expiry reads as expired."""
    future = dict(HM_JOB, expiryDate=_ts(-30),
                  guid="https://himalayas.app/companies/acme/jobs/future")
    past = dict(HM_JOB, expiryDate=_ts(30),
                guid="https://himalayas.app/companies/acme/jobs/past")
    for j in (future, past):
        S.upsert_job(conn, himalayas.native_id(j), "acme", j, source="himalayas")
    conn.commit()
    got = {r["native_id"].split("/")[-1]: r["expired"] for r in conn.execute(
        "SELECT native_id, expired FROM jobs")}
    assert got["future"] == 0
    assert got["past"] == 1


def test_expired_is_null_where_the_source_has_no_expiry(conn):
    """NULL means unknown, never 'not expired'. Wellfound publishes no expiry."""
    S.upsert_job(conn, "9", "acme", {"title": "T", "slug": "t"}, source="wellfound")
    conn.commit()
    assert conn.execute("SELECT expired FROM jobs").fetchone()["expired"] is None


# --- migration ----------------------------------------------------------------


def test_v1_database_migrates_to_namespaced_ids(tmp_path):
    """A pre-multi-source database must keep every row, and user_state must
    follow its job across the rename."""
    p = tmp_path / "old.db"
    raw = sqlite3.connect(str(p))
    raw.executescript("""
        CREATE TABLE job_raw (source_job_id TEXT PRIMARY KEY, company_slug TEXT NOT NULL,
          raw_json TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
        CREATE TABLE company_raw (company_slug TEXT PRIMARY KEY, raw_json TEXT NOT NULL,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
        CREATE TABLE job_provenance (source_job_id TEXT, role_slug TEXT, location TEXT,
          page INTEGER, PRIMARY KEY (source_job_id, role_slug, location));
        CREATE TABLE user_state (source_job_id TEXT PRIMARY KEY, status TEXT, note TEXT,
          updated_at TEXT);
        INSERT INTO job_raw VALUES ('4235671','acme','{"title":"AI Engineer","slug":"ai"}','t','t');
        INSERT INTO company_raw VALUES ('acme','{"name":"Acme"}','t','t');
        INSERT INTO job_provenance VALUES ('4235671','ai-engineer','anywhere',1);
        INSERT INTO user_state VALUES ('4235671','applied',NULL,'t');
    """)
    raw.commit()
    raw.close()

    conn = S.connect(p)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == S.SCHEMA_VERSION
    r = conn.execute("SELECT source, source_job_id, native_id FROM job_raw").fetchone()
    assert (r["source"], r["source_job_id"], r["native_id"]) == \
        ("wellfound", "wellfound:4235671", "4235671")
    j = conn.execute("SELECT status, found_via_roles FROM jobs").fetchone()
    assert j["status"] == "applied", "user_state did not follow the renamed id"
    assert j["found_via_roles"] == "ai-engineer"
    assert [c["name"] for c in conn.execute("PRAGMA table_info(company_raw)")
            if c["pk"]] == ["company_slug", "source"]


def test_migration_is_idempotent(tmp_path):
    p = tmp_path / "m.db"
    conn = S.connect(p)
    S.upsert_job(conn, "1", "c", {"title": "T"}, source="wellfound")
    conn.commit()
    before = conn.execute("SELECT source_job_id FROM job_raw").fetchone()[0]
    assert S.migrate(conn) == []
    assert conn.execute("SELECT source_job_id FROM job_raw").fetchone()[0] == before

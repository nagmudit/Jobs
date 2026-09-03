"""Role URL shapes, derived locations, and facet cascading. No network."""

from __future__ import annotations

import pytest

from src import store as S
from src.fetch import search_url
from src.parse import parse_search_page
from src.web.app import Filters, _clauses, _where

from . import fixtures as F


# --- URL shapes ---------------------------------------------------------------


def test_anywhere_maps_to_bare_role_url():
    """The widest shape: /role/{slug}, no location filter at all."""
    assert search_url("devops-engineer", "anywhere") == \
        "https://wellfound.com/role/devops-engineer"
    assert search_url("devops-engineer", "anywhere", 4) == \
        "https://wellfound.com/role/devops-engineer?page=4"


def test_remote_and_place_shapes_are_unchanged():
    assert search_url("x", "remote") == "https://wellfound.com/role/r/x"
    assert search_url("x", "pune") == "https://wellfound.com/role/l/x/pune"
    assert search_url("x", "pune", 2) == "https://wellfound.com/role/l/x/pune?page=2"


def test_all_three_shapes_are_distinct():
    urls = {search_url("x", loc) for loc in ("anywhere", "remote", "pune")}
    assert len(urls) == 3


# --- derived locations --------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    conn = S.connect(tmp_path / "t.db")
    page = parse_search_page(F.page(n_companies=2, jobs_per_company=1), "u")
    for startup, jobs in page.startups:
        S.upsert_company(conn, startup["slug"], startup)
        for jid, raw in jobs:
            S.upsert_job(conn, jid, startup["slug"], raw)
            S.add_provenance(conn, jid, "ai-engineer", "anywhere", 1)
    conn.commit()
    return conn


def test_rebuild_locations_derives_from_raw(db):
    """The ADR-002 promise: a new filter dimension is a re-derivation of stored
    raw JSON, never a re-crawl."""
    assert db.execute("SELECT COUNT(*) FROM job_location").fetchone()[0] == 0
    n = S.rebuild_locations(db)
    assert n == 2  # one locationName ("Bengaluru") per job, two jobs
    rows = db.execute("SELECT location, kind FROM job_location").fetchall()
    assert {r["location"] for r in rows} == {"Bengaluru"}
    assert {r["kind"] for r in rows} == {"onsite"}


def test_rebuild_locations_is_idempotent(db):
    a = S.rebuild_locations(db)
    b = S.rebuild_locations(db)
    assert a == b


def test_remote_locations_recorded_separately(tmp_path):
    conn = S.connect(tmp_path / "t2.db")
    S.upsert_company(conn, "c", {"name": "C", "slug": "c"})
    S.upsert_job(conn, "1", "c", {
        "title": "T", "slug": "t",
        "locationNames": ["Pune"],
        "acceptedRemoteLocationNames": ["India"],
    })
    conn.commit()
    S.rebuild_locations(conn)
    got = {(r["location"], r["kind"])
           for r in conn.execute("SELECT location, kind FROM job_location")}
    assert got == {("Pune", "onsite"), ("India", "remote")}


def test_location_filter_targets_real_places_not_slice_tokens():
    """Regression: the UI used to offer the crawl slice tokens (bangalore,
    remote, india) as 'Location'. It must query job_location instead."""
    where, params = _where(Filters(locations=["Pune", "Bengaluru"]))
    assert "job_location" in where
    assert "found_via_locations" not in where
    assert params[:2] == ["Pune", "Bengaluru"]


def test_location_clause_is_qualified():
    """source_job_id exists on jobs, job_provenance AND job_location, so the
    clause must be qualified or facet joins raise 'ambiguous column name'."""
    where, _ = _where(Filters(locations=["Pune"]))
    assert "j.source_job_id IN" in where


# --- facet cascading ----------------------------------------------------------


def test_exclude_drops_only_its_own_dimension():
    f = Filters(locations=["Pune"], sizes=["11-50"], remote=["Remote"])
    full, _ = _where(f)
    for dim, marker in (("locations", "job_location"),
                        ("sizes", "company_size"),
                        ("remote", "remote_label")):
        w, _ = _where(f, exclude=dim)
        assert marker not in w, f"{dim} not excluded"
        # the other two survive
        others = [m for d, m in (("locations", "job_location"),
                                 ("sizes", "company_size"),
                                 ("remote", "remote_label")) if d != dim]
        for o in others:
            assert o in w


def test_params_stay_aligned_with_placeholders_under_exclusion():
    f = Filters(q="ml", locations=["Pune", "Delhi"], sizes=["11-50"],
                salary_min=100000, max_days_old=30)
    for dim in (None, "locations", "sizes", "q", "salary"):
        w, p = _where(f, exclude=dim)
        assert w.count("?") == len(p), f"placeholder/param mismatch excluding {dim}"


def test_every_clause_declares_a_dimension():
    """A clause with no dimension name could never be excluded, so its facet
    would silently fail to cascade."""
    f = Filters(q="x", roles=["r"], locations=["l"], remote=["Remote"],
                sizes=["1-10"], job_types=["full-time"], companies=["Acme"],
                salary_min=1, has_equity=True, has_ats=True, max_days_old=1,
                statuses=["new"])
    dims = [d for d, _, _ in _clauses(f)]
    assert len(dims) == len(set(dims)), f"duplicate dimension names: {dims}"
    assert all(dims)


def test_hidden_still_excluded_by_default_and_is_a_dimension():
    dims = dict((d, s) for d, s, _ in _clauses(Filters()))
    assert "statuses" in dims and "status != 'hidden'" in dims["statuses"]

"""Greenhouse: company-scoped expansion, token resolution, content decoding.

No network. Payloads match the live shape verified 2026-09-04.
"""

from __future__ import annotations

import json

import pytest

from src import assertions as A
from src import ats as ATS
from src import store as S
from src.sources import greenhouse as GH

GH_JOB = {
    "id": 8029118,
    "internal_job_id": 3462255,
    "title": "Machine Learning Engineer",
    "company_name": "SingleStore",
    "absolute_url": "https://job-boards.greenhouse.io/singlestore/jobs/8029118",
    "location": {"name": "United States "},
    "offices": [{"id": 1, "name": "Hyderabad",
                 "location": "Hyderabad, Telangana, India"}],
    "departments": [{"id": 74243, "name": "Engineering"}],
    "first_published": "2026-06-25T15:00:17-04:00",
    "updated_at": "2026-08-21T16:09:33-04:00",
    # Greenhouse double-encodes: entity-encoded HTML.
    "content": "&lt;p&gt;Build &lt;b&gt;models&lt;/b&gt;&lt;/p&gt;",
}


def _payload(jobs):
    return json.dumps({"jobs": jobs, "meta": {"total": len(jobs)}})


class Fetch:
    """Serves one payload for board tokens in `boards`, 404 otherwise."""

    def __init__(self, boards: dict[str, list]):
        self.boards = boards
        self.urls: list[str] = []

    # Listings carry a freshness window; the fake ignores it but must
    # expose it, because production reads fetcher.listing_ttl.
    listing_ttl = None

    def get(self, url, refresh=False, max_age=None):
        self.urls.append(url)
        token = url.split("/boards/")[1].split("/")[0]
        outer = self

        class R:
            status = 200 if token in outer.boards else 404
            ok = token in outer.boards
            text = _payload(outer.boards.get(token, []))
        return R()


# --- parsing ------------------------------------------------------------------


def test_content_is_double_decoded():
    """Greenhouse sends entity-encoded HTML, so a single strip leaves &lt;p&gt;."""
    got = GH.clean_content(GH_JOB["content"])
    assert "&lt;" not in got and "<p>" not in got
    assert "Build" in got and "models" in got


def test_location_prefers_structured_offices_over_free_text():
    """location.name is messy free text ("United States ", "United States &
    EMEA"); offices[].location is structured."""
    assert GH.location_of(GH_JOB) == "Hyderabad, Telangana, India"
    assert GH.location_of({"location": {"name": "  Remote  "}}) == "Remote"


def test_native_id_is_namespaced_by_board():
    """Greenhouse ids look globally unique but nothing documents that; a
    collision would silently merge two companies' jobs."""
    assert GH.native_id("singlestore", GH_JOB) == "singlestore/8029118"


def test_parse_raises_on_shape_change():
    with pytest.raises(A.SchemaDrift, match="expected an object with 'jobs'"):
        GH.parse(json.dumps({"results": []}), "u")
    with pytest.raises(A.SchemaDrift, match="not a list"):
        GH.parse(json.dumps({"jobs": 5}), "u")
    with pytest.raises(A.SchemaDrift, match="not JSON"):
        GH.parse("<html>", "u")


# --- token resolution ---------------------------------------------------------


def test_candidates_strip_wellfound_disambiguators():
    """Measured misses: alloy-2, assemblyai-1, 10a-labs-1. The -N suffix is
    Wellfound's, not the company's, so it never appears in a board token.

    Passed with NO company name, so the assertion isolates the stripping rather
    than passing via the name-derived candidate -- which is how an earlier
    version of this test passed even with the stripping removed.
    """
    assert "alloy" in ATS.token_candidates("alloy-2", None)
    assert "assemblyai" in ATS.token_candidates("assemblyai-1", None)
    assert "10a-labs" in ATS.token_candidates("10a-labs-1", None)


def test_candidates_use_the_company_name_when_the_slug_differs():
    """afresh-technologies -> the board is 'afresh'."""
    c = ATS.token_candidates("afresh-technologies", "Afresh")
    assert "afresh" in c


def test_provider_of_parses_the_wellfound_hint():
    assert ATS.provider_of("AtsIntegration::Greenhouse::Listing") == "greenhouse"
    assert ATS.provider_of(None) is None


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


def test_resolve_finds_the_token_and_caches_it(conn):
    f = Fetch({"alloy": [GH_JOB]})
    rec = ATS.resolve(f, conn, "alloy-2", "Alloy", "greenhouse")
    assert rec["resolved"] == 1 and rec["board_token"] == "alloy"

    n = len(f.urls)
    again = ATS.resolve(f, conn, "alloy-2", "Alloy", "greenhouse")
    assert again["board_token"] == "alloy"
    assert len(f.urls) == n, "cached hit still hit the network"


def test_resolve_caches_misses_too(conn):
    """Re-probing six candidates for a company with no board is pure cost
    against a third party."""
    f = Fetch({})
    rec = ATS.resolve(f, conn, "nope", "Nope", "greenhouse")
    assert rec["resolved"] == 0
    n = len(f.urls)
    ATS.resolve(f, conn, "nope", "Nope", "greenhouse")
    assert len(f.urls) == n, "a cached miss was re-probed"


def test_unimplemented_provider_is_recorded_not_attempted(conn):
    f = Fetch({})
    rec = ATS.resolve(f, conn, "acme", "Acme", "dover")
    assert rec["resolved"] == 0 and f.urls == []
    assert "not implemented" in rec["attempts"]


# --- expansion ----------------------------------------------------------------


def test_expand_applies_the_local_role_filter(conn):
    off = dict(GH_JOB, id=2, title="Office Manager",
               departments=[{"id": 1, "name": "Operations"}])
    f = Fetch({"acme": [GH_JOB, off]})
    r = GH.expand_company(f, conn, "acme-co", "acme",
                          keywords=["machine learning"])
    assert r["seen"] == 1 and r["filtered_out"] == 1
    assert r["filter_mode"] == "local"
    assert [x[0] for x in conn.execute("SELECT title FROM jobs")] == \
        ["Machine Learning Engineer"]


def test_expanded_jobs_join_the_existing_company(conn):
    """ATS jobs must land under the SAME company_slug as the Wellfound rows, so
    a company's jobs stay together across sources."""
    f = Fetch({"acme": [GH_JOB]})
    GH.expand_company(f, conn, "acme-co", "acme", keywords=["machine learning"])
    r = conn.execute("SELECT company_slug, company, source FROM jobs").fetchone()
    assert r["company_slug"] == "acme-co"
    assert r["company"] == "SingleStore"
    assert r["source"] == "greenhouse"


def test_view_exposes_posted_ts_from_an_offset_timestamp(conn):
    """first_published carries a UTC offset; SQLite parses it, so no injected
    epoch is stored."""
    f = Fetch({"acme": [GH_JOB]})
    GH.expand_company(f, conn, "acme-co", "acme", keywords=[])
    r = conn.execute("SELECT posted_ts, posted_at, description, apply_url, "
                     "ats_source FROM jobs").fetchone()
    assert r["posted_ts"] == 1782414017
    assert r["posted_at"].startswith("2026-06-25")
    assert "&lt;" not in (r["description"] or "")
    assert r["apply_url"].startswith("https://job-boards.greenhouse.io/")
    assert "Greenhouse" in r["ats_source"]


def test_salary_is_null_not_guessed(conn):
    """pay_input_ranges is absent from the list endpoint and was null on the
    single-job endpoint too."""
    f = Fetch({"acme": [GH_JOB]})
    GH.expand_company(f, conn, "acme-co", "acme", keywords=[])
    r = conn.execute("SELECT salary_raw, salary_min, salary_currency FROM jobs").fetchone()
    assert r["salary_raw"] is None and r["salary_min"] is None
    assert r["salary_currency"] is None


def test_stale_jobs_are_dropped_and_counted(conn):
    import datetime as dt
    import time

    # GH_JOB's own first_published is fixed and already months old, so the
    # "fresh" case needs a generated recent date.
    recent = dict(GH_JOB, id=5, first_published=(
        dt.datetime.now(dt.UTC) - dt.timedelta(days=2)).isoformat())
    old = dict(GH_JOB, id=3, first_published="2020-01-01T00:00:00-04:00")
    f = Fetch({"acme": [recent, old]})
    r = GH.expand_company(f, conn, "acme-co", "acme", keywords=[],
                          cutoff_ts=int(time.time()) - 30 * 86400)
    assert r["stale_skipped"] == 1


def test_unparseable_date_is_kept_not_dropped(conn):
    """Unknown age is not old (ADR-006)."""
    import time

    bad = dict(GH_JOB, id=4, first_published="not-a-date", updated_at=None)
    f = Fetch({"acme": [bad]})
    r = GH.expand_company(f, conn, "acme-co", "acme", keywords=[],
                          cutoff_ts=int(time.time()) - 30 * 86400)
    assert r["seen"] == 1 and r["stale_skipped"] == 0


def test_companies_for_role_is_bounded_by_the_corpus(conn):
    """Expansion must never walk the whole company table."""
    S.upsert_company(conn, "acme", {"name": "Acme"}, source="wellfound")
    S.upsert_job(conn, "1", "acme",
                 {"title": "AI Engineer", "slug": "ai",
                  "atsSource": "AtsIntegration::Greenhouse::Listing"},
                 source="wellfound")
    S.add_provenance(conn, S.job_uid("wellfound", "1"), "ai-engineer", "anywhere", 1)
    S.upsert_company(conn, "other", {"name": "Other"}, source="wellfound")
    S.upsert_job(conn, "2", "other", {"title": "Chef", "slug": "c"}, source="wellfound")
    S.add_provenance(conn, S.job_uid("wellfound", "2"), "ai-engineer", "anywhere", 1)
    conn.commit()

    got = ATS.companies_for_role(conn, "ai-engineer", "greenhouse")
    assert [c["company_slug"] for c in got] == ["acme"]


def test_expanded_jobs_record_the_role_as_provenance(conn):
    """Without this they are invisible to the found_via_roles facet, breaking
    the one cross-source vocabulary ADR-009 established."""
    f = Fetch({"acme": [GH_JOB]})
    GH.expand_company(f, conn, "acme-co", "acme", keywords=[],
                      role="machine-learning-engineer")
    r = conn.execute("SELECT found_via_roles FROM jobs").fetchone()
    assert r["found_via_roles"] == "machine-learning-engineer"

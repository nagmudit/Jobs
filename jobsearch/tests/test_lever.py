"""Lever: company-scoped expansion, millisecond timestamps, stated intervals.

No network. Payloads match the live shape verified 2026-09-06 against
`api.lever.co/v0/postings/metabase?mode=json`.
"""

from __future__ import annotations

import json

import pytest

from src import assertions as A
from src import ats as ATS
from src import store as S
from src.sources import lever as LV

# Field-for-field the live Metabase/Samba shape, description shortened.
LV_JOB = {
    "id": "5eca795c-48dd-496a-be23-2181068a5450",
    "text": "Machine Learning Engineer",
    "createdAt": 1605753685375,          # epoch MILLISECONDS -> 2020-11-19
    "hostedUrl": "https://jobs.lever.co/metabase/5eca795c",
    "applyUrl": "https://jobs.lever.co/metabase/5eca795c/apply",
    "workplaceType": "remote",
    "country": "US",
    "categories": {
        "commitment": "Full-time (remote)",
        "location": "Remote-North & South America",
        "team": "Engineering",
        "department": "Engineering",
        "allLocations": ["Remote-North & South America"],
    },
    "descriptionPlain": "Build models and ship them.",
    "description": "<div>Build models and ship them.</div>",
    "lists": [{"text": "What You'll Do",
               "content": "\n<li>Train models</li><li>Ship them</li>"}],
    "additionalPlain": "We are an equal opportunity employer.",
    "salaryRange": {"min": 90000, "max": 190000,
                    "currency": "USD", "interval": "per-year-salary"},
}


class Fetch:
    """Serves a board for known tokens; Lever's real 404 body otherwise -- a
    well-formed JSON OBJECT, which is the trap `parse` has to catch."""

    listing_ttl = None

    def __init__(self, boards: dict[str, list]):
        self.boards = boards
        self.urls: list[str] = []

    def get(self, url, refresh=False, max_age=None):
        self.urls.append(url)
        token = url.split("/postings/")[1].split("?")[0]
        outer = self

        class R:
            status = 200 if token in outer.boards else 404
            ok = token in outer.boards
            text = (json.dumps(outer.boards[token]) if token in outer.boards
                    else json.dumps({"ok": False, "error": "Document not found"}))
        return R()


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


# --- parsing ------------------------------------------------------------------


def test_parse_rejects_the_404_body_even_though_it_is_valid_json():
    """Lever's "no such board" reply is a well-formed OBJECT. Accepting any
    valid JSON would turn a missing board into a board with strange fields."""
    with pytest.raises(A.SchemaDrift, match="expected a list"):
        LV.parse(json.dumps({"ok": False, "error": "Document not found"}), "u")


def test_parse_raises_on_other_shape_changes():
    with pytest.raises(A.SchemaDrift, match="not JSON"):
        LV.parse("<html>", "u")
    with pytest.raises(A.SchemaDrift, match="expected a list"):
        LV.parse(json.dumps("nope"), "u")


def test_parse_returns_the_registry_shape():
    """Every source's parse returns a dict carrying `jobs`; `ats.resolve` counts
    `data["jobs"]` for all of them. Lever's wire format is a bare array, so the
    adaptation happens here rather than special-casing the resolver."""
    got = LV.parse(json.dumps([LV_JOB]), "u")
    assert isinstance(got, dict) and len(got["jobs"]) == 1


def test_created_at_is_milliseconds_not_seconds():
    """1605753685375 read as seconds lands ~50,000 years out and every posting
    is then permanently 'fresh'."""
    assert LV.posted_ts_of(LV_JOB) == 1605753685
    assert LV.posted_ts_of({}) is None
    assert LV.posted_ts_of({"createdAt": "nope"}) is None


def test_description_assembles_the_lists_blocks():
    """descriptionPlain is only the opening; requirements live in `lists`, whose
    content is HTML even though the description is not."""
    got = LV.description_of(LV_JOB)
    assert "Build models" in got
    assert "What You'll Do" in got and "Train models" in got
    assert "<li>" not in got
    assert "equal opportunity" in got


def test_location_prefers_all_locations():
    assert LV.location_of(LV_JOB) == "Remote-North & South America"
    assert LV.location_of({"categories": {"location": "Berlin"}}) == "Berlin"
    assert LV.location_of({"categories": {}}) is None


def test_native_id_is_namespaced_by_board():
    assert LV.native_id("metabase", LV_JOB).startswith("metabase/5eca795c")
    assert LV.native_id("m", {}) is None


def test_board_url_only_ever_touches_the_permitted_origin():
    """www.lever.co disallows /api/. Only api.lever.co may be used."""
    for u in (LV.board_url("m"), LV.board_url("m", content=False)):
        assert u.startswith("https://api.lever.co/v0/postings/")
        assert "www.lever.co" not in u


# --- salary -------------------------------------------------------------------


def test_a_yearly_interval_yields_numeric_salary():
    s = LV.salary_of(LV_JOB)
    assert s["min"] == 90000 and s["max"] == 190000
    assert s["currency"] == "USD" and s["period"] == "annual"


def test_a_non_yearly_interval_keeps_the_string_and_nulls_the_numbers():
    """Mixing an hourly 45 into an annual column sorts silently wrong."""
    hourly = dict(LV_JOB, salaryRange={"min": 45, "max": 60, "currency": "USD",
                                       "interval": "per-hour-wage"})
    s = LV.salary_of(hourly)
    assert s["min"] is None and s["max"] is None
    assert s["period"] == "hourly" and "45" in s["raw"]


def test_absent_salary_is_all_null():
    s = LV.salary_of(dict(LV_JOB, salaryRange=None))
    assert all(s[k] is None for k in ("raw", "min", "max", "currency", "period"))


# --- resolution ---------------------------------------------------------------


def test_resolve_counts_jobs_without_tripping_on_the_list_shape(conn):
    """The integration bug this test exists for: ats.resolve does
    data.get("jobs"), so a parse returning a bare list raises AttributeError and
    takes the whole run down."""
    f = Fetch({"metabase": [LV_JOB]})
    rec = ATS.resolve(f, conn, "metabase", "Metabase", "lever")
    assert rec["resolved"] == 1 and rec["n_jobs"] == 1


def test_a_missing_board_is_recorded_as_a_miss(conn):
    f = Fetch({})
    rec = ATS.resolve(f, conn, "nope", "Nope", "lever")
    assert rec["resolved"] == 0 and rec["board_token"] is None


# --- expansion ----------------------------------------------------------------


def test_expand_applies_the_local_role_filter(conn):
    off = dict(LV_JOB, id="x2", text="Controller",
               categories={"team": "Operations", "department": "Finance"})
    f = Fetch({"m": [LV_JOB, off]})
    r = LV.expand_company(f, conn, "metabase", "m", keywords=["machine learning"])
    assert r["seen"] == 1 and r["filtered_out"] == 1
    assert r["filter_mode"] == "local"
    assert [x[0] for x in conn.execute("SELECT title FROM jobs")] == \
        ["Machine Learning Engineer"]


def test_the_whole_board_costs_one_request(conn):
    f = Fetch({"m": [LV_JOB, dict(LV_JOB, id="b"), dict(LV_JOB, id="c")]})
    LV.expand_company(f, conn, "metabase", "m", keywords=[])
    assert len(f.urls) == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3


def test_view_maps_every_field(conn):
    f = Fetch({"m": [LV_JOB]})
    LV.expand_company(f, conn, "metabase", "m", keywords=[])
    r = conn.execute(
        "SELECT title, posted_ts, posted_at, description, apply_url, ats_source, "
        "job_type, remote, remote_label, location_raw, badges, salary_min, "
        "salary_currency, salary_period, salary_usd_min FROM jobs").fetchone()
    assert r["title"] == "Machine Learning Engineer"
    assert r["posted_ts"] == 1605753685        # ms converted once, at ingest
    assert r["posted_at"].startswith("2020-11-19")
    assert "<li>" not in (r["description"] or "")
    assert r["apply_url"].endswith("/apply")
    assert "Lever" in r["ats_source"]
    assert r["job_type"] == "Full-time (remote)"
    assert r["remote"] == 1 and r["remote_label"] == "Remote"
    assert r["location_raw"] == "Remote-North & South America"
    assert r["badges"] == "Engineering"        # team == department, deduped
    assert r["salary_min"] == 90000 and r["salary_currency"] == "USD"
    assert r["salary_period"] == "annual"
    assert r["salary_usd_min"] == 90000        # ADR-012 sort key


@pytest.mark.parametrize("wt,label", [
    ("remote", "Remote"), ("onsite", "Onsite"), ("on-site", "Onsite"),
    ("hybrid", "Onsite or Remote"), ("unspecified", None),
])
def test_workplace_type_maps_to_the_canonical_labels(conn, wt, label):
    """Both spellings of onsite occur: the docs say `on-site`, live data sends
    `onsite`. A second label set would split the UI facet in half."""
    f = Fetch({"m": [dict(LV_JOB, id=f"w-{wt}", workplaceType=wt)]})
    LV.expand_company(f, conn, "metabase", "m", keywords=[])
    assert conn.execute("SELECT remote_label FROM jobs").fetchone()[0] == label


def test_stale_postings_are_dropped_and_counted(conn):
    import time

    # LV_JOB's own createdAt is from 2020 and genuinely stale.
    fresh_ms = int((time.time() - 2 * 86400) * 1000)
    fresh = dict(LV_JOB, id="fresh", createdAt=fresh_ms)
    f = Fetch({"m": [fresh, LV_JOB]})
    r = LV.expand_company(f, conn, "metabase", "m", keywords=[],
                          cutoff_ts=int(time.time()) - 30 * 86400)
    assert r["seen"] == 1 and r["stale_skipped"] == 1


def test_a_posting_with_no_date_is_kept(conn):
    """Unknown age is not old (ADR-006)."""
    import time

    undated = dict(LV_JOB, id="undated")
    undated.pop("createdAt")
    f = Fetch({"m": [undated]})
    r = LV.expand_company(f, conn, "metabase", "m", keywords=[],
                          cutoff_ts=int(time.time()) - 30 * 86400)
    assert r["seen"] == 1 and r["stale_skipped"] == 0


def test_a_dead_board_is_reported_not_raised(conn):
    """A cached token that has gone 404 must not abort the other companies."""
    f = Fetch({})
    r = LV.expand_company(f, conn, "metabase", "gone", keywords=[])
    assert r["ended_reason"] == "http_404" and r["seen"] == 0


def test_expanded_jobs_record_the_role_as_provenance(conn):
    f = Fetch({"m": [LV_JOB]})
    LV.expand_company(f, conn, "metabase", "m", keywords=[],
                      role="machine-learning-engineer")
    assert conn.execute("SELECT found_via_roles FROM jobs").fetchone()[0] == \
        "machine-learning-engineer"

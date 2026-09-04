"""Workable: company-scoped expansion, honest 404s, descriptions for free.

No network. Payloads match the live shape verified 2026-09-05 against
`apply.workable.com/api/v1/widget/accounts/lawnstarter`.
"""

from __future__ import annotations

import json

import pytest

from src import assertions as A
from src import ats as ATS
from src import store as S
from src.sources import workable as WK

# Field-for-field the live LawnStarter payload, with the description shortened.
WK_JOB = {
    "title": "Machine Learning Engineer",
    "shortcode": "09F2452C5D",
    "code": None,
    "employment_type": "Full-time",
    "telecommuting": True,
    "department": "Analytics",
    "function": "Data Analyst",
    "industry": "Consumer Services",
    "experience": "Mid-Senior level",
    "education": None,
    "url": "https://apply.workable.com/j/09F2452C5D",
    "shortlink": "https://apply.workable.com/j/09F2452C5D",
    "application_url": "https://apply.workable.com/j/09F2452C5D/apply",
    "published_on": "2026-09-02",
    "created_at": "2026-09-02",
    "country": "Brazil",
    "city": "",
    "state": "",
    "locations": [{"country": "Brazil", "countryCode": "BR",
                   "city": None, "region": None, "hidden": False}],
    # Plain HTML, NOT entity-encoded -- the opposite of Greenhouse.
    "description": "<h3>About us</h3><p>Build <b>models</b> &amp; ship them.</p>",
}


def _payload(jobs, name="LawnStarter"):
    return json.dumps({"name": name, "description": None, "jobs": jobs})


class Fetch:
    """Serves a board for tokens in `boards`, an honest 404 otherwise -- which
    is what Workable actually does for a nonexistent account."""

    listing_ttl = None

    def __init__(self, boards: dict[str, list], names: dict | None = None):
        self.boards = boards
        self.names = names or {}
        self.urls: list[str] = []

    def get(self, url, refresh=False, max_age=None):
        self.urls.append(url)
        token = url.split("/accounts/")[1].split("?")[0]
        outer = self

        class R:
            status = 200 if token in outer.boards else 404
            ok = token in outer.boards
            text = _payload(outer.boards.get(token, []),
                            outer.names.get(token, "LawnStarter"))
        return R()


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


# --- parsing ------------------------------------------------------------------


def test_description_is_stripped_once_not_unescaped_first():
    """Workable sends plain HTML. Greenhouse's unescape-then-strip would eat a
    literal &lt; the employer actually typed."""
    got = WK.clean_content(WK_JOB["description"])
    assert "<h3>" not in got and "<b>" not in got
    assert "About us" in got and "Build" in got
    assert "& ship them" in got, "entities in the text should still resolve"


def test_clean_content_preserves_a_literal_escaped_tag():
    """The case that separates a single strip from Greenhouse's double decode:
    an employer writing about a <div> in prose."""
    got = WK.clean_content("<p>Use the &lt;div&gt; element</p>")
    assert got == "Use the <div> element"


def test_location_prefers_structured_locations_over_empty_flat_fields():
    """city/state are '' rather than null on the live payload, so the flat
    fallback must not win over the structured list."""
    assert WK.location_of(WK_JOB) == "Brazil"
    assert WK.location_of({"locations": [
        {"city": "Pune", "region": "MH", "country": "India"}]}) == "Pune, MH, India"
    assert WK.location_of({"city": "Austin", "state": "TX",
                           "country": "USA", "locations": []}) == "Austin, TX, USA"
    assert WK.location_of({"locations": [], "city": "", "state": ""}) is None


def test_hidden_locations_are_skipped():
    assert WK.location_of({"locations": [
        {"city": "Secret", "hidden": True},
        {"city": "Berlin", "hidden": False}]}) == "Berlin"


def test_native_id_is_namespaced_by_board():
    assert WK.native_id("lawnstarter", WK_JOB) == "lawnstarter/09F2452C5D"
    assert WK.native_id("acme", {}) is None


def test_parse_raises_on_shape_change():
    with pytest.raises(A.SchemaDrift, match="expected an object with 'jobs'"):
        WK.parse(json.dumps({"results": []}), "u")
    with pytest.raises(A.SchemaDrift, match="not a list"):
        WK.parse(json.dumps({"jobs": 5}), "u")
    with pytest.raises(A.SchemaDrift, match="not JSON"):
        WK.parse("<html>", "u")


def test_board_url_only_ever_touches_the_permitted_origin():
    """www.workable.com DISALLOWS /j/ while apply.workable.com allows
    everything. Building a URL on the wrong host would be a conduct breach that
    robots.txt would then correctly refuse."""
    for u in (WK.board_url("acme"), WK.board_url("acme", content=False)):
        assert u.startswith("https://apply.workable.com/")
        assert "www.workable.com" not in u


def test_details_flag_controls_the_expensive_payload():
    """Resolution probes the cheap summary; only expansion pays for 635 KB."""
    assert WK.board_url("acme", content=False).endswith("/acme")
    assert WK.board_url("acme", content=True).endswith("?details=true")


# --- token resolution ---------------------------------------------------------


def test_missing_board_is_an_honest_404(conn):
    """Unlike Ashby, which serves 200 with a nulled payload, Workable 404s. So
    a miss is recorded as a miss and needs no BoardNotFound equivalent."""
    f = Fetch({})
    rec = ATS.resolve(f, conn, "nope", "Nope", "workable")
    assert rec["resolved"] == 0 and rec["board_token"] is None


def test_empty_board_is_a_resolution_not_a_miss(conn):
    """optisigns returns {"name":"OptiSigns","jobs":[]} -- the board exists and
    the company simply has nothing open. Reading that as a miss would re-probe
    six candidates on every future run."""
    f = Fetch({"optisigns": []}, names={"optisigns": "OptiSigns"})
    rec = ATS.resolve(f, conn, "optisigns", "OptiSigns", "workable")
    assert rec["resolved"] == 1 and rec["n_jobs"] == 0


def test_a_colliding_token_is_rejected_by_account_name(conn):
    """resolve() binds the FIRST token that answers 200. Without the name check
    'acme' would silently attach Globex's entire board to Acme."""
    f = Fetch({"acme": [WK_JOB]}, names={"acme": "Globex Industries"})
    rec = ATS.resolve(f, conn, "acme", "Acme", "workable")
    assert rec["resolved"] == 0, "another company's board was bound to ours"
    assert "Globex" in rec["attempts"]


def test_a_matching_account_name_is_accepted(conn):
    f = Fetch({"lawnstarter": [WK_JOB]}, names={"lawnstarter": "LawnStarter"})
    rec = ATS.resolve(f, conn, "lawnstarter", "LawnStarter", "workable")
    assert rec["resolved"] == 1 and rec["board_token"] == "lawnstarter"


def test_verify_account_accepts_when_there_is_nothing_to_compare():
    """The guard exists to catch a wrong match, not to reject an unverifiable
    one -- a company with no recorded name must still resolve."""
    assert WK.verify_account({"name": "Anything"}, None) is True
    assert WK.verify_account({}, "Acme") is True
    assert WK.verify_account({"name": "Acme, Inc."}, "Acme") is True


# --- expansion ----------------------------------------------------------------


def test_expand_applies_the_local_role_filter(conn):
    off = dict(WK_JOB, shortcode="X2", title="Office Manager",
               department="Operations", function="Administrative")
    f = Fetch({"acme": [WK_JOB, off]})
    r = WK.expand_company(f, conn, "acme-co", "acme",
                          keywords=["machine learning"])
    assert r["seen"] == 1 and r["filtered_out"] == 1
    assert r["filter_mode"] == "local"
    assert [x[0] for x in conn.execute("SELECT title FROM jobs")] == \
        ["Machine Learning Engineer"]


def test_the_whole_board_costs_one_request(conn):
    """The headline property: descriptions come inline, so unlike Ashby there
    is no per-posting detail fetch."""
    f = Fetch({"acme": [WK_JOB, dict(WK_JOB, shortcode="B"),
                        dict(WK_JOB, shortcode="C")]})
    WK.expand_company(f, conn, "acme-co", "acme", keywords=[])
    assert len(f.urls) == 1, f"expected 1 request, made {len(f.urls)}"
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 3


def test_view_maps_every_field(conn):
    f = Fetch({"acme": [WK_JOB]})
    WK.expand_company(f, conn, "acme-co", "acme", keywords=[])
    r = conn.execute(
        "SELECT title, company, posted_ts, posted_at, description, apply_url, "
        "ats_source, job_type, remote, remote_label, location_raw, badges "
        "FROM jobs").fetchone()
    assert r["title"] == "Machine Learning Engineer"
    assert r["company"] == "LawnStarter"
    # published_on is a bare YYYY-MM-DD, so the unknown time of day resolves to
    # the end of that day (ADR-006). Was 1788307200 (midnight) before that fix.
    assert r["posted_ts"] == 1788393599      # 2026-09-02T23:59:59Z
    assert r["posted_at"].startswith("2026-09-02")
    assert "<h3>" not in (r["description"] or "")
    assert r["apply_url"] == "https://apply.workable.com/j/09F2452C5D/apply"
    assert "Workable" in r["ats_source"]
    assert r["job_type"] == "Full-time"
    assert r["location_raw"] == "Brazil"
    assert r["badges"] == "Analytics, Data Analyst"


def test_badges_dedupe_department_against_function(conn):
    """Live: Sweep's board sets both to "Engineering", which rendered the pill
    as "Engineering, Engineering"."""
    same = dict(WK_JOB, department="Engineering", function="Engineering")
    f = Fetch({"acme": [same]})
    WK.expand_company(f, conn, "acme-co", "acme", keywords=[])
    assert conn.execute("SELECT badges FROM jobs").fetchone()["badges"] == "Engineering"


def test_remote_label_comes_from_a_real_boolean(conn):
    """telecommuting is explicit, so Workable can say Onsite honestly where
    Greenhouse has to leave it NULL."""
    onsite = dict(WK_JOB, shortcode="ON", telecommuting=False)
    f = Fetch({"acme": [WK_JOB, onsite]})
    WK.expand_company(f, conn, "acme-co", "acme", keywords=[])
    got = dict(conn.execute(
        "SELECT native_id, remote_label FROM jobs").fetchall())
    assert got["acme/09F2452C5D"] == "Remote"
    assert got["acme/ON"] == "Onsite"


def test_salary_is_null_not_guessed(conn):
    """Workable publishes no salary at all."""
    f = Fetch({"acme": [WK_JOB]})
    WK.expand_company(f, conn, "acme-co", "acme", keywords=[])
    r = conn.execute("SELECT salary_raw, salary_min, salary_currency, "
                     "salary_period FROM jobs").fetchone()
    assert all(r[k] is None for k in
               ("salary_raw", "salary_min", "salary_currency", "salary_period"))


def test_stale_jobs_are_dropped_and_counted(conn):
    import datetime as dt
    import time

    recent = dict(WK_JOB, shortcode="R",
                  published_on=(dt.datetime.now(dt.UTC)
                                - dt.timedelta(days=2)).date().isoformat())
    old = dict(WK_JOB, shortcode="O", published_on="2020-01-01")
    f = Fetch({"acme": [recent, old]})
    r = WK.expand_company(f, conn, "acme-co", "acme", keywords=[],
                          cutoff_ts=int(time.time()) - 30 * 86400)
    assert r["stale_skipped"] == 1 and r["seen"] == 1


def test_unparseable_date_is_kept_not_dropped(conn):
    """Unknown age is not old (ADR-006)."""
    import time

    bad = dict(WK_JOB, shortcode="B", published_on="not-a-date", created_at=None)
    f = Fetch({"acme": [bad]})
    r = WK.expand_company(f, conn, "acme-co", "acme", keywords=[],
                          cutoff_ts=int(time.time()) - 30 * 86400)
    assert r["seen"] == 1 and r["stale_skipped"] == 0


def test_a_dead_board_is_reported_not_raised(conn):
    """A cached token that has gone 404 must not abort the remaining companies
    -- the failure mode that Ashby's silent 200 caused once already."""
    f = Fetch({})
    r = WK.expand_company(f, conn, "acme-co", "gone", keywords=[])
    assert r["ended_reason"] == "http_404" and r["seen"] == 0


def test_expanded_jobs_record_the_role_as_provenance(conn):
    f = Fetch({"acme": [WK_JOB]})
    WK.expand_company(f, conn, "acme-co", "acme", keywords=[],
                      role="machine-learning-engineer")
    r = conn.execute("SELECT found_via_roles FROM jobs").fetchone()
    assert r["found_via_roles"] == "machine-learning-engineer"


def test_one_posting_per_location_is_collapsed_to_one_job(conn):
    """Workable emits ONE ENTRY PER LOCATION, all sharing a shortcode.

    Found live: lawnstarter's board returns 55 entries carrying only 10 distinct
    shortcodes. Counting entries would inflate every telemetry number 5.5x, and
    letting the last entry win would silently discard the other locations.
    """
    variants = [dict(WK_JOB, locations=[{"country": c}]) for c in
                ("Brazil", "Mexico", "Colombia")]
    f = Fetch({"acme": variants})
    r = WK.expand_company(f, conn, "acme-co", "acme", keywords=[])
    assert r["seen"] == 1, f"counted {r['seen']} for one posting in 3 locations"
    assert r["claimed"] == 1, "claimed should count postings, not location rows"
    rows = conn.execute("SELECT native_id, location_raw FROM jobs").fetchall()
    assert len(rows) == 1
    assert rows[0]["location_raw"] == "Brazil; Mexico; Colombia", \
        "locations from the other entries were dropped"


def test_a_date_only_posting_is_dated_utc_not_local(conn):
    """`published_on` is a bare YYYY-MM-DD. `datetime.fromisoformat` returns a
    NAIVE datetime and `.timestamp()` then reads it in the machine's local zone,
    so the same board goes stale at different moments in different timezones.
    The `jobs` view uses strftime, which is UTC -- ingest must agree."""
    assert WK.posted_ts_of({"published_on": "2026-08-06"}) == 1786060799, \
        "date-only timestamps must be resolved in UTC"


def test_the_view_dates_a_posting_exactly_as_ingest_does(conn):
    """The invariant. Ingest decides what to KEEP and the view decides what to
    SHOW; if they disagree, a job is stored and then immediately hidden by the
    UI's age filter and nothing explains why."""
    f = Fetch({"acme": [WK_JOB]})
    WK.expand_company(f, conn, "acme-co", "acme", keywords=[])
    in_view = conn.execute("SELECT posted_ts FROM jobs").fetchone()["posted_ts"]
    assert in_view == WK.posted_ts_of(WK_JOB)


def test_a_posting_from_the_cutoff_day_is_kept(conn):
    """Unknown age is not old (ADR-006), and a bare date has an unknown time of
    day. Reading it as midnight makes a job posted that afternoon look up to 24 h
    older than it is -- which is what dropped Sweep's Machine Learning Engineer,
    the single most relevant posting in the live run."""
    import time

    cutoff = int(time.time()) - 30 * 86400
    on_the_day = dict(WK_JOB, shortcode="EDGE",
                      published_on=__import__("datetime").date.fromtimestamp(
                          cutoff).isoformat())
    f = Fetch({"acme": [on_the_day]})
    r = WK.expand_company(f, conn, "acme-co", "acme", keywords=[],
                          cutoff_ts=cutoff)
    assert r["seen"] == 1 and r["stale_skipped"] == 0


def test_expanded_jobs_join_the_existing_company(conn):
    f = Fetch({"acme": [WK_JOB]})
    WK.expand_company(f, conn, "acme-co", "acme", keywords=[])
    r = conn.execute("SELECT company_slug, source FROM jobs").fetchone()
    assert r["company_slug"] == "acme-co" and r["source"] == "workable"

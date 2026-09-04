"""Ashby: allowed hosted-board expansion and posting-detail mapping.

No network. Payloads match the live HTML shapes verified 2026-09-04.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from src import assertions as A
from src import store as S
from src.sources import ashby as ASH


BOARD_JOB = {
    "id": "3b9b0141-86e7-46b2-b6b4-ed039b84cdc4",
    "title": "Senior Software Engineer, Product Engineering - EU",
    "teamId": "team-eng",
    "locationId": "remote-eu",
    "locationName": "Remote - European Union",
    "workplaceType": "Remote",
    "employmentType": "FullTime",
    "secondaryLocations": [
        {"locationId": "spain", "locationName": "Spain"},
        {"locationId": "germany", "locationName": "Germany"},
    ],
    "compensationTierSummary": "€108K – €145K • Offers Equity",
}

DETAIL = {
    "@context": "https://schema.org/",
    "@type": "JobPosting",
    "title": BOARD_JOB["title"],
    "description": "<p>Build <strong>reliable systems</strong>.</p>",
    "datePosted": "2026-08-31",
    "employmentType": "FULL_TIME",
    "jobLocationType": "TELECOMMUTE",
    "applicantLocationRequirements": [
        {"@type": "Country", "name": "Spain"},
        {"@type": "Country", "name": "Germany"},
    ],
    "baseSalary": {
        "@type": "MonetaryAmount",
        "currency": "EUR",
        "value": {
            "@type": "QuantitativeValue",
            "minValue": 108000,
            "maxValue": 145000,
            "unitText": "YEAR",
        },
    },
}


def _page(app_data: dict, linked_data: dict | None = None) -> str:
    ld = (f'<script type="application/ld+json">{json.dumps(linked_data)}</script>'
          if linked_data else "")
    return ("<html><head>" + ld + "</head><body><script>\n"
            "window.__appData = " + json.dumps(app_data) + ";\n"
            "window.after = true;</script></body></html>")


def _board(jobs: list[dict]) -> str:
    return _page({
        "organization": {"name": "Ashby", "hostedJobsPageSlug": "Ashby"},
        "posting": None,
        "jobBoard": {
            "teams": [{"id": "team-eng", "name": "Engineering",
                       "externalName": None, "parentTeamId": None}],
            "jobPostings": jobs,
        },
    })


def _detail(linked_data: dict = DETAIL, workplace_type: str = "Remote") -> str:
    return _page({
        "organization": {"name": "Ashby", "hostedJobsPageSlug": "Ashby"},
        "posting": {
            "id": BOARD_JOB["id"], "title": BOARD_JOB["title"],
            "departmentName": "Engineering", "teamNames": ["Engineering"],
            "locationName": "Remote - European Union",
            "secondaryLocationNames": ["Spain", "Germany"],
            "workplaceType": workplace_type, "employmentType": "FullTime",
            "descriptionHtml": DETAIL["description"], "isListed": True,
        },
        "jobBoard": None,
    }, linked_data)


class Fetch:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.urls: list[str] = []

    # Listings carry a freshness window; the fake ignores it but must
    # expose it, because production reads fetcher.listing_ttl.
    listing_ttl = None

    def get(self, url, refresh=False, max_age=None):
        self.urls.append(url)
        outer = self

        class R:
            status = 200 if url in outer.pages else 404
            ok = url in outer.pages
            text = outer.pages.get(url, "not found")

        return R()


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


def test_parse_board_extracts_app_data_and_rejects_silent_200_not_found():
    got = ASH.parse(_board([BOARD_JOB]), "u")
    assert got["organization"]["name"] == "Ashby"
    assert got["jobs"][0]["id"] == BOARD_JOB["id"]

    # A nulled payload means the board does not exist, NOT that the shape moved.
    # Changed deliberately: this previously asserted SchemaDrift, which made a
    # dead cached board token abort the whole multi-company run.
    with pytest.raises(ASH.BoardNotFound):
        ASH.parse(_page({"organization": None, "jobBoard": None}), "u")
    with pytest.raises(A.SchemaDrift, match="__appData"):
        ASH.parse("<html>not a board</html>", "u")


def test_parse_detail_extracts_jobposting_json_ld():
    got = ASH.parse_detail(_detail(), "u")
    assert got["posting"]["teamNames"] == ["Engineering"]
    assert got["linked_data"]["datePosted"] == "2026-08-31"
    assert got["linked_data"]["baseSalary"]["currency"] == "EUR"


def test_board_and_detail_urls_use_the_allowed_hosted_pages():
    assert ASH.board_url("Ashby") == "https://jobs.ashbyhq.com/Ashby"
    assert ASH.detail_url("Ashby", BOARD_JOB["id"]).endswith(BOARD_JOB["id"])
    assert "/api/" not in ASH.board_url("Ashby")


def test_expand_filters_before_fetching_details_and_maps_fields(conn):
    off = dict(BOARD_JOB, id="office", title="Office Manager", teamId="team-ops")
    board_url = ASH.board_url("Ashby")
    detail_url = ASH.detail_url("Ashby", BOARD_JOB["id"])
    f = Fetch({board_url: _board([BOARD_JOB, off]), detail_url: _detail()})

    r = ASH.expand_company(f, conn, "ashbyhq", "Ashby",
                           keywords=["software engineer"], role="software-engineer")

    assert r["seen"] == 1 and r["filtered_out"] == 1
    assert ASH.detail_url("Ashby", "office") not in f.urls
    job = conn.execute(
        "SELECT title,company,badges,location_raw,remote_locations,remote,"
        "remote_label,job_type,salary_raw,salary_min,salary_max,salary_currency,"
        "salary_period,description,posted_at,apply_url,found_via_roles FROM jobs"
    ).fetchone()
    assert job["company"] == "Ashby" and job["badges"] == "Engineering"
    assert job["location_raw"] == "Remote - European Union"
    assert job["remote_locations"] == "Spain, Germany"
    assert job["remote"] == 1 and job["remote_label"] == "Remote"
    assert job["job_type"] == "FullTime"
    assert (job["salary_min"], job["salary_max"], job["salary_currency"],
            job["salary_period"]) == (108000, 145000, "EUR", "annual")
    assert "108000" in job["salary_raw"] and "YEAR" in job["salary_raw"]
    assert job["description"] == "Build reliable systems."
    assert job["posted_at"].startswith("2026-08-31")
    assert job["apply_url"] == detail_url
    assert job["found_via_roles"] == "software-engineer"


def test_only_explicit_annual_salary_is_numeric(conn):
    hourly = json.loads(json.dumps(DETAIL))
    hourly["baseSalary"]["value"]["unitText"] = "HOUR"
    board_url = ASH.board_url("Ashby")
    detail_url = ASH.detail_url("Ashby", BOARD_JOB["id"])
    f = Fetch({board_url: _board([BOARD_JOB]), detail_url: _detail(hourly)})
    ASH.expand_company(f, conn, "ashbyhq", "Ashby", keywords=[])
    job = conn.execute("SELECT salary_min,salary_max,salary_period FROM jobs").fetchone()
    assert job["salary_min"] is None and job["salary_max"] is None
    assert job["salary_period"] == "hourly"


def test_hybrid_maps_to_the_canonical_onsite_or_remote_label(conn):
    hybrid_job = dict(BOARD_JOB, workplaceType="Hybrid")
    board_url = ASH.board_url("Ashby")
    detail_url = ASH.detail_url("Ashby", BOARD_JOB["id"])
    f = Fetch({board_url: _board([hybrid_job]),
               detail_url: _detail(workplace_type="Hybrid")})
    ASH.expand_company(f, conn, "ashbyhq", "Ashby", keywords=[])
    job = conn.execute("SELECT remote,remote_config,remote_label FROM jobs").fetchone()
    assert (job["remote"], job["remote_config"], job["remote_label"]) == \
        (1, "HYBRID", "Onsite or Remote")


def test_stale_posting_is_dropped_after_detail_fetch(conn):
    stale = dict(DETAIL, datePosted="2020-01-01")
    board_url = ASH.board_url("Ashby")
    detail_url = ASH.detail_url("Ashby", BOARD_JOB["id"])
    f = Fetch({board_url: _board([BOARD_JOB]), detail_url: _detail(stale)})
    cutoff = int(dt.datetime(2026, 8, 1, tzinfo=dt.UTC).timestamp())
    r = ASH.expand_company(f, conn, "ashbyhq", "Ashby", keywords=[], cutoff_ts=cutoff)
    assert r["seen"] == 0 and r["stale_skipped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# --- a board that no longer exists is not schema drift -------------------------

NOT_FOUND_APP_DATA = (
    "<html><body><script>window.__appData = "
    '{"organization": null, "jobBoard": null, "posting": null}'
    "</script></body></html>"
)


def test_missing_board_is_reported_not_raised():
    """Ashby serves HTTP 200 for a board that does not exist, with
    `organization` and `jobBoard` both null (verified live 2026-09-04).

    `company_ats` caches board tokens, and boards get renamed or deleted -- the
    Phase 1 probe already recorded BlueVine's Greenhouse board disappearing. So
    a cached token WILL go dead. Raising SchemaDrift there aborts the whole
    multi-company run; Greenhouse does not have this problem because a dead
    board 404s. This must be an ordinary per-company miss.
    """
    from src import store as S
    from src.sources import ashby as AB

    class F:
        # Listings carry a freshness window; the fake ignores it but must
        # expose it, because production reads fetcher.listing_ttl.
        listing_ttl = None

        def get(self, url, refresh=False, max_age=None):
            class R:
                ok, status, text = True, 200, NOT_FOUND_APP_DATA
            return R()

    import tempfile
    import pathlib

    conn = S.connect(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    r = AB.expand_company(F(), conn, "acme", "gone-board", keywords=[], role="r")
    assert r["ended_reason"] == "board_not_found"
    assert r["seen"] == 0


def test_real_schema_drift_still_raises():
    """The guard must survive the fix: a page with no __appData at all, or one
    whose jobPostings is the wrong type, is genuine drift."""
    from src import assertions as A
    from src.sources import ashby as AB

    with pytest.raises(A.SchemaDrift, match="window.__appData"):
        AB.parse("<html><body>nothing here</body></html>", "u")

    bad = ('<html><script>window.__appData = '
           '{"organization": {"name": "A"}, "jobBoard": {"jobPostings": 5, "teams": []}}'
           "</script></html>")
    with pytest.raises(A.SchemaDrift, match="jobPostings"):
        AB.parse(bad, "u")

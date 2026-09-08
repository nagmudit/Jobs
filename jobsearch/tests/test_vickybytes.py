"""vickybytes: one JSON endpoint, filtered locally.

Fixtures are shaped from the real feed sampled 2026-09-08, because the two
things most likely to break this source are both things the sample taught us and
a made-up fixture would not:

  * `type` is the WORK MODE (onsite / hybrid / remote), plus `job` where the
    poster never said. An earlier plan filtered on `type == 'job'`, which would
    have discarded 88% of the feed.
  * `location` frequently carries a trailing space ('Gurugram '), and empty
    strings appear where other sources send null.

No network: `Feed` stands in at the boundary.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from src import sources as SRC
from src import store as S
from src.sources import vickybytes as VB

DAY = 86400


def _ts(days_ago: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)).isoformat()


def _entry(**over):
    """One opportunity, shaped from a real record."""
    j = {
        "id": 6363,
        "fullName": "Speechify",
        "title": "Software Engineer, iOS Core Product",
        "contentLink": "https://job-boards.greenhouse.io/speechify/jobs/5981040004",
        "apply_method": "link",
        "application_form": None,
        "imageLink": "/fallback/job3.png",
        "timestamp": _ts(3),
        "type": "remote",
        "tags": ["full-time", "remote", "ios"],
        "location": "Bengaluru, India ",
        "description": "Senior-level iOS product experience.",
        "shortDescription": "High-growth iOS role.",
        "category": "High-growth",
        "yoe": "5+",
        "salaryRange": "$150K",
        "is_live": True,
    }
    j.update(over)
    return j


class Feed:
    """Serves one canned payload. Never touches the network."""

    listing_ttl = None

    def __init__(self, entries, status=200):
        self.payload = json.dumps(entries)
        self.status = status
        self.urls: list[str] = []

    def get(self, url, refresh=False, max_age=None):
        self.urls.append(url)
        outer = self

        class R:
            status = outer.status
            ok = outer.status == 200
            text = outer.payload
        return R()


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


def _rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM jobs ORDER BY source_job_id")]


# --- the contract -------------------------------------------------------------


def test_core_view_matches_core_columns_positionally(conn):
    """UNION ALL aligns by position; a drifted view silently serves another
    column's values."""
    SRC.assert_core_views(conn)


def test_ids_are_namespaced(conn):
    VB.ingest(Feed([_entry()]), conn, {})
    assert _rows(conn)[0]["source_job_id"] == "vickybytes:6363"
    assert _rows(conn)[0]["source"] == "vickybytes"


# --- what gets kept -----------------------------------------------------------


def test_dead_listings_are_skipped(conn):
    r = VB.ingest(Feed([_entry(id=1, is_live=False), _entry(id=2)]), conn, {})
    assert r["seen"] == 1 and r["filtered_out"] == 1
    assert [x["native_id"] for x in _rows(conn)] == ["2"]


def test_postings_with_no_apply_link_are_skipped(conn):
    """The two real `apply_method: form` entries carry contentLink: null.
    Storing one means a row whose Apply button goes nowhere."""
    r = VB.ingest(Feed([
        _entry(id=1, apply_method="form", contentLink=None),
        _entry(id=2, contentLink="   "),
        _entry(id=3),
    ]), conn, {})
    assert r["seen"] == 1 and r["filtered_out"] == 2
    assert [x["native_id"] for x in _rows(conn)] == ["3"]


def test_every_work_mode_is_kept(conn):
    """`type` is the work mode, not the kind of posting. Filtering on
    type == 'job' would have dropped 88% of the real feed."""
    entries = [_entry(id=i, type=t) for i, t in
               enumerate(["onsite", "hybrid", "remote", "job"], start=1)]
    r = VB.ingest(Feed(entries), conn, {})
    assert r["seen"] == 4, "a work mode was mistaken for a posting kind"


def test_stale_postings_are_dropped_at_the_cutoff(conn):
    cutoff = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).timestamp())
    r = VB.ingest(Feed([_entry(id=1, timestamp=_ts(200)),
                        _entry(id=2, timestamp=_ts(3))]), conn, {}, cutoff_ts=cutoff)
    assert r["stale_skipped"] == 1 and r["seen"] == 1


def test_an_unparseable_date_is_kept_not_dropped(conn):
    """ADR-006: unknown age is not old. Dropping it would lose data on a schema
    change, silently."""
    cutoff = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).timestamp())
    r = VB.ingest(Feed([_entry(id=1, timestamp="not-a-date")]), conn, {},
                  cutoff_ts=cutoff)
    assert r["seen"] == 1 and r["stale_skipped"] == 0
    assert _rows(conn)[0]["posted_ts"] is None


def test_the_role_filter_is_applied_locally(conn):
    """The endpoint takes no query parameters, so there is nothing to filter
    server-side (ADR-009)."""
    r = VB.ingest(Feed([_entry(id=1, title="Chef de Partie", tags=["kitchen"]),
                        _entry(id=2, title="Backend Engineer", tags=["python"])]),
                  conn, {"role": "backend-engineer", "keywords": ["backend"]})
    assert r["filter_mode"] == "local"
    assert r["filtered_out"] == 1
    assert [x["native_id"] for x in _rows(conn)] == ["2"]


def test_tags_count_toward_the_role_match(conn):
    """Unlike RemoteOK's crowd-applied tags, these are curated per posting."""
    r = VB.ingest(Feed([_entry(id=1, title="Engineer II", tags=["python", "ai"])]),
                  conn, {"role": "x", "keywords": ["python"]})
    assert r["seen"] == 1


# --- the derived view ---------------------------------------------------------


def test_work_mode_becomes_remote_columns(conn):
    for i, (t, remote, label) in enumerate([
            ("remote", 1, "Remote"),
            ("hybrid", 1, "Onsite or Remote"),
            ("onsite", 0, "Onsite")], start=1):
        VB.ingest(Feed([_entry(id=i, type=t)]), conn, {})
    got = {r["native_id"]: (r["remote"], r["remote_label"]) for r in _rows(conn)}
    assert got["1"] == (1, "Remote")
    assert got["2"] == (1, "Onsite or Remote")
    assert got["3"] == (0, "Onsite")


def test_an_unstated_work_mode_is_null_not_onsite(conn):
    """'job' means the poster never said. Calling that onsite would invent a
    fact and put the row in the wrong filter."""
    VB.ingest(Feed([_entry(type="job")]), conn, {})
    row = _rows(conn)[0]
    assert row["remote"] is None and row["remote_label"] is None


def test_trailing_whitespace_in_location_is_trimmed(conn):
    VB.ingest(Feed([_entry(location="Gurugram ")]), conn, {})
    assert _rows(conn)[0]["location_raw"] == "Gurugram"


def test_empty_strings_become_null_not_blank(conn):
    """This feed sends '' where other sources send null. A blank string would
    show as a present-but-empty value in every filter."""
    VB.ingest(Feed([_entry(salaryRange="", location="", description="",
                           shortDescription="")]), conn, {})
    row = _rows(conn)[0]
    for col in ("salary_raw", "location_raw", "description", "high_concept"):
        assert row[col] is None, f"{col} is {row[col]!r}, expected NULL"


def test_job_type_is_read_from_the_tags(conn):
    VB.ingest(Feed([_entry(id=1, tags=["internship", "python"]),
                    _entry(id=2, tags=["python", "ai"])]), conn, {})
    got = {r["native_id"]: r["job_type"] for r in _rows(conn)}
    assert got["1"] == "internship"
    assert got["2"] is None, "a skill tag was mistaken for an engagement type"


def test_salary_numerics_derive_from_the_raw_string(conn):
    """The outer view falls back to salary_min/max over salary_raw, so this
    source stores no numerics of its own."""
    VB.ingest(Feed([_entry(salaryRange="$40K-$80K")]), conn, {})
    row = _rows(conn)[0]
    assert row["salary_raw"] == "$40K-$80K"
    assert (row["salary_min"], row["salary_max"]) == (40000, 80000)


def test_an_hourly_rate_stays_raw_only(conn):
    """Hourly and annual in one column sort silently wrong (ADR-012)."""
    VB.ingest(Feed([_entry(salaryRange="$33.64-$50.46/hour")]), conn, {})
    row = _rows(conn)[0]
    assert row["salary_raw"] == "$33.64-$50.46/hour"
    assert row["salary_min"] is None and row["salary_max"] is None


def test_apply_url_is_the_content_link(conn):
    VB.ingest(Feed([_entry()]), conn, {})
    assert _rows(conn)[0]["apply_url"].startswith("https://job-boards.greenhouse.io/")


def test_ats_source_is_not_populated_from_the_apply_host(conn):
    """Deliberate: setting it would enrol every one of these companies into ATS
    expansion (ADR-010) and lengthen every fetch as a side effect."""
    VB.ingest(Feed([_entry()]), conn, {})
    assert _rows(conn)[0]["ats_source"] is None


# --- drift --------------------------------------------------------------------


def test_a_non_array_payload_raises(conn):
    from src import assertions as A

    f = Feed([])
    f.payload = json.dumps({"opportunities": []})
    with pytest.raises(A.SchemaDrift):
        VB.ingest(f, conn, {})


def test_a_payload_with_no_ids_raises(conn):
    from src import assertions as A

    f = Feed([])
    f.payload = json.dumps([{"headline": "x"}, {"headline": "y"}])
    with pytest.raises(A.SchemaDrift):
        VB.ingest(f, conn, {})


def test_an_empty_feed_is_not_an_error(conn):
    """No yield floor: an empty feed is a legitimate answer, and there is no
    claimed total to check a shortfall against."""
    r = VB.ingest(Feed([]), conn, {})
    assert r["seen"] == 0 and r["ended_reason"] == "exhausted"


def test_an_http_error_reports_rather_than_raising(conn):
    r = VB.ingest(Feed([], status=503), conn, {})
    assert r["ended_reason"] == "http_503" and r["seen"] == 0

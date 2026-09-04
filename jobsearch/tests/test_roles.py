"""Role-driven fetch across sources. No network.

The three sources filter by role very differently (ADR-009), and most of what is
worth testing here is that the difference is represented honestly rather than
papered over.
"""

from __future__ import annotations

import json

import pytest

from src import assertions as A
from src import store as S
from src.sources import himalayas, remoteok

RO_JOB = {"id": "1", "slug": "x", "position": "ML Engineer", "company": "Acme",
          "epoch": 1788372677, "description": "d", "location": "",
          "salary_min": 0, "salary_max": 0, "tags": ["ai", "engineer"],
          "url": "https://remoteOK.com/x"}
RO_LEGAL = {"legal": "API Terms of Service: Please link back ...",
            "last_updated": 1788462746}


# --- regression: an empty filtered result is not a schema change --------------


def test_legal_only_payload_is_an_empty_result_not_schema_drift():
    """RemoteOK returns ONLY the legal-notice element for a tag that matches
    nothing (verified live: ?tag=frontend, ?tag=zzz-not-a-real-tag). Treating
    that as SchemaDrift aborts an otherwise healthy ingest."""
    assert remoteok.parse(json.dumps([RO_LEGAL]), "u") == []


def test_genuine_shape_change_still_raises():
    """The guard must survive the fix: real data elements with no `position`
    still mean the job shape moved."""
    with pytest.raises(A.SchemaDrift):
        remoteok.parse(json.dumps([RO_LEGAL, {"headline": "renamed", "id": "9"}]), "u")


def test_empty_array_is_still_empty():
    assert remoteok.parse(json.dumps([]), "u") == []


# --- the local relevance filter ----------------------------------------------


def test_relevance_uses_word_boundaries_not_substrings():
    """RemoteOK's own ?tag=ml returns 98 jobs, none of them ML -- substring
    matching is exactly how that happens. 'ml' must not match 'HTML', and 'ai'
    must not match 'retail'."""
    from src.relevance import matches

    assert matches("ML Engineer", None, ["ml"]) is True
    assert matches("HTML Developer", None, ["ml"]) is False
    assert matches("AI Engineer", None, ["ai"]) is True
    assert matches("Retail Assistant", None, ["ai"]) is False


def test_relevance_matches_categories_too():
    from src.relevance import matches

    assert matches("Analyst", ["Machine-Learning"], ["machine learning"]) is True


def test_no_keywords_keeps_everything():
    """A role missing from role_map must not silently empty the corpus."""
    from src.relevance import matches

    assert matches("Anything At All", None, []) is True
    assert matches("Anything At All", None, None) is True


def test_relevance_never_reads_the_description():
    """Descriptions mention 'machine learning' in boilerplate on unrelated jobs;
    matching on them would let almost everything through."""
    from src import relevance
    import inspect

    src = inspect.getsource(relevance.matches)
    assert "description" not in src


# --- source-level local filtering --------------------------------------------


def test_remoteok_filters_off_role_jobs_and_counts_them(tmp_path):
    conn = S.connect(tmp_path / "t.db")
    payload = json.dumps([
        RO_LEGAL,
        RO_JOB,                                              # ML Engineer -> keep
        dict(RO_JOB, id="2", position="Valet Driver"),       # -> filtered out
        dict(RO_JOB, id="3", position="Kitchen Technician"), # -> filtered out
    ])

    class F:
        def get(self, url, refresh=False):
            class R:
                ok, status, text = True, 200, payload
            return R()

    r = remoteok.ingest(F(), conn, {"tag": "ai", "role": "ai-engineer",
                                    "keywords": ["machine learning", "ml"]})
    assert r["seen"] == 1
    assert r["filtered_out"] == 2
    assert r["filter_mode"] == "server+local"
    titles = [x[0] for x in conn.execute("SELECT title FROM jobs")]
    assert titles == ["ML Engineer"]


def test_remoteok_records_the_role_as_provenance_not_the_tag(tmp_path):
    """found_via_roles must be one vocabulary across sources."""
    conn = S.connect(tmp_path / "t2.db")
    payload = json.dumps([RO_LEGAL, RO_JOB])

    class F:
        def get(self, url, refresh=False):
            class R:
                ok, status, text = True, 200, payload
            return R()

    remoteok.ingest(F(), conn, {"tag": "ai", "role": "artificial-intelligence-engineer",
                                "keywords": ["ml"]})
    got = conn.execute("SELECT found_via_roles FROM jobs").fetchone()[0]
    assert got == "artificial-intelligence-engineer"


def test_himalayas_filters_off_role_jobs_and_counts_them(tmp_path):
    conn = S.connect(tmp_path / "t3.db")
    keep = {"title": "Machine Learning Engineer", "companyName": "Acme",
            "companySlug": "acme", "categories": ["Machine-Learning"],
            "pubDate": 1788451065, "expiryDate": 1793635064,
            "guid": "https://himalayas.app/companies/acme/jobs/ml"}
    drop = dict(keep, title="Account Executive", categories=["Sales"],
                guid="https://himalayas.app/companies/acme/jobs/ae")
    payload = json.dumps({"jobs": [keep, drop], "totalCount": 2, "offset": 0})

    class F:
        def get(self, url, refresh=False):
            class R:
                ok, status, text = True, 200, payload
            return R()

    r = himalayas.ingest(F(), conn, {"role": "machine-learning-engineer",
                                     "keywords": ["machine learning"]},
                         max_pages=1)
    assert r["seen"] == 1 and r["filtered_out"] == 1
    assert r["filter_mode"] == "local"
    assert [x[0] for x in conn.execute("SELECT title FROM jobs")] == \
        ["Machine Learning Engineer"]


def test_off_role_is_counted_separately_from_too_old(tmp_path):
    """Conflating them makes the telemetry unreadable -- the same mistake
    ADR-006 already corrected once for stale vs page-cap truncation."""
    import time

    conn = S.connect(tmp_path / "t4.db")
    old = int(time.time()) - 400 * 86400
    payload = json.dumps([
        RO_LEGAL,
        RO_JOB,                                                   # keep
        dict(RO_JOB, id="2", position="ML Engineer", epoch=old),  # too old
        dict(RO_JOB, id="3", position="Valet Driver"),            # off-role
    ])

    class F:
        def get(self, url, refresh=False):
            class R:
                ok, status, text = True, 200, payload
            return R()

    r = remoteok.ingest(F(), conn, {"tag": "ai", "role": "r", "keywords": ["ml"]},
                        cutoff_ts=int(time.time()) - 30 * 86400)
    assert (r["seen"], r["stale_skipped"], r["filtered_out"]) == (1, 1, 1)


# --- role fetch orchestration -------------------------------------------------


def test_remoteok_role_fetch_sends_no_tag(tmp_path, monkeypatch):
    """RemoteOK's tag endpoints serve an ARCHIVE -- measured 2026-09-04, median
    age 112-144 days versus 5 days for the unfiltered feed. With a 30-day
    cutoff a tag yields nothing, so role fetches must use the live feed."""
    from src import roles as R
    from src.config import Config

    cfg = Config.load()
    conn = S.connect(tmp_path / "t5.db")
    seen_urls = []

    class F:
        def get(self, url, refresh=False):
            seen_urls.append(url)

            class Resp:
                ok, status = True, 200
                text = json.dumps([RO_LEGAL, RO_JOB])
            return Resp()

    R.fetch_role(F(), conn, cfg, "artificial-intelligence-engineer",
                 sources=["remoteok"])
    assert seen_urls, "no request was made"
    assert all("tag=" not in u for u in seen_urls), \
        f"role fetch used a tag endpoint (archive): {seen_urls}"


def test_fetch_role_reports_filter_mode_per_source(tmp_path):
    """The honesty surface: a caller must be able to tell that RemoteOK and
    Himalayas were filtered locally, not by the source."""
    from src import roles as R
    from src.config import Config

    cfg = Config.load()
    conn = S.connect(tmp_path / "t6.db")

    class F:
        def get(self, url, refresh=False):
            class Resp:
                ok, status = True, 200
                text = json.dumps([RO_LEGAL, RO_JOB])
            return Resp()

    res = R.fetch_role(F(), conn, cfg, "artificial-intelligence-engineer",
                       sources=["remoteok"])
    assert [r["filter_mode"] for r in res] == ["local"]


def test_unknown_role_is_skipped_with_a_reason_not_crawled(tmp_path):
    """A slug not in targets.yaml must never reach Wellfound -- an invalid slug
    there silently serves an unfiltered search (ADR-003)."""
    from src import roles as R
    from src.config import Config

    cfg = Config.load()
    conn = S.connect(tmp_path / "t7.db")

    class F:
        def get(self, url, refresh=False):
            raise AssertionError(f"should not have fetched {url}")

    res = R.fetch_role(F(), conn, cfg, "not-a-real-role", sources=["wellfound"])
    assert res[0]["filter_mode"] == "skipped"
    assert "targets.yaml" in res[0]["reason"]


def test_describe_labels_every_source_with_its_filter_mode():
    from src.roles import describe

    out = describe([
        {"source": "wellfound", "filter_mode": "server", "seen": 10, "new": 3,
         "filtered_out": 0, "stale_skipped": 1},
        {"source": "remoteok", "filter_mode": "local", "seen": 2, "new": 2,
         "filtered_out": 90, "stale_skipped": 8},
        {"source": "himalayas", "filter_mode": "skipped", "reason": "no keywords",
         "seen": 0, "new": 0, "filtered_out": 0, "stale_skipped": 0},
    ])
    assert "[server]" in out and "[local]" in out and "skipped" in out
    assert "90 off-role" in out

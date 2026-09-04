"""Age cutoff: not ingested, not enriched, not shown. No network."""

from __future__ import annotations

import time

import pytest

from src import store as S
from src.config import Config
from src.crawl import crawl_slice
from src.web.app import Filters, _where

from . import fixtures as F

DAY = 86400


def _ts(days_ago: int) -> int:
    return int(time.time()) - days_ago * DAY


# --- config -------------------------------------------------------------------


def test_default_is_30_days_when_key_absent(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("roles: [x]\nlocations: [anywhere]\n", encoding="utf-8")
    assert Config.load(p).max_age_days == 30


def test_explicit_zero_disables_cutoff(tmp_path):
    """An absent key and an explicit 0 are different intents; 0 means 'keep
    everything', not 'use the default'."""
    p = tmp_path / "t.yaml"
    p.write_text("roles: [x]\nlocations: [a]\nmax_age_days: 0\n", encoding="utf-8")
    cfg = Config.load(p)
    assert cfg.max_age_days is None
    assert cfg.cutoff_ts() is None


def test_cli_override_beats_file(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("roles: [x]\nlocations: [a]\nmax_age_days: 30\n", encoding="utf-8")
    assert Config.load(p, max_age_days=90).max_age_days == 90
    assert Config.load(p, max_age_days=0).max_age_days is None


def test_cutoff_ts_matches_max_age_days(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("roles: [x]\nlocations: [a]\nmax_age_days: 30\n", encoding="utf-8")
    cutoff = Config.load(p).cutoff_ts()
    assert abs((int(time.time()) - cutoff) - 30 * DAY) < 5


# --- ingest -------------------------------------------------------------------


class FakeFetcher:
    """Serves canned search pages. Never touches the network."""

    def __init__(self, pages: dict[int, str]):
        self.pages = pages
        self.urls: list[str] = []

    # Listings carry a freshness window; the fake ignores it but must
    # expose it, because production reads fetcher.listing_ttl.
    listing_ttl = None

    def get(self, url, refresh=False, max_age=None):
        self.urls.append(url)
        page = 1
        if "page=" in url:
            page = int(url.split("page=")[1])

        class R:
            ok = True
            status = 200
        R.text = self.pages.get(page, F.page(n_companies=0, jobs_per_company=0))
        return R


def _mixed_page(page_no: int, ages: list[int]) -> str:
    """One company, one job per age given."""
    import json as _json

    html = F.page(page_no=page_no, n_companies=1, jobs_per_company=len(ages))
    raw = html.split('">', 1)[1].rsplit("</script>", 1)[0]
    nd = _json.loads(raw)
    store = nd["props"]["pageProps"]["apolloState"]["data"]
    jobs = [k for k in store if k.startswith("JobListingSearchResult:")]
    for key, age in zip(sorted(jobs), ages):
        store[key]["liveStartAt"] = _ts(age)
    return (
        "<!doctype html><html><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{_json.dumps(nd)}</script>'
        "</body></html>"
    )


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


def test_stale_jobs_are_not_ingested(conn):
    f = FakeFetcher({1: _mixed_page(1, [2, 5, 400, 900])})
    res = crawl_slice(f, conn, "ai-engineer", "anywhere", max_pages=1,
                      cutoff_ts=_ts(30))
    assert res.jobs_stale_skipped == 2
    assert conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == 2
    ages = [r[0] for r in conn.execute("SELECT days_old FROM jobs")]
    assert all(a <= 30 for a in ages)


def test_no_cutoff_keeps_everything(conn):
    f = FakeFetcher({1: _mixed_page(1, [2, 5, 400, 900])})
    res = crawl_slice(f, conn, "ai-engineer", "anywhere", max_pages=1, cutoff_ts=None)
    assert res.jobs_stale_skipped == 0
    assert conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == 4


def test_all_stale_page_does_not_end_the_slice(conn):
    """THE important one. Wellfound does not order results by date -- measured
    2026-09-04, page 20 of a slice still held a 3-day-old job. A page of only
    stale jobs must therefore keep the crawl going, or fresh jobs deeper in the
    results are silently lost."""
    f = FakeFetcher({
        1: _mixed_page(1, [500, 600]),   # entirely stale
        2: _mixed_page(2, [1, 2]),       # fresh, and only reachable if we continue
    })
    res = crawl_slice(f, conn, "ai-engineer", "anywhere", max_pages=2,
                      cutoff_ts=_ts(30))
    assert res.pages_walked == 2, "slice stopped early on a stale page"
    assert conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == 2
    assert res.jobs_stale_skipped == 2


def test_company_not_stored_when_all_its_jobs_are_stale(conn):
    f = FakeFetcher({1: _mixed_page(1, [500, 600])})
    crawl_slice(f, conn, "ai-engineer", "anywhere", max_pages=1, cutoff_ts=_ts(30))
    assert conn.execute("SELECT COUNT(*) FROM company_raw").fetchone()[0] == 0


def test_job_without_timestamp_is_kept(conn):
    """Unknown age is not the same as old. Dropping it would silently lose data
    on a schema change."""
    import json as _json

    html = _mixed_page(1, [500])
    raw = html.split('">', 1)[1].rsplit("</script>", 1)[0]
    nd = _json.loads(raw)
    store = nd["props"]["pageProps"]["apolloState"]["data"]
    for k in store:
        if k.startswith("JobListingSearchResult:"):
            store[k]["liveStartAt"] = None
    page = ("<!doctype html><html><body><script id=\"__NEXT_DATA__\" "
            f'type="application/json">{_json.dumps(nd)}</script></body></html>')
    f = FakeFetcher({1: page})
    res = crawl_slice(f, conn, "ai-engineer", "anywhere", max_pages=1, cutoff_ts=_ts(30))
    assert res.jobs_stale_skipped == 0
    assert conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == 1


# --- display ------------------------------------------------------------------


def test_max_days_old_is_a_filter_dimension():
    where, params = _where(Filters(max_days_old=30))
    assert "days_old <= ?" in where and params == [30]


def test_age_filter_can_be_excluded_for_faceting():
    where, _ = _where(Filters(max_days_old=30, sizes=["11-50"]),
                      exclude="max_days_old")
    assert "days_old" not in where and "company_size" in where


# --- prune --------------------------------------------------------------------


def test_prune_is_dry_run_by_default(conn):
    f = FakeFetcher({1: _mixed_page(1, [2, 400])})
    crawl_slice(f, conn, "ai-engineer", "anywhere", max_pages=1, cutoff_ts=None)
    out = S.prune_stale(conn, 30)
    assert out["would_remove"] == 1 and out["removed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == 2


def test_prune_apply_removes_stale_and_their_derived_rows(conn):
    f = FakeFetcher({1: _mixed_page(1, [2, 400])})
    crawl_slice(f, conn, "ai-engineer", "anywhere", max_pages=1, cutoff_ts=None)
    S.rebuild_locations(conn)
    out = S.prune_stale(conn, 30, dry_run=False)
    assert out["removed"] == 1
    assert conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM job_provenance").fetchone()[0] == 1


def test_prune_keeps_user_state(conn):
    """A job you marked applied may reappear on a later crawl; the mark must
    outlive the row."""
    f = FakeFetcher({1: _mixed_page(1, [400])})
    crawl_slice(f, conn, "ai-engineer", "anywhere", max_pages=1, cutoff_ts=None)
    jid = conn.execute("SELECT source_job_id FROM job_raw").fetchone()[0]
    S.set_status(conn, jid, "applied")
    conn.commit()
    S.prune_stale(conn, 30, dry_run=False)
    assert conn.execute("SELECT COUNT(*) FROM user_state").fetchone()[0] == 1

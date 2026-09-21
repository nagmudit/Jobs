"""A crawled page stays "done" only as long as a cached listing stays fresh.

**The incident this exists for (2026-09-06 to 2026-09-21).** `crawl_slice` skips
any page marked `done` in `crawl_unit`, and that mark had no expiry. The daily
workflow restores the previous corpus before fetching, so every Wellfound page
arrived already marked done and **no request was ever sent again**. Wellfound
went unfetched for fifteen days while the run reported
`212 kept / 212 scanned (0 new)` -- the counts come from rows already stored, so
a frozen source and a working one printed the same line.

The marker now expires with `cache_ttl_hours` (ADR-011): the same number that
decides when a cached listing must be re-fetched, for the same reason.

No network: the fetcher is faked at the boundary, as everywhere else.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from src import store as S
from src.crawl import crawl_slice
from src.fetch import HostBlocked

from . import fixtures as F

HOUR = 3600.0


class FakeFetcher:
    """Serves canned search pages and records every request. Unlike the fake in
    test_age_cutoff, it carries a real `listing_ttl`, because that is what now
    decides whether a page counts as still done."""

    def __init__(self, pages: dict[int, str], listing_ttl: float | None = 6 * HOUR,
                 blocked: bool = False):
        self.pages = pages
        self.listing_ttl = listing_ttl
        self.blocked = blocked
        self.urls: list[str] = []

    def get(self, url, refresh=False, max_age=None):
        if self.blocked:
            raise HostBlocked("https://wellfound.com", "MitigationDetected: 403")
        self.urls.append(url)
        page_no = int(url.split("page=")[1]) if "page=" in url else 1

        class R:
            ok = True
            status = 200
        R.text = self.pages.get(page_no, F.page(n_companies=0, jobs_per_company=0))
        return R


def _pages(n: int = 2) -> dict[int, str]:
    return {i: F.page(page_no=i, n_companies=1, jobs_per_company=2) for i in range(1, n + 1)}


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


def _age_marks(conn, hours: float) -> None:
    """Backdate every page mark, as restoring yesterday's corpus effectively does."""
    when = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()
    conn.execute("UPDATE crawl_unit SET updated_at = ?", (when,))
    conn.commit()


# --------------------------------------------------------------------------- #
# The regression
# --------------------------------------------------------------------------- #

def test_yesterdays_page_marks_do_not_freeze_the_slice(conn):
    """THE one. Crawl, restore the corpus a day later, crawl again: the pages
    must be requested again. Fifteen days of Wellfound were lost to this."""
    first = FakeFetcher(_pages())
    crawl_slice(first, conn, "ai-engineer", "anywhere", max_pages=2)
    assert len(first.urls) == 2

    _age_marks(conn, hours=24)
    before = conn.execute("SELECT MAX(last_seen) FROM job_raw").fetchone()[0]
    time.sleep(0.01)

    second = FakeFetcher(_pages())
    res = crawl_slice(second, conn, "ai-engineer", "anywhere", max_pages=2)

    assert len(second.urls) == 2, "pages were skipped; the source is frozen"
    assert res.pages_fetched == 2
    assert res.jobs_new == 0                      # same jobs, not new ones
    after = conn.execute("SELECT MAX(last_seen) FROM job_raw").fetchone()[0]
    assert after > before, "rows were not refreshed"


def test_a_fresh_mark_still_skips_the_page(conn):
    """Resume's real purpose: a run continued minutes later does not redo work."""
    first = FakeFetcher(_pages())
    crawl_slice(first, conn, "ai-engineer", "anywhere", max_pages=2)

    second = FakeFetcher(_pages())
    res = crawl_slice(second, conn, "ai-engineer", "anywhere", max_pages=2)

    assert second.urls == []
    assert res.pages_fetched == 0
    assert res.pages_walked == 2


def test_a_slice_that_fetched_nothing_says_so(conn):
    """The failure was invisible because a no-op run read like a normal one."""
    f = FakeFetcher(_pages())
    crawl_slice(f, conn, "ai-engineer", "anywhere", max_pages=2)
    res = crawl_slice(FakeFetcher(_pages()), conn, "ai-engineer", "anywhere", max_pages=2)
    assert res.ended_reason == "resumed_fresh"


def test_no_resume_ignores_the_mark_entirely(conn):
    f = FakeFetcher(_pages())
    crawl_slice(f, conn, "ai-engineer", "anywhere", max_pages=2)
    again = FakeFetcher(_pages())
    res = crawl_slice(again, conn, "ai-engineer", "anywhere", max_pages=2, resume=False)
    assert len(again.urls) == 2 and res.pages_fetched == 2


def test_ttl_none_keeps_the_permanent_resume(conn):
    """`cache_ttl_hours: 0` means a cache entry never expires (ADR-011); the
    page mark follows it, so that configuration is unchanged."""
    f = FakeFetcher(_pages(), listing_ttl=None)
    crawl_slice(f, conn, "ai-engineer", "anywhere", max_pages=2)
    _age_marks(conn, hours=24 * 30)
    again = FakeFetcher(_pages(), listing_ttl=None)
    crawl_slice(again, conn, "ai-engineer", "anywhere", max_pages=2)
    assert again.urls == []


def test_a_refused_host_is_still_not_requested_twice(conn):
    """The Fetcher raises before anything here runs; re-walking pages must not
    turn a refusal into a second request (ADR-016)."""
    with pytest.raises(HostBlocked):
        crawl_slice(FakeFetcher(_pages(), blocked=True), conn,
                    "ai-engineer", "anywhere", max_pages=2)


# --------------------------------------------------------------------------- #
# store.page_done
# --------------------------------------------------------------------------- #

def test_page_done_honours_max_age(conn):
    S.mark_page(conn, "ai-engineer|anywhere", 1, "done", 20)
    assert S.page_done(conn, "ai-engineer|anywhere", 1, max_age=6 * HOUR)
    assert S.page_done(conn, "ai-engineer|anywhere", 1)          # None = forever
    _age_marks(conn, hours=7)
    assert not S.page_done(conn, "ai-engineer|anywhere", 1, max_age=6 * HOUR)
    assert S.page_done(conn, "ai-engineer|anywhere", 1)


def test_an_unreadable_timestamp_means_refetch(conn):
    """Doing the work again is the safe direction; skipping is not."""
    S.mark_page(conn, "ai-engineer|anywhere", 1, "done", 20)
    conn.execute("UPDATE crawl_unit SET updated_at = 'not a date'")
    assert not S.page_done(conn, "ai-engineer|anywhere", 1, max_age=6 * HOUR)


def test_a_page_that_was_never_done_is_not_done(conn):
    S.mark_page(conn, "ai-engineer|anywhere", 1, "empty", 0)
    assert not S.page_done(conn, "ai-engineer|anywhere", 1, max_age=6 * HOUR)

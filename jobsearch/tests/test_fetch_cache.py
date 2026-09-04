"""Cache freshness: a listing fetch must be able to see jobs posted since the
last run, without ever becoming a retry path through a mitigation.

The bug this exists for: `cache/` never expired. `_raw_get` returned any cached
entry unconditionally and nothing in the crawl/ingest path passed `refresh=True`,
so `fetch --roles X` replayed pages fetched days earlier and made zero network
requests. A tool whose whole purpose is a live role fetch could not discover a
job posted after the first crawl of a URL.

No network: `httpx.Client` is replaced at the module boundary, which is the only
place a fake is allowed to live.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src import assertions as A
from src import fetch as F
from src.fetch import Fetcher

HOUR = 3600.0


class FakeClient:
    """Stands in for httpx.Client. Counts every request that reaches the wire."""

    calls: list[str] = []
    status: int = 200
    headers: dict[str, str] = {}
    body: str = "fresh-from-network"

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        FakeClient.calls.append(url)
        requested = url

        class R:
            status_code = FakeClient.status
            headers = FakeClient.headers
            text = FakeClient.body
            url = requested

        return R()


@pytest.fixture
def f(tmp_path, monkeypatch):
    monkeypatch.setattr(F.httpx, "Client", FakeClient)
    # No real sleeping: the throttle is exercised in its own test, and 3-5 s per
    # call would make this file unrunnable.
    monkeypatch.setattr(F.time, "sleep", lambda _s: None)
    FakeClient.calls = []
    FakeClient.status, FakeClient.headers = 200, {}
    FakeClient.body = "fresh-from-network"
    fetcher = Fetcher(tmp_path / "cache", "test-agent/1.0", (3.0, 5.0))
    # robots.txt for the test origin, pre-seeded so `allowed()` resolves offline.
    _seed(fetcher, "https://x.test/robots.txt", "User-agent: *\nDisallow: /admin/\n",
          age_hours=0)
    return fetcher


def _seed(f: Fetcher, url: str, body: str, age_hours: float,
          status: int = 200, headers: dict | None = None) -> None:
    """Write a cache entry that claims to have been fetched `age_hours` ago.

    The age comes from the recorded `fetched_at`, not the file mtime: it is the
    real fetch time and it travels with the cache directory.
    """
    meta_p, body_p = f._paths(url)
    when = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    meta_p.write_text(json.dumps({
        "url": url, "final_url": url, "status": status,
        "headers": headers or {}, "elapsed_ms": 1,
        "fetched_at": when.isoformat(), "error": None,
    }), encoding="utf-8")
    body_p.write_text(body, encoding="utf-8")


# --- the regression -----------------------------------------------------------


def test_stale_listing_is_refetched(f):
    """THE BUG. A 7-hour-old page under a 6-hour TTL must go back to the network,
    or a job posted this morning is invisible until the cache is deleted by hand."""
    _seed(f, "https://x.test/role/ai", "day-old-page", age_hours=7)
    r = f.get("https://x.test/role/ai", max_age=6 * HOUR)
    assert FakeClient.calls == ["https://x.test/role/ai"], "stale entry was replayed"
    assert r.text == "fresh-from-network"
    assert r.from_cache is False


def test_fresh_listing_is_served_from_cache(f):
    """The other half: within the window, a re-run must stay free. A TTL that
    always refetches would just be `refresh=True` with extra steps."""
    _seed(f, "https://x.test/role/ai", "recent-page", age_hours=1)
    r = f.get("https://x.test/role/ai", max_age=6 * HOUR)
    assert FakeClient.calls == []
    assert r.text == "recent-page" and r.from_cache is True


def test_without_max_age_the_cache_is_still_permanent(f):
    """Detail pages pass no max_age. A job description does not change, so
    re-fetching them is pure cost against a third party."""
    _seed(f, "https://x.test/jobs/1-abc", "old-detail", age_hours=24 * 30)
    r = f.get("https://x.test/jobs/1-abc")
    assert FakeClient.calls == []
    assert r.text == "old-detail"


# --- the conduct carve-out ----------------------------------------------------


def test_cached_mitigation_never_expires(f):
    """A cached 403 must stay sticky FOREVER and keep halting.

    Expiring it would turn the TTL into a backoff-and-continue path, which is
    exactly what AGENTS.md forbids: "Never retry through it, never add a
    backoff-and-continue path." The age here is 30 days against a 6-hour TTL.
    """
    _seed(f, "https://x.test/role/ai", "<html>denied</html>", age_hours=24 * 30,
          status=403, headers={"cf-ray": "abc123"})
    with pytest.raises(A.MitigationDetected):
        f.get("https://x.test/role/ai", max_age=6 * HOUR)
    assert FakeClient.calls == [], "a cached mitigation was retried through"


def test_cached_cf_mitigated_header_never_expires(f):
    """Same guard, triggered by the header rather than the status code -- a 200
    carrying cf-mitigated is still Cloudflare acting on us."""
    _seed(f, "https://x.test/role/ai", "ok-looking", age_hours=24 * 30,
          status=200, headers={"cf-mitigated": "challenge", "cf-ray": "r1"})
    with pytest.raises(A.MitigationDetected):
        f.get("https://x.test/role/ai", max_age=6 * HOUR)
    assert FakeClient.calls == []


def test_ordinary_failures_do_expire(f):
    """A 404 is not a mitigation. A board that 404'd last week may exist today,
    so it is subject to the TTL like any other response."""
    _seed(f, "https://x.test/role/gone", "Not Found", age_hours=7, status=404)
    f.get("https://x.test/role/gone", max_age=6 * HOUR)
    assert FakeClient.calls == ["https://x.test/role/gone"]


# --- robots -------------------------------------------------------------------


def test_robots_is_refetched_daily(f):
    """robots.txt was cached permanently, so a site could tighten its rules and
    we would never see it. It gets its own, longer TTL."""
    _seed(f, "https://y.test/robots.txt", "User-agent: *\nDisallow: /old/\n",
          age_hours=30)
    FakeClient.body = "User-agent: *\nDisallow: /jobs\n"
    assert f.allowed("https://y.test/jobs")[0] is False, \
        "judged by a stale robots.txt"
    assert FakeClient.calls == ["https://y.test/robots.txt"]


def test_fresh_robots_is_not_refetched(f):
    _seed(f, "https://y.test/robots.txt", "User-agent: *\nDisallow: /jobs\n",
          age_hours=2)
    assert f.allowed("https://y.test/jobs")[0] is False
    assert FakeClient.calls == []


# --- interaction with the existing flags --------------------------------------


def test_refresh_still_wins_over_a_fresh_entry(f):
    _seed(f, "https://x.test/role/ai", "recent", age_hours=0.1)
    f.get("https://x.test/role/ai", refresh=True, max_age=6 * HOUR)
    assert FakeClient.calls == ["https://x.test/role/ai"]


def test_robots_is_still_enforced_on_a_refetch(f):
    """The TTL adds requests; it must not bypass a single rule."""
    _seed(f, "https://x.test/admin/x", "stale", age_hours=99)
    with pytest.raises(F.RobotsDisallowed):
        f.get("https://x.test/admin/x", max_age=6 * HOUR)
    assert FakeClient.calls == []

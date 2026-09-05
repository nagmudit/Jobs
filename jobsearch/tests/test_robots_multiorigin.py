"""robots.txt is per-origin. No network: rules are pre-seeded into the disk cache."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.fetch import Fetcher, RobotsDisallowed

PERMISSIVE = "User-agent: *\nDisallow: /admin/\n"
STRICT = "User-agent: *\nDisallow: /jobs\nDisallow: /search\n"


def _seed(f: Fetcher, origin: str, body: str, status: int = 200) -> None:
    """Write a robots.txt straight into the fetcher's cache so `allowed()`
    resolves offline.

    The timestamp is relative to NOW, deliberately. It was once hardcoded to a
    literal date, and when robots.txt gained a 24 h TTL (ADR-011) that turned
    every test in this file into a time bomb: green while the date was recent,
    red the following day, with no code change in between. A fixture that
    expresses "fresh" must say so in relative terms.
    """
    url = f"{origin}/robots.txt"
    meta_p, body_p = f._paths(url)
    fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
    meta_p.write_text(json.dumps({
        "url": url, "final_url": url, "status": status, "headers": {},
        "elapsed_ms": 1, "fetched_at": fresh.isoformat(), "error": None,
    }), encoding="utf-8")
    body_p.write_text(body, encoding="utf-8")


@pytest.fixture
def f(tmp_path):
    return Fetcher(tmp_path / "cache", "test-agent/1.0", (3.0, 5.0))


def test_rules_do_not_leak_between_origins(f):
    """The bug this exists for: one shared rule set meant the first host's
    robots.txt governed every later host, so a path another site forbids could
    be fetched anyway."""
    _seed(f, "https://a.test", PERMISSIVE)
    _seed(f, "https://b.test", STRICT)

    # a.test allows /jobs; b.test forbids it. Load a.test FIRST.
    assert f.allowed("https://a.test/jobs")[0] is True
    assert f.allowed("https://b.test/jobs")[0] is False, \
        "b.test was judged by a.test's robots.txt"


def test_order_independent(f):
    """Same assertion with the load order reversed -- a cache keyed by anything
    other than origin passes one direction and fails the other."""
    _seed(f, "https://a.test", PERMISSIVE)
    _seed(f, "https://b.test", STRICT)
    assert f.allowed("https://b.test/jobs")[0] is False
    assert f.allowed("https://a.test/jobs")[0] is True


def test_each_origin_is_fetched_once(f):
    _seed(f, "https://a.test", PERMISSIVE)
    _seed(f, "https://b.test", STRICT)
    f.allowed("https://a.test/x")
    f.allowed("https://b.test/x")
    f.allowed("https://a.test/y")
    assert set(f._robots) == {"https://a.test", "https://b.test"}


def test_shared_disallow_still_applies_per_origin(f):
    _seed(f, "https://a.test", STRICT)
    _seed(f, "https://b.test", STRICT)
    assert f.allowed("https://a.test/search")[0] is False
    assert f.allowed("https://b.test/search")[0] is False


def test_unreadable_robots_refuses_rather_than_assuming_allowed(f):
    """naukri.com serves 403 on its own robots.txt. Crawling blind is not an
    option, so this must raise rather than default to permissive."""
    _seed(f, "https://blind.test", "<html>forbidden</html>", status=403)
    with pytest.raises(RuntimeError, match="refusing to crawl blind"):
        f.allowed("https://blind.test/jobs")


def test_unreadable_robots_is_not_cached_as_permissive(f):
    """A failed load must keep failing. Caching the failure as an empty rule set
    would make the second call silently permissive."""
    _seed(f, "https://blind.test", "nope", status=403)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            f.allowed("https://blind.test/jobs")
    assert "https://blind.test" not in f._robots


def test_one_bad_origin_does_not_poison_a_good_one(f):
    _seed(f, "https://blind.test", "nope", status=403)
    _seed(f, "https://a.test", PERMISSIVE)
    with pytest.raises(RuntimeError):
        f.allowed("https://blind.test/jobs")
    assert f.allowed("https://a.test/jobs")[0] is True


def test_assert_allowed_names_the_origin(f):
    _seed(f, "https://b.test", STRICT)
    with pytest.raises(RobotsDisallowed, match="b.test"):
        f.assert_allowed("https://b.test/jobs")

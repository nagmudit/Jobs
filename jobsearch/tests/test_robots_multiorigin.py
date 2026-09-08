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


# --- out-of-band permission overrides ------------------------------------------
#
# vickybytes.com's robots.txt says `Disallow: /api`, and its owner separately
# agreed to let this tool read /api/opportunities. That is a real permission but
# an out-of-band one, so it is DECLARED in targets.yaml with its evidence rather
# than resolved by loosening the check. robots.txt is still fetched, still
# parsed, and still governs every other path on that host and every other host.
#
# These tests exist to stop the mechanism drifting into a general bypass, which
# is the only way it could become one.

VICKY = ("User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /auth/\n"
         "Disallow: /api\n")

GRANT = [{
    "origin": "https://vicky.test",
    "prefix": "/api/opportunities",
    "granted_by": "site owner, informal",
    "granted_on": "2026-09-08",
    "note": "owner agreed to a personal daily fetch of this one endpoint",
}]


@pytest.fixture
def granted(tmp_path):
    return Fetcher(tmp_path / "cache", "test-agent/1.0", (3.0, 5.0),
                   robots_overrides=GRANT)


def test_the_declared_prefix_is_permitted(granted):
    _seed(granted, "https://vicky.test", VICKY)
    ok, _ = granted.allowed("https://vicky.test/api/opportunities")
    assert ok, "the granted endpoint is still refused"
    granted.assert_allowed("https://vicky.test/api/opportunities")


def test_every_other_disallowed_path_on_that_host_is_still_refused(granted):
    """The override must flip ONE prefix, not the host."""
    _seed(granted, "https://vicky.test", VICKY)
    for path in ("/admin/x", "/auth/login", "/api", "/api/users"):
        with pytest.raises(RobotsDisallowed):
            granted.assert_allowed(f"https://vicky.test{path}")


def test_the_same_prefix_on_another_host_is_refused(granted):
    """Permission was granted by one site's owner. It is not transferable."""
    _seed(granted, "https://other.test", VICKY)
    with pytest.raises(RobotsDisallowed):
        granted.assert_allowed("https://other.test/api/opportunities")


def test_an_override_cannot_be_used_to_crawl_blind(tmp_path):
    """The naukri.com protection (ADR-007) must survive.

    A host whose robots.txt cannot be read still raises, even for a path an
    override would otherwise permit -- otherwise declaring an override becomes a
    way to skip reading robots.txt at all.
    """
    f = Fetcher(tmp_path / "cache", "test-agent/1.0", (3.0, 5.0),
                robots_overrides=GRANT)
    _seed(f, "https://vicky.test", "", status=403)
    with pytest.raises(RuntimeError, match="could not read robots.txt"):
        f.allowed("https://vicky.test/api/opportunities")


def test_no_overrides_behaves_exactly_as_before(f):
    """The default path is untouched: same fetcher, same rules, still refused."""
    _seed(f, "https://vicky.test", VICKY)
    with pytest.raises(RobotsDisallowed):
        f.assert_allowed("https://vicky.test/api/opportunities")


def test_an_allowed_path_is_not_reported_as_an_override(granted):
    """`/blog` was always allowed. Reporting it as overridden would make the
    override look load-bearing where it is not."""
    _seed(granted, "https://vicky.test", VICKY)
    ok, rule = granted.allowed("https://vicky.test/blog/post")
    assert ok and rule != "override"

"""The conduct guarantees, asserted as behaviour rather than as convention.

These close the P0 cluster in `quality/test-manifest.yaml`:

    GAP-011  the rate limiter itself is unverified
    GAP-002  nothing proves Fetcher.get actually calls check_mitigation
    GAP-003  the delay_range floor guard has never been exercised
    GAP-007  "all egress goes through Fetcher.get" is convention only

Every one of them guards the same thing: getting the user's IP blocked, which
breaks the tool irrecoverably and harms a third party who never agreed to any of
this. A regression that dropped the spacing to zero passed the entire suite
before this file existed.

No network. `httpx.Client` is replaced at the module boundary, and `time.sleep`
is recorded rather than performed -- a real 3-5 s wait per call would make this
unrunnable, and what matters is that the delay is REQUESTED, not endured.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from src import assertions as A
from src import fetch as F
from src.config import Config
from src.fetch import Fetcher

SRC = Path(__file__).resolve().parent.parent / "src"


class Client:
    """Network boundary stand-in. Returns whatever the test asked for."""

    status = 200
    headers: dict[str, str] = {}
    body = "ok"
    calls: list[str] = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        Client.calls.append(url)
        requested = url

        class R:
            status_code = Client.status
            headers = Client.headers
            text = Client.body
            url = requested

        return R()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A Fetcher whose sleeps are recorded, with robots pre-seeded."""
    slept: list[float] = []
    monkeypatch.setattr(F.httpx, "Client", Client)
    monkeypatch.setattr(F.time, "sleep", lambda s: slept.append(s))
    Client.calls, Client.status, Client.headers, Client.body = [], 200, {}, "ok"
    f = Fetcher(tmp_path / "cache", "jobsearch-test/1.0 (+someone@example.com)",
                (3.0, 5.0))
    meta_p, body_p = f._paths("https://x.test/robots.txt")
    meta_p.write_text(json.dumps({
        "url": "https://x.test/robots.txt", "final_url": "https://x.test/robots.txt",
        "status": 200, "headers": {}, "elapsed_ms": 1,
        "fetched_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(), "error": None,
    }), encoding="utf-8")
    body_p.write_text("User-agent: *\nDisallow: /admin/\n", encoding="utf-8")
    return f, slept


# --- GAP-011: the rate limiter ------------------------------------------------


def test_consecutive_requests_are_spaced_by_the_configured_delay(env):
    """The single worst regression this repo can ship: spacing dropped to zero
    and hammering a third party at full speed, with every other test green."""
    f, slept = env
    for i in range(4):
        f.get(f"https://x.test/p{i}")
    assert len(Client.calls) == 4
    # First call may not sleep (no prior request); every later one must.
    assert len(slept) >= 3, f"only {len(slept)} waits across 4 requests"
    assert all(3.0 <= s <= 5.0 for s in slept), \
        f"waits outside the mandated 3-5 s window: {slept}"


def test_the_delay_is_drawn_from_the_configured_range_not_a_constant(env):
    """A hardcoded sleep would satisfy the bound above while ignoring config."""
    f, slept = env
    f.delay_range = (7.0, 8.0)
    f.get("https://x.test/a")
    f.get("https://x.test/b")
    assert slept and all(7.0 <= s <= 8.0 for s in slept), slept


def test_a_cache_hit_does_not_sleep(env):
    """Throttling is about traffic we actually send. Sleeping on a cache hit
    would make re-runs pointlessly slow and mask a broken cache."""
    f, slept = env
    f.get("https://x.test/p")
    n = len(slept)
    f.get("https://x.test/p")
    assert len(slept) == n, "slept for a request that never left the machine"


# --- GAP-002: the mitigation call path ----------------------------------------


@pytest.mark.parametrize("status,headers", [
    (403, {"cf-ray": "r1"}),
    (429, {"cf-ray": "r2"}),
    (503, {"cf-ray": "r3"}),
    (200, {"cf-mitigated": "challenge", "cf-ray": "r4"}),
])
def test_get_halts_on_a_live_mitigation(env, status, headers):
    """check_mitigation is unit-tested, but nothing proved `get` CALLS it. It
    could have been deleted from the request path without a single failure."""
    f, _ = env
    Client.status, Client.headers = status, headers
    with pytest.raises(A.MitigationDetected):
        f.get("https://x.test/p")


def test_a_clean_response_does_not_raise(env):
    """The other half: the guard must not fire on ordinary traffic, or it would
    be disabled in frustration."""
    f, _ = env
    Client.status, Client.headers = 200, {"cf-ray": "ok1"}
    assert f.get("https://x.test/p").ok


def test_the_mitigation_is_recorded_before_it_raises(env):
    """`on_response` feeds request_log. Raising before logging would lose the
    cf-ray, which is the only evidence of what happened."""
    f, _ = env
    seen: list = []
    f.on_response = seen.append
    Client.status, Client.headers = 403, {"cf-ray": "evidence-1"}
    with pytest.raises(A.MitigationDetected):
        f.get("https://x.test/p")
    assert seen and seen[0].cf_ray == "evidence-1", "the halt lost its evidence"


# --- GAP-003: the delay_range floor guard -------------------------------------


def test_config_refuses_a_delay_floor_below_three_seconds(tmp_path):
    """A conduct control expressed in config. It had never been executed."""
    p = tmp_path / "t.yaml"
    p.write_text("roles: [x]\nlocations: [anywhere]\ndelay_range: [0.1, 0.2]\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="Refusing to go faster"):
        Config.load(p)


def test_config_accepts_the_mandated_floor(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("roles: [x]\nlocations: [anywhere]\ndelay_range: [3.0, 5.0]\n",
                 encoding="utf-8")
    assert Config.load(p).delay_range == (3.0, 5.0)


def test_a_cli_override_cannot_smuggle_a_faster_floor(tmp_path):
    """The guard runs at load time, after overrides are applied, so there is no
    path that produces a Config below the floor."""
    p = tmp_path / "t.yaml"
    p.write_text("roles: [x]\nlocations: [anywhere]\ndelay_range: [2.9, 5.0]\n",
                 encoding="utf-8")
    with pytest.raises(ValueError):
        Config.load(p, roles="a,b")


# --- GAP-007: one way out ------------------------------------------------------


def _egress_offenders() -> list[str]:
    """Modules under src/ that reach the network without going through Fetcher.

    A static check rather than a runtime one: the risk is a NEW call site added
    later, which no runtime test would exercise until it shipped.
    """
    allowed = {"fetch.py"}          # the one module that may hold the transport
    # Modules that actually open a connection. `urllib.parse` is deliberately
    # NOT here -- it is string manipulation, and every source module uses it for
    # `quote`/`urlparse`. Listing it would make this guard cry wolf, and a guard
    # that cries wolf gets deleted.
    transports = {"httpx", "requests", "aiohttp", "urllib3", "socket",
                  "http.client", "urllib.request", "urllib.error"}

    def offends(dotted: str) -> bool:
        return any(dotted == t or dotted.startswith(t + ".") for t in transports)

    bad: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                # `from urllib import request` is as much a transport as
                # `import urllib.request`.
                names = [mod] + [f"{mod}.{a.name}" for a in node.names]
            else:
                continue
            for n in names:
                if offends(n):
                    bad.append(f"{path.relative_to(SRC)} imports {n}")
    return bad


def test_only_fetch_py_can_reach_the_network():
    """"All network access goes through Fetcher.get" was convention only. A
    second transport anywhere else bypasses robots, the rate limit AND the
    mitigation halt in one step -- every conduct rule at once."""
    offenders = _egress_offenders()
    assert offenders == [], (
        "network transport imported outside src/fetch.py: " + "; ".join(offenders))


def test_the_egress_check_can_actually_detect_an_offender(tmp_path):
    """A guard that cannot fail is not a guard. This proves the AST walk sees a
    real import rather than vacuously passing."""
    bad = SRC / "_egress_probe_tmp.py"
    bad.write_text("import httpx\n", encoding="utf-8")
    try:
        assert any("_egress_probe_tmp" in o for o in _egress_offenders())
    finally:
        bad.unlink()


def test_the_user_agent_is_honest_and_carries_a_contact(env):
    """No UA spoofing, and a contact address so anyone we bother can reach the
    user. Asserted on what is actually sent, not on config."""
    f, _ = env
    sent: dict = {}

    class Recording(Client):
        def get(self, url, headers=None):
            sent.update(headers or {})
            return super().get(url, headers)

    f_headers_client = Recording
    import src.fetch as mod
    old, mod.httpx.Client = mod.httpx.Client, f_headers_client
    try:
        f.get("https://x.test/p")
    finally:
        mod.httpx.Client = old
    ua = sent.get("User-Agent", "")
    assert "jobsearch" in ua, f"User-Agent does not identify the tool: {ua!r}"
    assert "@" in ua, f"User-Agent carries no contact address: {ua!r}"
    for browser in ("Mozilla", "Chrome", "Safari", "Gecko"):
        assert browser not in ua, f"User-Agent impersonates a browser: {ua!r}"

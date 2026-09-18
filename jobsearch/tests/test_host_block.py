"""A block stops that host for the rest of the run, never the others (ADR-016).

Replays the 2026-09-18 daily failure: himalayas.app answered 403 to its own
robots.txt and the whole run died, taking every other source and role with it.

Two halves, both conduct guarantees:

* A blocked origin gets NO further request of any kind in that run -- not a
  page, not its robots.txt. That is what keeps "carry on with other hosts"
  from being a retry path. Enforced in Fetcher.get, so no caller can undo it.
* Every other origin carries on, and the run still fails loudly (exit 3).

Fakes sit at httpx.Client only (AGENTS.md testing contract).
"""

from __future__ import annotations

import json
import time

import pytest

from src import assertions as A
from src import fetch as F
from src import store as S
from src.fetch import Fetcher, HostBlocked, RobotsUnreadable

OPEN = "User-agent: *\nAllow: /\n"


class Net:
    """Network boundary. Routes by URL prefix; unmatched URLs 404."""

    routes: dict[str, tuple[int, dict, str]] = {}
    calls: list[str] = []

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        Net.calls.append(url)
        status, hdrs, body = 404, {}, "not found"
        for prefix, resp in sorted(Net.routes.items(), key=lambda kv: -len(kv[0])):
            if url.startswith(prefix):
                status, hdrs, body = resp
                break
        requested = url

        class R:
            status_code = status
            headers = hdrs
            text = body
            url = requested

        return R()


def calls_to(host: str) -> list[str]:
    return [u for u in Net.calls if u.startswith(f"https://{host}/")]


@pytest.fixture
def net(tmp_path, monkeypatch):
    monkeypatch.setattr(F.httpx, "Client", Net)
    monkeypatch.setattr(F.time, "sleep", lambda s: None)
    Net.routes, Net.calls = {}, []
    return tmp_path


def _fetcher(tmp_path) -> Fetcher:
    return Fetcher(tmp_path / "cache", "jobsearch-test/1.0", (3.0, 5.0))


# --- the Fetcher --------------------------------------------------------------


@pytest.mark.parametrize("status,headers", [
    (403, {}), (429, {}), (503, {}), (200, {"cf-mitigated": "challenge"}),
])
def test_a_mitigation_blocks_its_host_for_the_rest_of_the_run(net, status, headers):
    Net.routes = {"https://a.test/robots.txt": (200, {}, OPEN),
                  "https://a.test/": (status, headers, "no")}
    f = _fetcher(net)
    with pytest.raises(A.MitigationDetected):
        f.get("https://a.test/jobs?page=1")
    before = len(Net.calls)

    with pytest.raises(HostBlocked) as e:
        f.get("https://a.test/jobs?page=2")      # a different URL, same origin
    assert len(Net.calls) == before, "a blocked host was contacted again"
    assert e.value.origin == "https://a.test"
    assert "https://a.test" in f.blocked


def test_an_unreadable_robots_txt_blocks_its_host(net):
    """The 2026-09-18 failure: 403 on robots.txt itself."""
    Net.routes = {"https://a.test/robots.txt": (403, {}, "forbidden")}
    f = _fetcher(net)
    with pytest.raises(RobotsUnreadable, match="refusing to crawl blind"):
        f.get("https://a.test/jobs")
    before = len(Net.calls)

    with pytest.raises(HostBlocked):
        f.get("https://a.test/other")
    assert len(Net.calls) == before, "robots.txt of a blocked host was re-requested"


def test_other_hosts_carry_on_and_keep_their_own_robots(net):
    """Scoped to the origin: b.test is still fetched, and still judged by its
    own robots.txt (ADR-007) -- the block must not turn into a free pass."""
    Net.routes = {"https://a.test/robots.txt": (403, {}, "forbidden"),
                  "https://b.test/robots.txt": (200, {}, "User-agent: *\nDisallow: /private\n"),
                  "https://b.test/": (200, {}, "fine")}
    f = _fetcher(net)
    with pytest.raises(RobotsUnreadable):
        f.get("https://a.test/jobs")
    assert f.get("https://b.test/jobs").ok
    with pytest.raises(F.RobotsDisallowed):
        f.get("https://b.test/private/x")
    assert set(f.blocked) == {"https://a.test"}


def test_the_refusal_is_still_logged_before_the_block(net):
    """request_log keeps the cf-ray of the response that blocked us."""
    Net.routes = {"https://a.test/robots.txt": (200, {}, OPEN),
                  "https://a.test/": (429, {"cf-ray": "evidence-9"}, "slow down")}
    f = _fetcher(net)
    seen: list = []
    f.on_response = seen.append
    with pytest.raises(A.MitigationDetected):
        f.get("https://a.test/jobs")
    assert [r.cf_ray for r in seen] == ["evidence-9"]


def test_a_path_disallow_is_not_a_host_block(net):
    """robots forbidding one path says nothing about the rest of the host."""
    Net.routes = {"https://a.test/robots.txt": (200, {}, "User-agent: *\nDisallow: /x\n"),
                  "https://a.test/": (200, {}, "ok")}
    f = _fetcher(net)
    with pytest.raises(F.RobotsDisallowed):
        f.get("https://a.test/x/1")
    assert not f.blocked
    assert f.get("https://a.test/y").ok


# --- a whole fetch ------------------------------------------------------------


def _ro_feed() -> str:
    now = int(time.time())
    return json.dumps([
        {"legal": "API Terms of Service"},
        {"id": "501", "slug": "ai-eng", "position": "AI Engineer", "company": "Acme",
         "epoch": now - 3600, "description": "LLM work", "location": "Remote",
         "tags": ["ai"], "url": "https://remoteok.com/remote-jobs/501"},
    ])


def _replay_2026_09_18():
    Net.routes = {
        "https://himalayas.app/robots.txt": (403, {}, "<html>forbidden</html>"),
        "https://remoteok.com/robots.txt": (200, {}, OPEN),
        "https://remoteok.com/api": (200, {}, _ro_feed()),
    }


def test_fetch_role_continues_past_a_blocked_source(net):
    from src import roles as R
    from src.config import Config

    _replay_2026_09_18()
    cfg = Config.load()
    conn = S.connect(net / "t.db")
    f = _fetcher(net)
    res = R.fetch_role(f, conn, cfg, "artificial-intelligence-engineer",
                       sources=["himalayas", "remoteok"])
    by = {r["source"]: r for r in res}
    assert by["himalayas"]["filter_mode"] == "blocked"
    assert "himalayas.app" in by["himalayas"]["reason"]
    assert by["remoteok"]["seen"] == 1
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE source='remoteok'").fetchone()[0] == 1
    assert "blocked" in R.describe(res)


def test_a_host_blocked_in_one_role_gets_nothing_in_the_next(net):
    from src import roles as R
    from src.config import Config

    _replay_2026_09_18()
    cfg = Config.load()
    conn = S.connect(net / "t.db")
    f = _fetcher(net)
    R.fetch_role(f, conn, cfg, "artificial-intelligence-engineer",
                 sources=["himalayas", "remoteok"])
    first = len(calls_to("himalayas.app"))
    res = R.fetch_role(f, conn, cfg, "machine-learning-engineer",
                       sources=["himalayas", "remoteok"])
    assert first == 1, "only the refused robots.txt should ever have gone out"
    assert len(calls_to("himalayas.app")) == first
    assert [r["filter_mode"] for r in res if r["source"] == "himalayas"] == ["blocked"]


def test_an_integrity_error_still_halts_the_whole_run(net):
    """A host refusing us is scoped; OUR parser being wrong is not. SchemaDrift
    and friends mean the data cannot be trusted anywhere (ADR-003)."""
    from src import roles as R
    from src.config import Config

    Net.routes = {
        "https://himalayas.app/robots.txt": (200, {}, OPEN),
        "https://himalayas.app/jobs/api": (200, {}, json.dumps({"results": []})),
        "https://remoteok.com/robots.txt": (200, {}, OPEN),
        "https://remoteok.com/api": (200, {}, _ro_feed()),
    }
    cfg = Config.load()
    conn = S.connect(net / "t.db")
    with pytest.raises(A.SchemaDrift):
        R.fetch_role(_fetcher(net), conn, cfg, "artificial-intelligence-engineer",
                     sources=["himalayas", "remoteok"])
    assert not calls_to("remoteok.com"), "the run went on past an integrity error"


# --- the CLI: exit codes ------------------------------------------------------


def _cli(net, monkeypatch, *argv) -> int:
    from src import cli

    monkeypatch.setenv("JOBSEARCH_DB", str(net / "cli.db"))
    monkeypatch.setenv("JOBSEARCH_CACHE", str(net / "cli-cache"))
    return cli.main(list(argv))


def test_fetch_exits_3_and_names_the_blocked_host(net, monkeypatch, capsys):
    _replay_2026_09_18()
    rc = _cli(net, monkeypatch, "fetch", "--roles",
              "artificial-intelligence-engineer,machine-learning-engineer",
              "--sources", "himalayas,remoteok")
    err = capsys.readouterr().err
    assert rc == 3
    assert "BLOCKED" in err and "https://himalayas.app" in err
    assert len(calls_to("himalayas.app")) == 1


def test_fetch_exits_0_when_nothing_was_blocked(net, monkeypatch):
    _replay_2026_09_18()
    Net.routes["https://himalayas.app/robots.txt"] = (200, {}, OPEN)
    Net.routes["https://himalayas.app/jobs/api"] = (200, {}, json.dumps({"jobs": []}))
    assert _cli(net, monkeypatch, "fetch", "--roles", "artificial-intelligence-engineer",
                "--sources", "himalayas,remoteok") == 0


def test_fetch_exits_2_on_an_integrity_halt(net, monkeypatch):
    _replay_2026_09_18()
    Net.routes["https://himalayas.app/robots.txt"] = (200, {}, OPEN)
    Net.routes["https://himalayas.app/jobs/api"] = (200, {}, json.dumps({"results": []}))
    assert _cli(net, monkeypatch, "fetch", "--roles", "artificial-intelligence-engineer",
                "--sources", "himalayas,remoteok") == 2


def test_ingest_continues_past_a_blocked_source_and_exits_3(net, monkeypatch, capsys):
    _replay_2026_09_18()
    rc = _cli(net, monkeypatch, "ingest", "--sources", "himalayas,remoteok")
    assert rc == 3
    assert calls_to("remoteok.com"), "ingest stopped at the blocked source"
    assert "https://himalayas.app" in capsys.readouterr().err


# --- the web UI's background jobs ---------------------------------------------


@pytest.mark.parametrize("mode", ["fetch", "ingest"])
def test_the_ui_job_continues_and_reports_the_blocked_host(net, monkeypatch, mode):
    from src.config import Config
    from src.web.app import CrawlJob

    _replay_2026_09_18()
    monkeypatch.setenv("JOBSEARCH_DB", str(net / "ui.db"))
    monkeypatch.setenv("JOBSEARCH_CACHE", str(net / "ui-cache"))
    cfg = Config.load()
    job = CrawlJob()
    job.lock.acquire()                     # start_* would; the run releases it
    job.state = job._fresh_state(mode=mode)
    if mode == "fetch":
        job._run_fetch(cfg, ["artificial-intelligence-engineer"], ["himalayas", "remoteok"])
    else:
        job._run_ingest(cfg, ["himalayas", "remoteok"])

    assert job.state["error"] is None, job.state["error"]
    assert list(job.state["blocked"]) == ["https://himalayas.app"]
    assert calls_to("remoteok.com"), "the run stopped at the blocked host"

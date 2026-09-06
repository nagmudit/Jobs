"""A running fetch must be distinguishable from a hung one.

A cold all-sources fetch is ~180 minutes, almost all of it inside ATS company
expansion. `ATS.resolve` probes up to six token candidates per company at 3-5s
each, so a company that has no board burns ~30s -- and `_expand_ats` used to
`continue` past it without emitting anything. Whole minutes passed with the
counters frozen, which is indistinguishable from a deadlock.

Two guarantees are pinned here:

  * every company expansion emits an event, resolved or not, so the counter
    tracks work DONE rather than work that happened to succeed;
  * every response bumps a heartbeat, hung on Fetcher.on_response -- the one
    chokepoint all network access goes through -- so no code path can be busy
    and silent at the same time.
"""

import json

import pytest

from src import ats as ATS
from src import store as S
from src.web.app import CrawlJob

GH_JOB = {
    "id": 5001,
    "title": "AI Engineer",
    "absolute_url": "https://boards.greenhouse.io/alloy/jobs/5001",
    "updated_at": "2026-09-01T00:00:00Z",
    "location": {"name": "Remote"},
    "content": "<p>Build models</p>",
}


class Boards:
    """Serves a Greenhouse payload for known tokens, 404 otherwise.

    Mirrors tests/test_greenhouse.py::Fetch -- an unknown company exhausts all
    six candidates, which is the expensive-and-silent path under test.
    """

    listing_ttl = None

    def __init__(self, boards: dict[str, list]):
        self.boards = boards
        self.urls: list[str] = []

    def get(self, url, refresh=False, max_age=None):
        self.urls.append(url)
        token = url.split("/boards/")[1].split("/")[0]
        outer = self

        class R:
            status = 200 if token in outer.boards else 404
            ok = token in outer.boards
            text = json.dumps(
                {"jobs": outer.boards.get(token, []), "meta": {"total": 0}})
        return R()


def _seed(conn, role: str, companies: list[str]) -> None:
    """Put Wellfound rows in the corpus that declare Greenhouse as their ATS.

    That is what `ATS.companies_for_role` reads: expansion is bounded by the
    companies the role already touched, not by the provider (ADR-010).
    """
    for i, slug in enumerate(companies):
        native = str(9000 + i)
        S.upsert_job(conn, native, slug, {
            "id": native,
            "title": "AI Engineer",
            "atsSource": "AtsIntegration::Greenhouse::Listing",
            "startups": {"name": slug.title()},
        }, source="wellfound")
        S.add_provenance(conn, S.job_uid("wellfound", native), role, "anywhere", 1)
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


# --- every company reports, resolved or not -----------------------------------


def test_expand_ats_emits_an_event_for_unresolved_companies(conn):
    """The bug: a company with no board cost six requests and reported nothing.

    Two of the three companies here have no Greenhouse board. All three must
    still show up as progress -- otherwise the UI sits frozen for the ~60s
    those two take.
    """
    from src import roles as R

    role = "artificial-intelligence-engineer"
    _seed(conn, role, ["alloy", "ghostco", "phantomco"])
    f = Boards({"alloy": [GH_JOB]})

    events: list[dict] = []
    R._expand_ats(f, conn, None, role, "greenhouse",
                  keywords=[], cutoff=None, on_event=events.append)

    seen = {e.get("company") for e in events}
    assert seen == {"alloy", "ghostco", "phantomco"}, (
        f"only {seen} reported; an unresolved company is silent for ~30s")
    assert len(f.urls) > 3, "the unresolved companies really did cost requests"


def test_expand_ats_marks_which_companies_resolved(conn):
    """Progress that counts a failed probe as success would be its own lie."""
    from src import roles as R

    role = "artificial-intelligence-engineer"
    _seed(conn, role, ["alloy", "ghostco"])
    f = Boards({"alloy": [GH_JOB]})

    events: list[dict] = []
    R._expand_ats(f, conn, None, role, "greenhouse",
                  keywords=[], cutoff=None, on_event=events.append)

    by_company = {e["company"]: e for e in events}
    assert by_company["alloy"]["resolved"] is True
    assert by_company["ghostco"]["resolved"] is False


def test_expand_ats_reports_position_against_the_total(conn):
    """"company 37 of 312" is the only honest progress fraction available --
    the expansion is bounded by the corpus, so the total is known up front."""
    from src import roles as R

    role = "artificial-intelligence-engineer"
    _seed(conn, role, ["alloy", "ghostco", "phantomco"])
    f = Boards({"alloy": [GH_JOB]})

    events: list[dict] = []
    R._expand_ats(f, conn, None, role, "greenhouse",
                  keywords=[], cutoff=None, on_event=events.append)

    assert [e["unit"] for e in events] == [1, 2, 3]
    assert {e["unit_total"] for e in events} == {3}


# --- the heartbeat ------------------------------------------------------------


class _Resp:
    def __init__(self, url="https://example.test/x", status=200, from_cache=False):
        self.url = url
        self.status = status
        self.headers = {}
        self.text = ""
        self.elapsed_ms = 1
        self.from_cache = from_cache
        self.fetched_at = S.now()
        self.ok = status == 200
        self.error = None


def test_heartbeat_advances_on_every_response(conn):
    """Hung on Fetcher.on_response, which fires for EVERY response. A path that
    is busy cannot also be silent."""
    job = CrawlJob()
    job.state = job._fresh_state(mode="fetch", roles=["r"])

    job._note_response(_Resp(url="https://a.test/1"), conn)
    first = job.state["last_at"]
    job._note_response(_Resp(url="https://a.test/2"), conn)

    assert job.state["requests"] == 2
    assert job.state["last_at"] >= first
    assert job.state["last_url"].endswith("/2")


def test_heartbeat_counts_a_cache_hit_but_still_beats(conn):
    """A cached response is not network work, but it IS liveness -- an ATS
    re-run served entirely from cache must not read as hung."""
    job = CrawlJob()
    job.state = job._fresh_state(mode="fetch", roles=["r"])

    job._note_response(_Resp(from_cache=True), conn)
    job._note_response(_Resp(from_cache=False), conn)

    assert job.state["requests"] == 2
    assert job.state["cached"] == 1
    assert job.state["last_at"] is not None


def test_status_payload_carries_the_server_clock(conn):
    """"last activity 3s ago" is computed against the server's clock, not the
    browser's -- otherwise a skewed client invents a hang or hides one."""
    job = CrawlJob()
    job.state = job._fresh_state(mode="fetch", roles=["r"])
    job._note_response(_Resp(), conn)

    snap = job.snapshot()
    assert snap["now"] >= snap["last_at"]
    assert snap["now"] - snap["last_at"] < 5


# --- deploy: the corpus must be able to live outside the checkout -------------


def test_db_and_cache_paths_default_to_the_repo_root(monkeypatch):
    """Unset env must change nothing -- every local run depends on this."""
    from src.config import CACHE_DIR, DB_PATH, Config

    monkeypatch.delenv("JOBSEARCH_DB", raising=False)
    monkeypatch.delenv("JOBSEARCH_CACHE", raising=False)
    cfg = Config.load()
    assert cfg.db_path == DB_PATH and cfg.cache_dir == CACHE_DIR


def test_db_and_cache_paths_follow_the_environment(monkeypatch, tmp_path):
    """An unattended deploy keeps code in a replaceable checkout and data on a
    volume that must survive it."""
    from src.config import Config

    monkeypatch.setenv("JOBSEARCH_DB", str(tmp_path / "corpus" / "jobs.db"))
    monkeypatch.setenv("JOBSEARCH_CACHE", str(tmp_path / "corpus" / "cache"))
    cfg = Config.load()
    assert cfg.db_path == tmp_path / "corpus" / "jobs.db"
    assert cfg.cache_dir == tmp_path / "corpus" / "cache"

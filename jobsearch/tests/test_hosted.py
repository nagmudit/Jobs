"""The hosted split: GitHub Actions crawls, Vercel serves a read-only UI.

Vercel cannot crawl -- a cold fetch is 75-180 minutes against function timeouts
measured in minutes, on a read-only filesystem. So the hosted app registers no
crawl routes at all. That is a conduct control, not tidiness: a public URL
carrying /api/fetch is an internet-reachable trigger for crawling third-party
sites, with the concurrency-1 guarantee broken by whoever else calls it.

The published corpus carries NO user marks. The daily workflow strips
`user_state` before upload, so the public URL really is only third-party job
listings -- shortlist/applied/hidden stay on the user's machine.

That makes a local `sync` load-bearing: downloading a fresh corpus REPLACES the
local one, and the marks would go with it. `store.prune_stale` already states
the invariant this has to uphold -- a job you marked applied keeps that mark.
"""

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from src import store as S
from src.config import Config
from src.web.app import create_app

CRAWL_ROUTES = ["/api/crawl", "/api/fetch", "/api/ingest", "/api/enrich"]


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSEARCH_DB", str(tmp_path / "t.db"))
    return Config.load()


def _seed(conn, n=3):
    for i in range(n):
        native = str(7000 + i)
        S.upsert_job(conn, native, f"co{i}", {
            "id": native, "title": f"AI Engineer {i}",
            "startups": {"name": f"Co {i}"}}, source="wellfound")
        S.add_provenance(conn, S.job_uid("wellfound", native),
                         "artificial-intelligence-engineer", "anywhere", 1)
    conn.commit()


# --- read-only mode -----------------------------------------------------------


def test_readonly_app_does_not_register_crawl_routes(conn, cfg, monkeypatch):
    """404, not 403. The route must not exist on a public URL."""
    _seed(conn)
    monkeypatch.setenv("JOBSEARCH_READONLY", "1")
    client = TestClient(create_app(cfg))

    for path in CRAWL_ROUTES:
        r = client.post(path, json={})
        assert r.status_code == 404, f"{path} is reachable on the hosted app ({r.status_code})"


def test_readonly_app_still_serves_and_filters(conn, cfg, monkeypatch):
    """Read-only must not mean crippled -- browsing is the whole point."""
    _seed(conn)
    monkeypatch.setenv("JOBSEARCH_READONLY", "1")
    client = TestClient(create_app(cfg))

    assert client.get("/api/meta").json()["readonly"] is True
    rows = client.post("/api/jobs", json={"limit": 10}).json()
    assert rows["total"] == 3
    assert client.post("/api/facets", json={"limit": 10}).status_code == 200


def test_local_app_keeps_the_crawl_routes(conn, cfg, monkeypatch):
    """The default is unchanged: locally this is still the full tool."""
    _seed(conn)
    monkeypatch.delenv("JOBSEARCH_READONLY", raising=False)
    client = TestClient(create_app(cfg))

    assert client.get("/api/meta").json()["readonly"] is False
    # Reachable means "not 404" -- a 400/409 is the endpoint doing its own job.
    assert client.post("/api/fetch", json={"roles": [], "sources": []}).status_code != 404


# --- status survives the daily rebuild ----------------------------------------


# --- sync: a fresh corpus must not eat local marks ----------------------------


def _marks(db) -> dict[str, str]:
    conn = S.connect(db)
    try:
        return {r["source_job_id"]: r["status"]
                for r in conn.execute("SELECT source_job_id, status FROM user_state")}
    finally:
        conn.close()


def _incoming(tmp_path, n=3):
    """A corpus as published: listings, no marks."""
    p = tmp_path / "incoming.db"
    c = S.connect(p)
    _seed(c, n=n)
    c.close()
    return p


def test_sync_preserves_local_marks(tmp_path, monkeypatch):
    """Downloading a fresh corpus REPLACES the local file. Without the merge the
    applied/shortlisted history goes with it -- which is the whole reason this
    command exists."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = S.connect(local)
    _seed(c, n=3)
    S.set_status(c, S.job_uid("wellfound", "7000"), "applied")
    S.set_status(c, S.job_uid("wellfound", "7001"), "shortlisted")
    c.commit()
    c.close()

    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    assert cli.main(["sync", str(_incoming(tmp_path)), "--apply"]) == 0

    assert _marks(local) == {
        S.job_uid("wellfound", "7000"): "applied",
        S.job_uid("wellfound", "7001"): "shortlisted",
    }


def test_sync_without_apply_changes_nothing(tmp_path, monkeypatch):
    """Dry-run by default, like prune and reconcile."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = S.connect(local)
    _seed(c, n=1)
    S.set_status(c, S.job_uid("wellfound", "7000"), "applied")
    c.commit()
    c.close()
    before = local.stat().st_mtime_ns

    incoming = _incoming(tmp_path)
    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    assert cli.main(["sync", str(incoming)]) == 0

    assert local.stat().st_mtime_ns == before, "dry run rewrote the corpus"
    assert incoming.exists(), "dry run consumed the incoming file"


def test_sync_keeps_a_mark_whose_job_is_gone(tmp_path, monkeypatch):
    """user_state is keyed separately from job_raw on purpose. A job that ages
    out and comes back on a later crawl must still be marked applied."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = S.connect(local)
    _seed(c, n=3)
    S.set_status(c, "wellfound:does-not-exist-any-more", "applied")
    c.commit()
    c.close()

    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    assert cli.main(["sync", str(_incoming(tmp_path)), "--apply"]) == 0
    assert _marks(local).get("wellfound:does-not-exist-any-more") == "applied"


def test_sync_backs_up_what_it_replaces(tmp_path, monkeypatch):
    from src import cli

    local = tmp_path / "jobs.db"
    c = S.connect(local)
    _seed(c, n=1)
    c.close()

    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    cli.main(["sync", str(_incoming(tmp_path)), "--apply"])
    assert list(tmp_path.glob("jobs.db.bak-*")), "replaced the corpus with no backup"


# --- export: the published corpus carries no marks ----------------------------


def test_export_strips_every_mark_and_keeps_every_job(tmp_path, monkeypatch):
    """The privacy guarantee, asserted rather than documented. The public URL is
    meant to be third-party job listings and nothing else."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = S.connect(local)
    _seed(c, n=4)
    S.set_status(c, S.job_uid("wellfound", "7000"), "applied")
    S.set_status(c, S.job_uid("wellfound", "7002"), "hidden")
    c.commit()
    jobs_before = c.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0]
    c.close()

    out = tmp_path / "corpus.db"
    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    assert cli.main(["export", "--out", str(out)]) == 0

    assert _marks(out) == {}, "the published corpus carries user marks"
    pub = S.connect(out)
    assert pub.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == jobs_before
    pub.close()
    assert _marks(local), "export must not touch the local corpus"


def test_export_round_trips_through_sync(tmp_path, monkeypatch):
    """Publish strips the marks; sync puts them back. Together they are the
    whole persistence story now that there is no hosted store."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = S.connect(local)
    _seed(c, n=2)
    S.set_status(c, S.job_uid("wellfound", "7001"), "applied")
    c.commit()
    c.close()

    out = tmp_path / "corpus.db"
    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    cli.main(["export", "--out", str(out)])
    assert _marks(out) == {}

    cli.main(["sync", str(out), "--apply"])
    assert _marks(local) == {S.job_uid("wellfound", "7001"): "applied"}


# --- the published corpus is pruned and compacted ------------------------------
#
# Vercel's serverless bundle limit is 250 MB and the corpus grows every day, so
# the daily workflow prunes jobs past the age cutoff and `export` VACUUMs. SQLite
# does not hand deleted pages back to the OS, so the DELETE alone reclaims
# nothing -- the VACUUM is the part that actually shrinks the file.


def _stale_corpus(path, n_stale=40, n_fresh=4, start=8000):
    """A corpus with a lot of prunable bulk, so a VACUUM has something to do.

    `start` offsets the ids: a published corpus and the local one it replaces
    must not share job ids, or a carry-forward test passes vacuously because the
    row was in the incoming file all along.
    """
    day = 86400
    now = int(__import__("time").time())
    c = S.connect(path)
    for i in range(n_stale + n_fresh):
        stale = i < n_stale
        native = str(start + i)
        S.upsert_job(c, native, f"co{i}", {
            "id": native,
            "title": f"AI Engineer {i}",
            "liveStartAt": now - (400 if stale else 2) * day,
            "startups": {"name": f"Co {i}"},
            # Padding, so deleted rows are worth reclaiming.
            "description": "x" * 40_000,
        }, source="wellfound")
        S.add_provenance(c, S.job_uid("wellfound", native),
                         "artificial-intelligence-engineer", "anywhere", 1)
    c.commit()
    return c


def test_export_vacuums_so_the_file_actually_shrinks(tmp_path, monkeypatch):
    """The whole point of pruning. Asserted on bytes, because a DELETE that
    reclaims nothing looks identical from SQL."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = _stale_corpus(local)
    S.prune_stale(c, 30, dry_run=False)
    c.close()
    after_delete = local.stat().st_size

    out = tmp_path / "corpus.db"
    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    assert cli.main(["export", "--out", str(out)]) == 0

    assert out.stat().st_size < after_delete, (
        f"export did not compact: {out.stat().st_size} vs {after_delete} bytes")


def test_sync_carries_forward_a_pruned_job_you_applied_to(tmp_path, monkeypatch):
    """The published corpus prunes by age and knows nothing about marks. Without
    this, a job you applied to 40 days ago vanishes on the next sync and its
    user_state row points at nothing."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = _stale_corpus(local, n_stale=3, n_fresh=1)
    old_uid = S.job_uid("wellfound", "8000")          # one of the stale ones
    S.set_status(c, old_uid, "applied")
    c.commit()
    c.close()

    # What the workflow publishes: pruned, no marks.
    incoming = tmp_path / "incoming.db"
    inc = _stale_corpus(incoming, n_stale=0, n_fresh=2, start=9000)
    inc.close()

    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    assert cli.main(["sync", str(incoming), "--apply"]) == 0

    conn = S.connect(local)
    row = conn.execute(
        "SELECT status, title FROM jobs WHERE source_job_id = ?", (old_uid,)).fetchone()
    assert row is not None, "the applied job was lost in the sync"
    assert row["status"] == "applied"
    prov = conn.execute(
        "SELECT COUNT(*) FROM job_provenance WHERE source_job_id = ?",
        (old_uid,)).fetchone()[0]
    assert prov == 1, "the job came back without its provenance"
    conn.close()


def test_sync_does_not_carry_forward_unmarked_pruned_jobs(tmp_path, monkeypatch):
    """Otherwise the local corpus never shrinks and the prune is pointless."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = _stale_corpus(local, n_stale=3, n_fresh=0)
    c.close()

    incoming = tmp_path / "incoming.db"
    inc = _stale_corpus(incoming, n_stale=0, n_fresh=2, start=9000)
    inc.close()

    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    cli.main(["sync", str(incoming), "--apply"])

    conn = S.connect(local)
    assert conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == 2
    conn.close()


def test_sync_reports_a_locked_corpus_instead_of_an_errno(tmp_path, monkeypatch):
    """On Windows a running `serve` blocks the swap. The raw PermissionError
    says nothing useful, and a half-done sync would be worse."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = _stale_corpus(local, n_stale=1, n_fresh=1)
    c.close()
    incoming = tmp_path / "incoming.db"
    _stale_corpus(incoming, n_stale=0, n_fresh=1, start=9000).close()

    def locked(self, target):
        raise PermissionError(32, "in use by another process")
    monkeypatch.setattr(pathlib.Path, "replace", locked)
    monkeypatch.setenv("JOBSEARCH_DB", str(local))

    assert cli.main(["sync", str(incoming), "--apply"]) == 2
    assert local.exists(), "the corpus was disturbed despite the failure"

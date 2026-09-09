"""A dated history of status changes, and the numbers it answers.

`user_state` holds one MUTABLE row per job, so `set_status` overwrites `status`
and `updated_at` in place. The moment a job applied to on the 1st is marked
rejected on the 20th, the applied date is gone -- "what did I apply to last week"
is unanswerable rather than merely unimplemented. `status_event` is an
append-only log alongside it; every question here is a query over that.

The privacy half matters as much as the analytics half. The corpus is published
to a public repo daily, so a dated log of every job the owner applied to must
never leave the machine.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest
from fastapi.testclient import TestClient

from src import store as S
from src.config import Config
from src.web.app import create_app

DAY = 86400


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


def _seed(conn, n=4):
    for i in range(n):
        native = str(5000 + i)
        S.upsert_job(conn, native, f"co{i}", {
            "id": native, "title": f"AI Engineer {i}",
            "liveStartAt": int(dt.datetime.now(dt.timezone.utc).timestamp()) - 2 * DAY,
            "startups": {"name": f"Co {i}"}}, source="wellfound")
        S.add_provenance(conn, S.job_uid("wellfound", native),
                         "artificial-intelligence-engineer", "anywhere", 1)
    conn.commit()
    return [S.job_uid("wellfound", str(5000 + i)) for i in range(n)]


def _events(conn, job_id=None):
    q = "SELECT source_job_id, status, at FROM status_event"
    p: tuple = ()
    if job_id:
        q += " WHERE source_job_id = ?"
        p = (job_id,)
    return [dict(r) for r in conn.execute(q + " ORDER BY id", p)]


# --- the history ---------------------------------------------------------------


def test_the_applied_date_survives_a_later_status_change(conn):
    """The bug the whole design exists to prevent.

    With only `user_state`, marking a job rejected overwrites `updated_at` and
    the date it was applied to is destroyed -- silently, and unrecoverably.
    """
    (uid, *_) = _seed(conn)
    S.set_status(conn, uid, "applied")
    conn.commit()
    applied_at = _events(conn, uid)[0]["at"]

    S.set_status(conn, uid, "rejected")
    conn.commit()

    evs = _events(conn, uid)
    assert [e["status"] for e in evs] == ["applied", "rejected"]
    assert evs[0]["at"] == applied_at, "the applied date was rewritten"


def test_the_log_is_append_only(conn):
    (uid, *_) = _seed(conn)
    for st in ("shortlisted", "applied", "interviewing"):
        S.set_status(conn, uid, st)
    conn.commit()
    assert [e["status"] for e in _events(conn, uid)] == \
        ["shortlisted", "applied", "interviewing"]


def test_current_state_still_lives_in_user_state(conn):
    """The event log is additive. Every existing filter, facet and view reads
    `user_state`, and none of them change."""
    (uid, *_) = _seed(conn)
    S.set_status(conn, uid, "applied")
    S.set_status(conn, uid, "rejected")
    conn.commit()
    row = conn.execute(
        "SELECT status FROM jobs WHERE source_job_id = ?", (uid,)).fetchone()
    assert row["status"] == "rejected"
    assert conn.execute(
        "SELECT COUNT(*) FROM user_state WHERE source_job_id = ?",
        (uid,)).fetchone()[0] == 1


def test_applied_in_the_last_7_days_counts_events_not_current_state(conn):
    """Counting `user_state` would miss anything since moved to rejected, which
    is exactly the week's most useful number."""
    uids = _seed(conn, n=3)
    for u in uids:
        S.set_status(conn, u, "applied")
    S.set_status(conn, uids[0], "rejected")          # still applied last week
    conn.commit()

    # Age one application out of the window.
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)).isoformat()
    conn.execute("UPDATE status_event SET at = ? WHERE source_job_id = ? "
                 "AND status = 'applied'", (old, uids[2]))
    conn.commit()

    assert S.applied_since(conn, 7) == 2
    assert S.applied_since(conn, 30) == 3


def test_applied_counts_a_job_once_however_often_it_is_remarked(conn):
    """Re-marking the same job applied must not inflate the count."""
    (uid, *_) = _seed(conn)
    for _ in range(3):
        S.set_status(conn, uid, "applied")
    conn.commit()
    assert S.applied_since(conn, 7) == 1


# --- the numbers ---------------------------------------------------------------


def test_stats_reports_the_funnel(conn):
    uids = _seed(conn, n=4)
    S.set_status(conn, uids[0], "shortlisted")
    S.set_status(conn, uids[1], "applied")
    S.set_status(conn, uids[2], "applied")
    S.set_status(conn, uids[2], "interviewing")
    S.set_status(conn, uids[3], "applied")
    S.set_status(conn, uids[3], "rejected")
    conn.commit()

    st = S.application_stats(conn)
    assert st["applied_7d"] == 3
    assert st["funnel"]["shortlisted"] == 1
    assert st["funnel"]["interviewing"] == 1
    assert st["funnel"]["rejected"] == 1
    # Two of three applications drew a reply of some kind.
    assert st["responded"] == 2
    assert st["applied_total"] == 3


def test_time_to_reply_is_measured_from_the_application(conn):
    """Both timestamps are pinned. Backdating only one leaves the other on the
    wall clock, so the gap comes out at 9.99999 days and the answer depends on
    how long the test took to run -- green alone, red in a full suite."""
    (uid, *_) = _seed(conn)
    S.set_status(conn, uid, "applied")
    S.set_status(conn, uid, "interviewing")
    conn.commit()
    base = dt.datetime.now(dt.timezone.utc)
    conn.execute("UPDATE status_event SET at = ? WHERE source_job_id = ? "
                 "AND status = 'applied'",
                 ((base - dt.timedelta(days=10)).isoformat(), uid))
    conn.execute("UPDATE status_event SET at = ? WHERE source_job_id = ? "
                 "AND status = 'interviewing'", (base.isoformat(), uid))
    conn.commit()
    assert S.application_stats(conn)["median_days_to_reply"] == 10


def test_a_same_day_reply_is_not_rounded_up_to_a_day(conn):
    """The other end of the rounding: a reply hours later is 0 days, not 1."""
    (uid, *_) = _seed(conn)
    S.set_status(conn, uid, "applied")
    S.set_status(conn, uid, "rejected")
    conn.commit()
    base = dt.datetime.now(dt.timezone.utc)
    conn.execute("UPDATE status_event SET at = ? WHERE status = 'applied'",
                 ((base - dt.timedelta(hours=5)).isoformat(),))
    conn.execute("UPDATE status_event SET at = ? WHERE status = 'rejected'",
                 (base.isoformat(),))
    conn.commit()
    assert S.application_stats(conn)["median_days_to_reply"] == 0


def test_stats_are_empty_not_broken_on_a_fresh_corpus(conn):
    _seed(conn)
    st = S.application_stats(conn)
    assert st["applied_7d"] == 0 and st["applied_total"] == 0
    assert st["median_days_to_reply"] is None


def test_the_stats_endpoint_returns_the_shape(conn, tmp_path, monkeypatch):
    uids = _seed(conn, n=2)
    S.set_status(conn, uids[0], "applied")
    conn.commit()
    monkeypatch.setenv("JOBSEARCH_DB", str(tmp_path / "t.db"))
    client = TestClient(create_app(Config.load()))
    body = client.get("/api/stats").json()
    for k in ("applied_7d", "applied_30d", "applied_total", "funnel",
              "responded", "median_days_to_reply", "by_source"):
        assert k in body, f"/api/stats is missing {k}"
    assert body["applied_7d"] == 1


# --- the new statuses ----------------------------------------------------------


@pytest.mark.parametrize("status", ["interviewing", "offer", "rejected"])
def test_the_api_accepts_the_outcome_statuses(conn, tmp_path, monkeypatch, status):
    (uid, *_) = _seed(conn)
    monkeypatch.setenv("JOBSEARCH_DB", str(tmp_path / "t.db"))
    client = TestClient(create_app(Config.load()))
    r = client.post("/api/status", json={"job_id": uid, "status": status})
    assert r.status_code == 200, r.text


def test_the_api_still_rejects_a_junk_status(conn, tmp_path, monkeypatch):
    (uid, *_) = _seed(conn)
    monkeypatch.setenv("JOBSEARCH_DB", str(tmp_path / "t.db"))
    client = TestClient(create_app(Config.load()))
    assert client.post("/api/status",
                       json={"job_id": uid, "status": "maybe"}).status_code == 400


def test_prune_keeps_a_job_you_are_interviewing_for(conn):
    """An active interview is the last thing that should age out at 30 days."""
    uids = _seed(conn, n=2)
    old = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)).timestamp())
    for u in uids:
        conn.execute("UPDATE job_raw SET raw_json = json_set(raw_json,'$.liveStartAt',?) "
                     "WHERE source_job_id = ?", (old, u))
    S.set_status(conn, uids[0], "interviewing")
    conn.commit()

    S.prune_stale(conn, 30, dry_run=False)
    kept = {r[0] for r in conn.execute("SELECT source_job_id FROM job_raw")}
    assert uids[0] in kept, "a job under interview was pruned"
    assert uids[1] not in kept


# --- the history must not be published ------------------------------------------


def test_export_publishes_no_event_history(tmp_path, monkeypatch):
    """The corpus goes to a public repo every day. A dated log of every job the
    owner applied to must never leave the machine."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = S.connect(local)
    uids = _seed(c, n=3)
    S.set_status(c, uids[0], "applied")
    S.set_status(c, uids[0], "rejected")
    c.commit()
    jobs_before = c.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0]
    c.close()

    out = tmp_path / "corpus.db"
    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    assert cli.main(["export", "--out", str(out)]) == 0

    raw = sqlite3.connect(str(out))          # plain sqlite3: no view, no helpers
    assert raw.execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == 0
    assert raw.execute("SELECT COUNT(*) FROM user_state").fetchone()[0] == 0
    assert raw.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == jobs_before
    raw.close()

    back = S.connect(local)
    assert len(_events(back)) == 2, "export damaged the local history"


def test_sync_carries_the_history_across_a_corpus_refresh(tmp_path, monkeypatch):
    """Without this, every refresh of the published corpus wipes the log."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = S.connect(local)
    uids = _seed(c, n=2)
    S.set_status(c, uids[0], "applied")
    S.set_status(c, uids[0], "interviewing")
    c.commit()
    c.close()

    incoming = tmp_path / "incoming.db"
    inc = S.connect(incoming)
    _seed(inc, n=2)
    inc.close()

    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    assert cli.main(["sync", str(incoming), "--apply"]) == 0

    back = S.connect(local)
    assert [e["status"] for e in _events(back, uids[0])] == ["applied", "interviewing"]


# --- migration ------------------------------------------------------------------


def test_existing_marks_are_backfilled_into_the_log(tmp_path):
    """Someone with marks already made should not open an empty panel."""
    p = tmp_path / "old.db"
    conn = S.connect(p)
    _seed(conn, n=1)
    uid = S.job_uid("wellfound", "5000")
    # A pre-migration row: user_state populated, no event, older schema version.
    conn.execute("DELETE FROM status_event")
    conn.execute("INSERT OR REPLACE INTO user_state VALUES (?,?,?,?)",
                 (uid, "applied", None, "2026-08-01T10:00:00+00:00"))
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    conn = S.connect(p)
    evs = _events(conn, uid)
    assert [e["status"] for e in evs] == ["applied"]
    assert evs[0]["at"].startswith("2026-08-01"), "backfill lost the original date"


def test_the_backfill_does_not_duplicate_on_reconnect(tmp_path):
    p = tmp_path / "again.db"
    conn = S.connect(p)
    _seed(conn, n=1)
    uid = S.job_uid("wellfound", "5000")
    S.set_status(conn, uid, "applied")
    conn.commit()
    conn.close()

    for _ in range(3):
        S.connect(p).close()

    conn = S.connect(p)
    assert len(_events(conn, uid)) == 1, "reconnecting duplicated the history"

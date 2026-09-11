"""Recording an application costs one click you were already making.

Marking a job applied used to mean coming back, finding the row again, and
picking one of six small buttons. Nobody does that, so the funnel stayed empty
and the analytics were fiction. Instead the Apply click -- which happens anyway --
records that the posting was OPENED, and the question comes to the user in a tray
rather than the user going back to the row.

`opened`, `skipped` and `nudged` live only in `status_event`. They never reach
`user_state`, so every existing filter, facet, view and the funnel itself are
untouched by this.
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


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBSEARCH_DB", str(tmp_path / "t.db"))
    return TestClient(create_app(Config.load()))


def _seed(conn, n=4):
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    for i in range(n):
        native = str(7100 + i)
        S.upsert_job(conn, native, f"co{i}", {
            "id": native, "title": f"AI Engineer {i}",
            "liveStartAt": now - 2 * DAY,
            "startups": {"name": f"Co {i}"}}, source="wellfound")
        S.add_provenance(conn, S.job_uid("wellfound", native),
                         "artificial-intelligence-engineer", "anywhere", 1)
    conn.commit()
    return [S.job_uid("wellfound", str(7100 + i)) for i in range(n)]


def _statuses(conn, uid):
    return [r[0] for r in conn.execute(
        "SELECT status FROM status_event WHERE source_job_id = ? ORDER BY id", (uid,))]


# --- the log-only writer --------------------------------------------------------


def test_record_event_does_not_touch_current_state(conn):
    """Opening a posting is not applying to it. If it moved `user_state` the row
    would change colour, leave the default view, and pollute the funnel."""
    (uid, *_) = _seed(conn)
    S.record_event(conn, uid, "opened")
    conn.commit()

    assert _statuses(conn, uid) == ["opened"]
    assert conn.execute("SELECT COUNT(*) FROM user_state").fetchone()[0] == 0
    assert conn.execute(
        "SELECT status FROM jobs WHERE source_job_id = ?", (uid,)).fetchone()[0] == "new"


def test_record_event_can_backdate(conn):
    """`at` exists so a confirmation can be filed under the day the posting was
    actually opened."""
    (uid, *_) = _seed(conn)
    when = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat()
    S.record_event(conn, uid, "applied", at=when)
    conn.commit()
    assert conn.execute(
        "SELECT at FROM status_event WHERE source_job_id = ?", (uid,)).fetchone()[0] == when


# --- the confirmation queue -----------------------------------------------------


def test_opening_a_job_puts_it_in_the_queue(conn):
    uids = _seed(conn)
    S.record_event(conn, uids[0], "opened")
    conn.commit()
    assert [p["source_job_id"] for p in S.pending_confirmations(conn)] == [uids[0]]


def test_confirming_yes_dates_the_application_at_the_open_time(conn):
    """Opened Monday, confirmed Thursday, is a Monday application.

    Dating it at confirmation would silently move it into the wrong week and
    make "applied this week" wrong in the user's favour.
    """
    (uid, *_) = _seed(conn)
    opened = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=4)).isoformat()
    S.record_event(conn, uid, "opened", at=opened)
    conn.commit()

    S.confirm_application(conn, uid, applied=True)
    conn.commit()

    at = conn.execute("SELECT at FROM status_event WHERE source_job_id = ? "
                      "AND status = 'applied'", (uid,)).fetchone()[0]
    assert at == opened, "the application was re-dated to the confirmation"
    assert S.applied_since(conn, 7) == 1
    assert S.applied_since(conn, 2) == 0


def test_confirming_yes_sets_the_current_state(conn):
    (uid, *_) = _seed(conn)
    S.record_event(conn, uid, "opened")
    conn.commit()
    S.confirm_application(conn, uid, applied=True)
    conn.commit()
    assert conn.execute(
        "SELECT status FROM jobs WHERE source_job_id = ?", (uid,)).fetchone()[0] == "applied"


def test_confirming_no_records_a_skip_and_no_application(conn):
    (uid, *_) = _seed(conn)
    S.record_event(conn, uid, "opened")
    conn.commit()
    S.confirm_application(conn, uid, applied=False)
    conn.commit()

    assert "applied" not in _statuses(conn, uid)
    assert "skipped" in _statuses(conn, uid)
    assert S.applied_since(conn, 7) == 0


@pytest.mark.parametrize("applied", [True, False])
def test_an_answered_job_leaves_the_queue(conn, applied):
    uids = _seed(conn)
    for u in uids[:2]:
        S.record_event(conn, u, "opened")
    conn.commit()
    S.confirm_application(conn, uids[0], applied=applied)
    conn.commit()
    assert [p["source_job_id"] for p in S.pending_confirmations(conn)] == [uids[1]]


def test_reopening_an_answered_job_asks_again(conn):
    """Opened again after being skipped is a fresh decision, not a stale one."""
    (uid, *_) = _seed(conn)
    S.record_event(conn, uid, "opened")
    S.confirm_application(conn, uid, applied=False)
    conn.commit()
    assert S.pending_confirmations(conn) == []

    S.record_event(conn, uid, "opened")
    conn.commit()
    assert [p["source_job_id"] for p in S.pending_confirmations(conn)] == [uid]


def test_the_queue_carries_enough_to_show_the_job(conn):
    """The tray exists so the row never has to be found. It must therefore say
    which job it is asking about."""
    (uid, *_) = _seed(conn)
    S.record_event(conn, uid, "opened")
    conn.commit()
    p = S.pending_confirmations(conn)[0]
    for k in ("source_job_id", "title", "company", "at", "apply_url"):
        assert k in p, f"pending row is missing {k}"


# --- the follow-up nudge ---------------------------------------------------------


def _age_event(conn, uid, status, days):
    when = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    conn.execute("UPDATE status_event SET at = ? WHERE source_job_id = ? AND status = ?",
                 (when, uid, status))
    conn.commit()


def test_a_fresh_application_is_not_nudged(conn):
    (uid, *_) = _seed(conn)
    S.set_status(conn, uid, "applied")
    conn.commit()
    assert S.pending_followups(conn, 10) == []


def test_an_old_application_with_no_outcome_is_nudged(conn):
    (uid, *_) = _seed(conn)
    S.set_status(conn, uid, "applied")
    _age_event(conn, uid, "applied", 30)
    assert [p["source_job_id"] for p in S.pending_followups(conn, 10)] == [uid]


def test_an_application_that_already_has_an_outcome_is_not_nudged(conn):
    (uid, *_) = _seed(conn)
    S.set_status(conn, uid, "applied")
    _age_event(conn, uid, "applied", 30)
    S.set_status(conn, uid, "rejected")
    conn.commit()
    assert S.pending_followups(conn, 10) == []


def test_not_yet_snoozes_the_nudge(conn):
    (uid, *_) = _seed(conn)
    S.set_status(conn, uid, "applied")
    _age_event(conn, uid, "applied", 30)
    assert S.pending_followups(conn, 10)

    S.record_event(conn, uid, "nudged")
    conn.commit()
    assert S.pending_followups(conn, 10) == [], "snoozing did not quiet the nudge"

    _age_event(conn, uid, "nudged", 30)
    assert [p["source_job_id"] for p in S.pending_followups(conn, 10)] == [uid], \
        "the snooze never expired"


# --- the event-only statuses stay out of the funnel -------------------------------


def test_the_workflow_statuses_never_reach_user_state_or_the_funnel(conn):
    uids = _seed(conn)
    S.record_event(conn, uids[0], "opened")
    S.record_event(conn, uids[1], "skipped")
    S.record_event(conn, uids[2], "nudged")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM user_state").fetchone()[0] == 0
    assert set(S.application_stats(conn)["funnel"]) == set(S.FUNNEL)
    assert S.application_stats(conn)["applied_total"] == 0


def test_export_strips_the_workflow_events_too(tmp_path, monkeypatch):
    """The corpus is published publicly. Which postings were opened and when is
    no less revealing than which were applied to."""
    from src import cli

    local = tmp_path / "jobs.db"
    c = S.connect(local)
    uids = _seed(c)
    S.record_event(c, uids[0], "opened")
    S.confirm_application(c, uids[0], applied=True)
    c.commit()
    c.close()

    out = tmp_path / "corpus.db"
    monkeypatch.setenv("JOBSEARCH_DB", str(local))
    assert cli.main(["export", "--out", str(out)]) == 0

    raw = sqlite3.connect(str(out))
    assert raw.execute("SELECT COUNT(*) FROM status_event").fetchone()[0] == 0
    raw.close()


# --- the endpoints ----------------------------------------------------------------


def test_the_open_endpoint_records_and_queues(conn, client):
    (uid, *_) = _seed(conn)
    assert client.post("/api/event", json={"job_id": uid}).status_code == 200
    body = client.get("/api/pending").json()
    assert [p["source_job_id"] for p in body["confirm"]] == [uid]
    assert body["followup"] == []


def test_the_confirm_endpoint_answers_the_question(conn, client):
    (uid, *_) = _seed(conn)
    client.post("/api/event", json={"job_id": uid})
    assert client.post("/api/confirm",
                       json={"job_id": uid, "applied": True}).status_code == 200
    assert client.get("/api/pending").json()["confirm"] == []
    assert client.get("/api/stats").json()["applied_total"] == 1


def test_the_confirm_endpoint_rejects_an_unknown_job(conn, client):
    _seed(conn)
    r = client.post("/api/confirm", json={"job_id": "nope:1", "applied": True})
    assert r.status_code == 404


def test_the_event_endpoint_records_a_snooze_not_an_open(conn, client):
    """The tray's "Not yet" button. Filing it as an `opened` event would push
    the job back into the confirm queue -- asking "did you apply?" about
    something already applied to."""
    (uid, *_) = _seed(conn)
    S.set_status(conn, uid, "applied")
    _age_event(conn, uid, "applied", 30)

    assert client.get("/api/pending").json()["followup"], "no nudge to answer"
    assert client.post("/api/event",
                       json={"job_id": uid, "status": "nudged"}).status_code == 200

    body = client.get("/api/pending").json()
    assert body["followup"] == [], "the snooze did not quiet the nudge"
    assert body["confirm"] == [], "the snooze was filed as an open"


def test_the_event_endpoint_refuses_a_funnel_status(conn, client):
    """Only workflow statuses go through here. `applied` must travel via
    /api/confirm or /api/status so current state is set too."""
    (uid, *_) = _seed(conn)
    assert client.post("/api/event",
                       json={"job_id": uid, "status": "applied"}).status_code == 400

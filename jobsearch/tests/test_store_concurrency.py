"""store.connect() must be safe to call repeatedly and concurrently.

The web app opens a connection per request and the UI fires /api/jobs and
/api/facets without awaiting either, so FastAPI runs them in parallel threads.
Views are database-global, not per-connection: two connections that both drop
and recreate `jobs_core` race, and the loser dies with "view jobs_core already
exists". That was a real 500 on every page load.

The fix is to rebuild the views only when they have actually drifted. These
tests pin both halves of that: the fast path must not race, and it must still
rebuild when a rebuild is genuinely due.
"""

import sqlite3
import threading

import pytest

from src import sources as SRC
from src import store as S


def _view_names(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'")}


def test_concurrent_connect_is_safe(tmp_path):
    """Eight threads through a barrier onto one database file.

    Before the fix this failed 6/8 with OperationalError: view jobs_core
    already exists.
    """
    p = tmp_path / "race.db"
    S.connect(p).close()                      # cold create, single-threaded

    errors: list[str] = []
    n = 8
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()                        # maximise the overlap
        try:
            S.connect(p).close()
        except Exception as e:                # noqa: BLE001 -- reporting, not handling
            errors.append(f"{type(e).__name__}: {e}")

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"{len(errors)}/{n} concurrent connects failed: {set(errors)}"


def test_concurrent_connect_on_a_cold_database(tmp_path):
    """The same race, but with nothing pre-created -- every thread arrives
    needing the full schema build, not just the fast path.

    Repeated across independent databases on purpose. This is the only test
    that exercises _SCHEMA_LOCK (once a database is warm, the staleness check
    alone keeps every thread out of the DDL), and a single round caught a
    removed lock only about 4 times in 10. Twenty rounds turn a coin-flip into
    a guard.
    """
    errors: list[str] = []
    n = 8

    for round_no in range(20):
        p = tmp_path / f"cold{round_no}.db"
        barrier = threading.Barrier(n)

        def worker(path=p, b=barrier):
            b.wait()                          # maximise the overlap
            try:
                S.connect(path).close()
            except Exception as e:            # noqa: BLE001 -- reporting
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        conn = S.connect(p)
        assert {"jobs", "jobs_core"} <= _view_names(conn)
        conn.close()

    assert errors == [], (
        f"{len(errors)} of {n * 20} cold connects failed: {set(errors)}")


def test_views_rebuilt_when_missing(tmp_path):
    """Skipping the rebuild must not mean skipping a rebuild that is due."""
    p = tmp_path / "drift.db"
    conn = S.connect(p)
    conn.execute("DROP VIEW jobs")
    conn.execute("DROP VIEW jobs_core")
    conn.commit()
    conn.close()

    conn = S.connect(p)
    assert {"jobs", "jobs_core"} <= _view_names(conn)
    SRC.assert_core_views(conn)
    conn.execute("SELECT * FROM jobs LIMIT 1").fetchall()


def test_views_rebuilt_when_definition_drifts(tmp_path):
    """A view whose stored SQL no longer matches the registry is stale, even
    though it exists and queries fine. This is what happens when a source's
    CORE_VIEW_SQL changes under an existing database."""
    p = tmp_path / "stale.db"
    S.connect(p).close()

    raw = sqlite3.connect(str(p))
    raw.executescript(
        "DROP VIEW jobs; DROP VIEW jobs_core;"
        "CREATE VIEW jobs_core AS SELECT 1 AS source;"
    )
    raw.commit()
    raw.close()

    conn = S.connect(p)
    got = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='jobs_core'"
    ).fetchone()[0]
    assert "SELECT 1 AS source" not in got, "stale view was not rebuilt"
    SRC.assert_core_views(conn)


def test_migration_runs_even_when_views_look_current(tmp_path):
    """The ordering trap.

    Views must be dropped before migrate(), because a migration that rebuilds a
    table cannot DROP it while a view still references it. A database can be
    view-current and migration-pending at the same time, so the rebuild branch
    has to trigger on either condition, not just on view drift.
    """
    p = tmp_path / "pending.db"
    conn = S.connect(p)
    S.upsert_job(conn, "4235671", "acme", {"title": "AI Engineer"},
                 source="wellfound")
    conn.commit()
    conn.close()

    # Rewind to a pre-v2 state while leaving the (current) views in place.
    raw = sqlite3.connect(str(p))
    raw.execute("UPDATE job_raw SET source_job_id = native_id")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    conn = S.connect(p)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == S.SCHEMA_VERSION
    assert conn.execute(
        "SELECT source_job_id FROM job_raw").fetchone()[0] == "wellfound:4235671"


def test_repeated_connect_leaves_the_schema_identical(tmp_path):
    """Ten sequential connects must converge, not accumulate drift."""
    p = tmp_path / "repeat.db"
    def schema(c):
        return sorted(tuple(r) for r in c.execute(
            "SELECT name, sql FROM sqlite_master"))

    conn = S.connect(p)
    first = schema(conn)
    conn.close()

    for _ in range(10):
        conn = S.connect(p)
        conn.close()

    conn = S.connect(p)
    assert schema(conn) == first

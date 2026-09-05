"""Retroactively apply the role filter to rows that predate it.

The bug this exists for: jobs ingested before ADR-009 carry provenance `all`,
`dev` or `engineer` and were never role-filtered at all. Measured on the live
corpus 2026-09-06: **661 jobs** reachable only via those legacy tokens, holding
JANITOR, Kitchen Technician, Joiner, Carpenter and Data Entry Clerk.

The subtle part is the matching rule, and getting it wrong is easy: a first
attempt at this backfill matched RemoteOK rows against their `badges`, which
ARE RemoteOK's own tags, and duly concluded that JANITOR was a
`software-engineer` -- because that row genuinely carries the tag `engineer`.
That is the exact trap AGENTS.md documents. The backfill must use the SAME rule
each source uses live: title-only for RemoteOK, title+categories elsewhere.
"""

from __future__ import annotations

import pytest

from src import store as S

# Two roles, mirroring targets.yaml's shape.
KEYWORDS = {
    "software-engineer": ["software engineer", "developer", "engineer"],
    "machine-learning-engineer": ["machine learning", "mlops"],
}


@pytest.fixture
def conn(tmp_path):
    return S.connect(tmp_path / "t.db")


def _legacy(conn, source, nid, title, tags=None, role="all"):
    """A row as it looked before ADR-009: real job, legacy provenance token."""
    S.upsert_company(conn, "acme", {"name": "Acme"}, source=source)
    if source == "remoteok":
        raw = {"id": nid, "position": title, "company": "Acme",
               "tags": tags or [], "date": "2026-09-01T00:00:00+00:00",
               "url": "https://remoteok.com/l/1"}
    else:
        raw = {"guid": nid, "title": title, "companyName": "Acme",
               "categories": tags or [], "pubDate": "2026-09-01T00:00:00Z",
               "applicationLink": "https://x.test"}
    S.upsert_job(conn, nid, "acme", raw, source=source)
    S.add_provenance(conn, S.job_uid(source, nid), role, "feed", 1)
    conn.commit()


def test_remoteok_is_matched_on_title_only(conn):
    """THE TRAP. This row's title is JANITOR and its RemoteOK tag is 'engineer'.
    Matching the tag would keep a janitor as a software engineer."""
    _legacy(conn, "remoteok", "1", "JANITOR", tags=["engineer", "remote"])
    out = S.reconcile_roles(conn, KEYWORDS, dry_run=True)
    assert out["would_remove"] == 1, "matched a janitor via RemoteOK's own tag"
    assert out["would_retag"] == 0


def test_a_genuinely_relevant_legacy_row_is_kept_and_retagged(conn):
    """It must not be a blunt delete: real jobs arrived through the broad feeds
    too, and they should join the real role vocabulary rather than vanish."""
    _legacy(conn, "remoteok", "2", "Principal Engineer")
    out = S.reconcile_roles(conn, KEYWORDS, dry_run=False)
    assert out["retagged"] == 1 and out["removed"] == 0
    r = conn.execute("SELECT found_via_roles FROM jobs").fetchone()
    assert "software-engineer" in r["found_via_roles"]


def test_himalayas_still_matches_on_categories(conn):
    """Himalayas' categories are real taxonomy, not user-supplied tags, so they
    ARE matched -- the asymmetry with RemoteOK is deliberate."""
    _legacy(conn, "himalayas", "3", "Applied Scientist",
            tags=["Machine-Learning"])
    out = S.reconcile_roles(conn, KEYWORDS, dry_run=True)
    assert out["would_retag"] == 1 and out["would_remove"] == 0


def test_rows_with_a_real_role_are_never_touched(conn):
    """Only rows whose provenance is ENTIRELY legacy are in scope. A job also
    found via a configured role has already been filtered properly."""
    _legacy(conn, "remoteok", "4", "JANITOR", tags=["engineer"])
    S.add_provenance(conn, S.job_uid("remoteok", "4"),
                     "software-engineer", "feed", 1)
    conn.commit()
    out = S.reconcile_roles(conn, KEYWORDS, dry_run=True)
    assert out["would_remove"] == 0 and out["would_retag"] == 0


def test_a_wellfound_row_under_a_retired_slug_is_never_touched(conn):
    """The near-miss this test exists for.

    `ai-engineer` and `data-engineer` are Wellfound slugs from earlier crawls.
    Wellfound filtered those server-side at crawl time (ADR-009), so the rows
    are legitimate -- the slug simply is not in today's targets.yaml. Judging
    them by "has no configured role slug" put 2,478 properly-filtered jobs in
    scope and would have deleted 598 of them by re-testing against a much
    cruder keyword list than the server already applied.

    Only sources that CANNOT filter server-side are ever in scope.
    """
    S.upsert_company(conn, "acme", {"name": "Acme"}, source="wellfound")
    S.upsert_job(conn, "w1", "acme",
                 {"title": "Staff Data Platform Lead", "slug": "s"},
                 source="wellfound")
    S.add_provenance(conn, S.job_uid("wellfound", "w1"),
                     "data-engineer", "anywhere", 1)
    conn.commit()
    out = S.reconcile_roles(conn, KEYWORDS, dry_run=True)
    assert out["scanned"] == 0, "a server-side-filtered Wellfound row was in scope"
    assert out["would_remove"] == 0


def test_ats_rows_are_out_of_scope_too(conn):
    """ATS boards apply the current keywords locally at expand time, so their
    rows are already filtered by the same rule this would re-apply."""
    S.upsert_company(conn, "acme", {"name": "Acme"}, source="greenhouse")
    S.upsert_job(conn, "acme/1", "acme",
                 {"id": 1, "title": "Office Manager", "company_name": "Acme"},
                 source="greenhouse")
    S.add_provenance(conn, S.job_uid("greenhouse", "acme/1"),
                     "some-old-role", "board", 1)
    conn.commit()
    assert S.reconcile_roles(conn, KEYWORDS, dry_run=True)["scanned"] == 0


def test_dry_run_changes_nothing(conn):
    _legacy(conn, "remoteok", "5", "Data Entry Clerk")
    before = conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0]
    out = S.reconcile_roles(conn, KEYWORDS, dry_run=True)
    assert out["would_remove"] == 1 and out["removed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == before


def test_apply_removes_the_row_and_its_derived_rows(conn):
    _legacy(conn, "remoteok", "6", "Store Manager")
    S.reconcile_roles(conn, KEYWORDS, dry_run=False)
    for t in ("job_raw", "job_provenance", "job_location"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t


def test_a_job_you_acted_on_is_never_deleted(conn):
    """user_state is the only non-regenerable data here. A job marked applied
    must survive a cleanup even if it no longer matches any role -- losing it
    would destroy the record of having applied."""
    _legacy(conn, "remoteok", "7", "Data Entry Clerk")
    conn.execute("INSERT INTO user_state (source_job_id,status) VALUES (?,?)",
                 (S.job_uid("remoteok", "7"), "applied"))
    conn.commit()
    out = S.reconcile_roles(conn, KEYWORDS, dry_run=False)
    assert out["removed"] == 0 and out["protected"] == 1
    assert conn.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == 1


def test_no_keywords_configured_is_a_no_op(conn):
    """A misconfigured role map must never be read as "nothing matches, delete
    everything" -- the failure mode would wipe the corpus."""
    _legacy(conn, "remoteok", "8", "Data Entry Clerk")
    out = S.reconcile_roles(conn, {}, dry_run=True)
    assert out["would_remove"] == 0 and out["scanned"] == 0

"""Derived columns, storage round-trip, and filter SQL. No network."""

from __future__ import annotations

import json

import pytest

from src import store as S
from src.derive import (equity_bounds, remote_label, salary_bounds, size_label,
                        size_min)
from src.enrich import parse_detail
from src.parse import parse_search_page
from src.web.app import Filters, _where

from . import fixtures as F


@pytest.mark.parametrize("raw,expect", [
    ("$25k – $50k", (25000, 50000)),
    ("$120k - $200k", (120000, 200000)),
    ("₹15L – ₹25L", (1500000, 2500000)),
    ("₹1.2Cr", (12000000, 12000000)),
    ("$90k", (90000, 90000)),
    ("50,000 - 70,000", (50000, 70000)),
    ("$25k – $50k • 0.0% – 1.0%", (25000, 50000)),
    ("0.0% – 1.0%", (None, None)),      # equity, not salary
    ("", (None, None)),
    (None, (None, None)),
    ("competitive", (None, None)),      # refuse to guess
])
def test_salary_bounds(raw, expect):
    assert salary_bounds(raw) == expect


def test_equity_bounds():
    assert equity_bounds("0.0% – 1.0%") == (0.0, 1.0)
    assert equity_bounds("0.05% – 0.2%") == (0.05, 0.2)
    assert equity_bounds("$25k") == (None, None)


def test_size_helpers():
    assert size_label("SIZE_11_50") == "11-50"
    assert size_label("SIZE_5000_PLUS") == "5000+"
    assert size_min("SIZE_1001_5000") == 1001
    assert size_label(None) is None


def test_remote_label_is_consistent_across_both_paths():
    """kind and the bare boolean must produce the SAME vocabulary, or the UI
    grows duplicate half-populated filter options."""
    assert remote_label("ONSITE", 0) == "Onsite"
    assert remote_label(None, 0) == "Onsite"
    assert remote_label("REMOTE", 1) == "Remote"
    assert remote_label(None, 1) == "Remote"
    assert remote_label("ONSITE_OR_REMOTE", 1) == "Onsite or Remote"


@pytest.fixture
def db(tmp_path):
    conn = S.connect(tmp_path / "t.db")
    page = parse_search_page(F.page(n_companies=2, jobs_per_company=2), "u")
    for startup, jobs in page.startups:
        S.upsert_company(conn, startup["slug"], startup)
        for jid, raw in jobs:
            S.upsert_job(conn, jid, startup["slug"], raw)
            S.add_provenance(conn, S.job_uid("wellfound", jid),
                             "ai-engineer", "bangalore", 1)
    conn.commit()
    return conn


def test_view_exposes_derived_columns(db):
    r = db.execute("SELECT * FROM jobs LIMIT 1").fetchone()
    assert r["salary_min"] == 25000 and r["salary_max"] == 50000
    assert r["company_size"] == "11-50"
    assert r["remote_label"] == "Onsite"
    assert r["status"] == "new"
    assert r["apply_url"].startswith("https://wellfound.com/jobs/")
    assert r["found_via_roles"] == "ai-engineer"


def test_dedupe_and_provenance(db):
    """The same job seen via a second slice is one row, two provenance rows."""
    page = parse_search_page(F.page(n_companies=2, jobs_per_company=2), "u")
    before = db.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0]
    for startup, jobs in page.startups:
        for jid, raw in jobs:
            S.upsert_job(db, jid, startup["slug"], raw)
            S.add_provenance(db, S.job_uid("wellfound", jid),
                             "data-engineer", "remote", 1)
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM job_raw").fetchone()[0] == before
    r = db.execute("SELECT found_via_roles, n_slices FROM jobs LIMIT 1").fetchone()
    assert r["n_slices"] == 2
    assert "data-engineer" in r["found_via_roles"]


def test_status_survives_rewrite(db):
    """Re-crawling rewrites job_raw; user_state must be untouched."""
    jid = db.execute("SELECT source_job_id FROM jobs LIMIT 1").fetchone()[0]
    S.set_status(db, jid, "applied")
    db.commit()
    S.upsert_job(db, jid, "company-s10", {"title": "changed", "slug": "x"})
    db.commit()
    r = db.execute("SELECT status FROM jobs WHERE source_job_id=?", (jid,)).fetchone()
    assert r["status"] == "applied"


def test_empty_salary_is_null_not_empty_string(tmp_path):
    """Wellfound sends '' for 'no salary'; the view must normalise it to NULL
    or every has-salary filter and fill rate is wrong."""
    conn = S.connect(tmp_path / "t2.db")
    S.upsert_company(conn, "c", {"name": "C", "companySize": "SIZE_1_10"})
    S.upsert_job(conn, "1", "c", {"title": "T", "slug": "t", "compensation": ""})
    conn.commit()
    r = conn.execute("SELECT salary_raw FROM jobs").fetchone()
    assert r["salary_raw"] is None


def test_where_is_fully_parameterised():
    where, params = _where(Filters(q="x'; DROP TABLE job_raw;--", roles=["a", "b"],
                                   salary_min=100000, max_days_old=7))
    assert "DROP TABLE" not in where
    assert where.count("?") == len(params)


def test_hidden_excluded_by_default():
    where, _ = _where(Filters())
    assert "status != 'hidden'" in where
    where2, _ = _where(Filters(show_hidden=True))
    assert "status != 'hidden'" not in where2


def test_include_unlisted_salary_changes_sql():
    strict, _ = _where(Filters(salary_min=100000, include_unlisted_salary=False))
    loose, _ = _where(Filters(salary_min=100000, include_unlisted_salary=True))
    assert "salary_raw IS NULL" in loose
    assert "salary_raw IS NULL" not in strict


def test_detail_parse_recovers_equity_and_ignores_ld_identifier():
    d = parse_detail(F.detail_page())
    assert d["equity_raw"] == "0.0% – 1.0%"
    assert d["salary_raw"].startswith("$25k")
    assert d["has_jobposting"]
    # identifier is the company name; it must never become a job id
    assert "Smart Audit" not in str(d.get("source_job_id", ""))


def test_detail_parse_without_jsonld():
    d = parse_detail(F.detail_page(with_jsonld=False))
    assert d["has_jobposting"] is False
    assert d["equity_raw"] == "0.0% – 1.0%"   # still recoverable from rendered text

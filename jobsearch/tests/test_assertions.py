"""The four silent-failure modes must RAISE, not warn. No network."""

from __future__ import annotations

import pytest

from src import assertions as A
from src.parse import parse_search_page

from . import fixtures as F


def test_valid_page_parses():
    p = parse_search_page(F.page(n_companies=3, jobs_per_company=2), "u")
    assert p.n_jobs == 6
    assert p.n_companies == 3
    assert p.args["role"] == "ai-engineer"
    assert p.total_job_count == 215
    A.check_role_applied(p.args, "ai-engineer", "u")
    A.check_page(p.args, 1, "u")


def test_silent_role_fallback_raises():
    """A bad slug serves a location-wide search: 200, no 'role' in the args."""
    p = parse_search_page(F.page(role=None), "u")
    assert "role" not in p.args
    with pytest.raises(A.SilentRoleFallback):
        A.check_role_applied(p.args, "artificial-intelligence", "u")


def test_role_mismatch_raises():
    p = parse_search_page(F.page(role="data-engineer"), "u")
    with pytest.raises(A.SilentRoleFallback):
        A.check_role_applied(p.args, "ai-engineer", "u")


def test_page_wrap_raises():
    """Requesting page 16 and being served page 1 must stop the slice."""
    p = parse_search_page(F.page(page_no=1), "u")
    with pytest.raises(A.PageWrap):
        A.check_page(p.args, 16, "u")


def test_page_match_ok():
    p = parse_search_page(F.page(page_no=7), "u")
    A.check_page(p.args, 7, "u")


def test_yield_floor_raises_after_a_full_page():
    with pytest.raises(A.YieldFloor):
        A.check_yield(0, 1, "u", prev_yield=40)


def test_yield_floor_allows_natural_end():
    """0 jobs after a thin page is exhaustion, not a broken parse."""
    A.check_yield(0, 1, "u", prev_yield=None)
    A.check_yield(0, 1, "u", prev_yield=2)


def test_schema_drift_app_router():
    with pytest.raises(A.SchemaDrift, match="App Router"):
        parse_search_page(F.app_router_page(), "u")


def test_schema_drift_unknown_shape():
    with pytest.raises(A.SchemaDrift):
        parse_search_page(F.unknown_page(), "u")


@pytest.mark.parametrize("status", [403, 429, 503])
def test_mitigation_on_block_status(status):
    with pytest.raises(A.MitigationDetected):
        A.check_mitigation(status, {"cf-ray": "abc"}, "u", "blocked")


def test_mitigation_on_cf_header():
    with pytest.raises(A.MitigationDetected):
        A.check_mitigation(200, {"cf-mitigated": "challenge"}, "u")


def test_no_mitigation_on_clean_200():
    A.check_mitigation(200, {"cf-ray": "abc"}, "u")


def test_remote_page_args():
    p = parse_search_page(F.page(location=None, remote=True), "u")
    assert p.args["remote"] is True
    A.check_role_applied(p.args, "ai-engineer", "u")

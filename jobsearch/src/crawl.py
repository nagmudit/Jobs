"""Slice crawl loop: breadth tier.

One (role, location) slice is walked page by page until it ends. A slice ends
for exactly one of four reasons, all recorded in slice_stats.ended_reason:

    exhausted  -- a page returned no jobs; the natural end
    page_wrap  -- the server re-served page 1; the real end for big slices
    max_pages  -- our own safety stop from targets.yaml
    mitigated  -- Cloudflare acted on us; the whole crawl halts here

Each (role, location) slice is capped by Wellfound at roughly 15 pages / ~300
companies regardless of what totalJobCount claims. Slicing is therefore how the
cap is DEFEATED, not how the search is narrowed: overlapping slices each get
their own budget and dedupe handles the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import assertions as A
from . import store as S
from .fetch import Fetcher, search_url
from .parse import parse_search_page


@dataclass
class SliceResult:
    role: str
    location: str
    total_claimed: int | None = None
    companies_claimed: int | None = None
    pages_walked: int = 0
    jobs_seen: int = 0
    jobs_new: int = 0
    companies_seen: int = 0
    ended_reason: str = "unknown"
    pages: list[dict] = field(default_factory=list)

    @property
    def recovery_ratio(self) -> float | None:
        """jobs_seen / total_claimed. Low ratios mark slices that need
        sub-partitioning -- surfaced in the UI."""
        if not self.total_claimed:
            return None
        return round(self.jobs_seen / self.total_claimed, 3)


def validate_role(fetcher: Fetcher, conn, role: str, location: str) -> dict[str, Any]:
    """Confirm the server actually applies the role filter before crawling it.

    An invalid slug does not 404 -- it silently serves an unfiltered
    location-wide search. Refusing to proceed is the whole point.
    """
    url = search_url(role, location, 1)
    resp = fetcher.get(url)
    rec = {"role": role, "location": location, "url": url, "valid": False,
           "total_job_count": None, "reason": None}
    if resp.status == 404:
        rec["reason"] = "HTTP 404 -- role slug does not exist"
        return rec
    if not resp.ok:
        rec["reason"] = f"HTTP {resp.status}"
        return rec

    page = parse_search_page(resp.text, url)
    rec["total_job_count"] = page.total_job_count
    rec["page_type"] = page.page_type
    try:
        A.check_role_applied(page.args, role, url)
    except A.SilentRoleFallback as e:
        rec["reason"] = str(e)
        return rec
    rec["valid"] = True
    return rec


def crawl_slice(
    fetcher: Fetcher,
    conn,
    role: str,
    location: str,
    max_pages: int,
    yield_floor: int = 1,
    resume: bool = True,
    run_at: str | None = None,
    on_page: Callable[[dict], None] | None = None,
) -> SliceResult:
    run_at = run_at or S.now()
    slice_key = f"{role}|{location}"
    res = SliceResult(role=role, location=location)
    prev_yield: int | None = None
    seen_companies: set[str] = set()

    for page_no in range(1, max_pages + 1):
        url = search_url(role, location, page_no)

        if resume and S.page_done(conn, slice_key, page_no):
            # Already ingested in a previous run; skip the request entirely.
            res.pages_walked += 1
            continue

        resp = fetcher.get(url)      # raises MitigationDetected -> caller halts
        if not resp.ok:
            res.ended_reason = f"http_{resp.status}"
            break

        page = parse_search_page(resp.text, url)

        # Ground truth checks, in order of severity.
        A.check_role_applied(page.args, role, url)
        try:
            A.check_page(page.args, page_no, url)
        except A.PageWrap:
            res.ended_reason = "page_wrap"
            break

        n = page.n_jobs
        A.check_yield(n, yield_floor, url, prev_yield)
        if n == 0:
            res.ended_reason = "exhausted"
            S.mark_page(conn, slice_key, page_no, "empty", 0)
            conn.commit()
            break

        if res.total_claimed is None:
            res.total_claimed = page.total_job_count
            res.companies_claimed = page.total_startup_count

        for startup, jobs in page.startups:
            cslug = startup.get("slug")
            if not cslug:
                continue
            S.upsert_company(conn, cslug, startup)
            seen_companies.add(cslug)
            for jid, jraw in jobs:
                if S.upsert_job(conn, jid, cslug, jraw):
                    res.jobs_new += 1
                S.add_provenance(conn, jid, role, location, page_no)
                res.jobs_seen += 1

        S.mark_page(conn, slice_key, page_no, "done", n)
        conn.commit()

        res.pages_walked += 1
        res.pages.append({"page": page_no, "jobs": n, "companies": page.n_companies})
        prev_yield = n
        if on_page:
            on_page({"role": role, "location": location, "page": page_no,
                     "jobs": n, "companies": page.n_companies})
    else:
        res.ended_reason = "max_pages"

    if res.ended_reason == "unknown":
        res.ended_reason = "exhausted"

    # Count from provenance rather than from this run's loop. On a resumed run
    # the already-ingested pages are skipped, so the in-loop counter would
    # under-report and make recovery_ratio look like truncation that isn't there.
    res.jobs_seen = conn.execute(
        "SELECT COUNT(DISTINCT source_job_id) FROM job_provenance"
        " WHERE role_slug=? AND location=?", (role, location)).fetchone()[0]
    res.companies_seen = conn.execute(
        "SELECT COUNT(DISTINCT j.company_slug) FROM job_raw j"
        " JOIN job_provenance p ON p.source_job_id = j.source_job_id"
        " WHERE p.role_slug=? AND p.location=?", (role, location)).fetchone()[0]
    if res.total_claimed is None:
        # Resumed slice where page 1 was skipped: recover the claim from the
        # last recorded run so the ratio stays meaningful.
        prev = conn.execute(
            "SELECT total_claimed, companies_claimed FROM slice_stats"
            " WHERE role_slug=? AND location=? AND total_claimed IS NOT NULL"
            " ORDER BY run_at DESC LIMIT 1", (role, location)).fetchone()
        if prev:
            res.total_claimed = prev["total_claimed"]
            res.companies_claimed = prev["companies_claimed"]

    S.record_slice(conn, role, location, res.total_claimed, res.companies_claimed,
                   res.pages_walked, res.jobs_seen, res.ended_reason, run_at)
    conn.commit()
    return res


def crawl(
    fetcher: Fetcher,
    conn,
    slices: list[tuple[str, str]],
    max_pages: int,
    yield_floor: int = 1,
    resume: bool = True,
    validate: bool = True,
    verbose: bool = True,
) -> dict[str, Any]:
    """Walk every slice. Halts the whole crawl on the first mitigation."""
    run_at = S.now()
    results: list[SliceResult] = []
    halted: str | None = None

    for role, location in slices:
        if validate:
            v = validate_role(fetcher, conn, role, location)
            conn.execute(
                "INSERT INTO role_slug (role_slug,valid,total_job_count,location_probed,"
                "reason,checked_at) VALUES (?,?,?,?,?,?) ON CONFLICT(role_slug) DO UPDATE "
                "SET valid=excluded.valid, total_job_count=excluded.total_job_count, "
                "location_probed=excluded.location_probed, reason=excluded.reason, "
                "checked_at=excluded.checked_at",
                (role, int(v["valid"]), v["total_job_count"], location, v["reason"], S.now()))
            conn.commit()
            if not v["valid"]:
                # Never silently fall back to an unfiltered search.
                raise A.SilentRoleFallback(
                    f"refusing to crawl {role!r}@{location!r}: {v['reason']}"
                )

        if verbose:
            print(f"\n  slice {role} @ {location}")
        try:
            r = crawl_slice(fetcher, conn, role, location, max_pages, yield_floor,
                            resume, run_at,
                            on_page=(lambda d: print(
                                f"    p{d['page']:<3} jobs={d['jobs']:<4} "
                                f"companies={d['companies']}")) if verbose else None)
        except A.MitigationDetected as e:
            halted = str(e)
            if verbose:
                print(f"    !! HALTED: {e}")
            break

        results.append(r)
        if verbose:
            print(f"    -> {r.jobs_seen} jobs ({r.jobs_new} new), "
                  f"{r.companies_seen} companies, {r.pages_walked} pages, "
                  f"ended={r.ended_reason}, recovery={r.recovery_ratio}")

    return {
        "run_at": run_at,
        "slices": [vars(r) | {"recovery_ratio": r.recovery_ratio} for r in results],
        "halted": halted,
        "counts": S.counts(conn),
    }

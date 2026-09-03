"""FastAPI backend for the local UI.

All filtering happens in SQL against the `jobs` view -- the browser never holds
the corpus. The page asks for a window of rows and a total count; that keeps it
responsive at 20k rows without a virtualiser.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import store as S
from ..config import Config
from ..enrich import enrich_ids
from ..fetch import Fetcher

STATIC = Path(__file__).resolve().parent / "static"

SORTS = {
    "posted": "posted_ts DESC",
    "oldest": "posted_ts ASC",
    "salary": "salary_max DESC NULLS LAST, salary_min DESC",
    "salary_asc": "salary_min ASC NULLS LAST",
    "company": "company COLLATE NOCASE ASC",
    "title": "title COLLATE NOCASE ASC",
    "size": "company_size_min DESC NULLS LAST",
    "equity": "equity_max DESC NULLS LAST",
    "slices": "n_slices DESC",
}


class Filters(BaseModel):
    q: str | None = None
    roles: list[str] = []
    locations: list[str] = []
    remote: list[str] = []
    sizes: list[str] = []
    job_types: list[str] = []
    salary_min: int | None = None
    include_unlisted_salary: bool = True
    has_equity: bool = False
    has_ats: bool = False
    max_days_old: int | None = None
    statuses: list[str] = []
    show_hidden: bool = False
    sort: str = "posted"
    limit: int = 100
    offset: int = 0


class StatusIn(BaseModel):
    job_id: str
    status: str
    note: str | None = None


class EnrichIn(BaseModel):
    ids: list[str]


# NOTE: these models MUST stay at module level. With `from __future__ import
# annotations` every annotation is a string, and FastAPI resolves them against
# module globals -- a model defined inside create_app() is invisible there, so
# the body parameter silently degrades into a required query param (HTTP 422).


def _where(f: Filters) -> tuple[str, list[Any]]:
    """Build the WHERE clause. Everything is parameterised -- no string
    interpolation of user input anywhere."""
    w: list[str] = []
    p: list[Any] = []

    if f.q:
        w.append("(title LIKE ? OR description LIKE ? OR company LIKE ?)")
        like = f"%{f.q}%"
        p += [like, like, like]

    # found_via_roles is a comma-joined group_concat, so match on membership.
    for col, vals in (("found_via_roles", f.roles), ("found_via_locations", f.locations)):
        if vals:
            w.append("(" + " OR ".join([f"(',' || {col} || ',') LIKE ?"] * len(vals)) + ")")
            p += [f"%,{v},%" for v in vals]

    if f.remote:
        w.append("(" + " OR ".join(["remote_label = ?"] * len(f.remote)) + ")")
        p += f.remote
    if f.sizes:
        w.append("(" + " OR ".join(["company_size = ?"] * len(f.sizes)) + ")")
        p += f.sizes
    if f.job_types:
        w.append("(" + " OR ".join(["job_type = ?"] * len(f.job_types)) + ")")
        p += f.job_types

    if f.salary_min is not None:
        # "include unlisted" matters: 48% of rows have no salary at all, and
        # dropping them silently would hide most of the corpus.
        if f.include_unlisted_salary:
            w.append("(salary_max >= ? OR salary_min >= ? OR salary_raw IS NULL)")
            p += [f.salary_min, f.salary_min]
        else:
            w.append("(salary_max >= ? OR salary_min >= ?)")
            p += [f.salary_min, f.salary_min]

    if f.has_equity:
        w.append("equity_raw IS NOT NULL")
    if f.has_ats:
        w.append("ats_source IS NOT NULL")
    if f.max_days_old is not None:
        w.append("days_old <= ?")
        p.append(f.max_days_old)

    if f.statuses:
        w.append("(" + " OR ".join(["status = ?"] * len(f.statuses)) + ")")
        p += f.statuses
    elif not f.show_hidden:
        w.append("status != 'hidden'")

    return (" WHERE " + " AND ".join(w)) if w else "", p


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="jobsearch")
    lock = threading.Lock()

    def db() -> sqlite3.Connection:
        return S.connect(cfg.db_path)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/meta")
    def meta():
        conn = db()
        def col(name: str, src: str = "jobs"):
            return [r[0] for r in conn.execute(
                f"SELECT DISTINCT {name} FROM {src} WHERE {name} IS NOT NULL "
                f"ORDER BY {name}") if r[0] not in (None, "")]
        roles = [r[0] for r in conn.execute(
            "SELECT DISTINCT role_slug FROM job_provenance ORDER BY role_slug")]
        locs = [r[0] for r in conn.execute(
            "SELECT DISTINCT location FROM job_provenance ORDER BY location")]
        c = S.counts(conn)
        last = conn.execute("SELECT MAX(run_at) r FROM slice_stats").fetchone()["r"]
        slices = [dict(r) for r in conn.execute(
            "SELECT role_slug,location,total_claimed,jobs_recovered,pages_walked,"
            "ended_reason FROM slice_stats ORDER BY run_at DESC LIMIT 60")]
        for s in slices:
            s["recovery"] = (round(s["jobs_recovered"] / s["total_claimed"], 3)
                             if s["total_claimed"] else None)
        return {
            "roles": roles, "locations": locs,
            "remote": col("remote_label"), "sizes": col("company_size"),
            "job_types": col("job_type"),
            "counts": c, "last_crawl": last, "slices": slices,
            "sorts": list(SORTS),
        }

    @app.post("/api/jobs")
    def jobs(f: Filters):
        conn = db()
        where, params = _where(f)
        total = conn.execute(f"SELECT COUNT(*) FROM jobs{where}", params).fetchone()[0]
        n_enriched = conn.execute(
            f"SELECT COALESCE(SUM(enriched),0) FROM jobs{where}", params).fetchone()[0]
        order = SORTS.get(f.sort, SORTS["posted"])
        rows = conn.execute(
            f"SELECT * FROM jobs{where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [min(f.limit, 500), f.offset]).fetchall()
        return {"total": total, "enriched": n_enriched,
                "rows": [dict(r) for r in rows]}

    @app.post("/api/ids")
    def ids(f: Filters):
        """Every id matching the filter -- what the Enrich button acts on."""
        conn = db()
        where, params = _where(f)
        rows = conn.execute(
            f"SELECT source_job_id FROM jobs{where}", params).fetchall()
        return {"ids": [r[0] for r in rows]}

    @app.post("/api/status")
    def status(s: StatusIn):
        if s.status not in ("new", "shortlisted", "applied", "hidden"):
            raise HTTPException(400, f"bad status {s.status!r}")
        conn = db()
        S.set_status(conn, s.job_id, s.status, s.note)
        conn.commit()
        return {"ok": True, "job_id": s.job_id, "status": s.status}

    @app.post("/api/enrich")
    def enrich(e: EnrichIn):
        if not e.ids:
            return {"enriched": 0, "requested": 0}
        # Serialised: concurrency 1 to Wellfound is non-negotiable, and two
        # overlapping enrich calls would break the rate limit.
        if not lock.acquire(blocking=False):
            raise HTTPException(409, "an enrichment run is already in progress")
        try:
            conn = db()
            f = Fetcher(cfg.cache_dir, cfg.user_agent, cfg.delay_range)
            f.on_response = lambda r: (S.log_request(conn, r), conn.commit())
            return enrich_ids(f, conn, e.ids)
        finally:
            lock.release()

    @app.get("/api/estimate")
    def estimate(n: int):
        lo, hi = cfg.delay_range
        return {"n": n, "seconds": round(n * (lo + hi) / 2),
                "minutes": round(n * (lo + hi) / 2 / 60, 1)}

    return app

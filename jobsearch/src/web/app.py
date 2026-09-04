"""FastAPI backend for the local UI.

All filtering happens in SQL against the `jobs` view -- the browser never holds
the corpus. The page asks for a window of rows and a total count; that keeps it
responsive at 20k rows without a virtualiser.

Two things here are load-bearing and easy to get wrong:

* **Filters are built per dimension**, so `/api/facets` can compute each
  dimension's options with that dimension's own selection removed. That is what
  makes facets narrow each other without a selected value erasing its own
  siblings from the list.

* **Location means the real place** (`job_location`, derived from
  `locationNames`), not the crawl slice token. Those are different things:
  slices are `anywhere` / `remote` / a city we asked Wellfound to pre-filter on;
  locations are Pune, San Francisco, Bengaluru -- 700+ of them.
"""

from __future__ import annotations

import sqlite3
import threading
import traceback
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
    "posted": "posted_ts DESC",           # newest first -- the default
    "oldest": "posted_ts ASC",
    "salary": "salary_max DESC NULLS LAST, salary_min DESC",
    "salary_asc": "salary_min ASC NULLS LAST",
    "company": "company COLLATE NOCASE ASC",
    "title": "title COLLATE NOCASE ASC",
    "size": "company_size_min DESC NULLS LAST",
    "equity": "equity_max DESC NULLS LAST",
    "slices": "n_slices DESC",
    "expires": "expires_ts ASC NULLS LAST",
}


class Filters(BaseModel):
    q: str | None = None
    sources: list[str] = []
    hide_expired: bool = True
    roles: list[str] = []          # role slugs the job was found via
    locations: list[str] = []      # REAL places, from job_location
    remote: list[str] = []
    sizes: list[str] = []
    job_types: list[str] = []
    companies: list[str] = []
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


class CrawlIn(BaseModel):
    roles: list[str]
    location: str = "anywhere"
    max_pages: int | None = None


class IngestIn(BaseModel):
    sources: list[str]


class FetchIn(BaseModel):
    roles: list[str]
    sources: list[str] = []


# NOTE: these models MUST stay at module level. With `from __future__ import
# annotations` every annotation is a string, and FastAPI resolves them against
# module globals -- a model defined inside create_app() is invisible there, so
# the body parameter silently degrades into a required query param (HTTP 422).


def _clauses(f: Filters) -> list[tuple[str, str, list[Any]]]:
    """(dimension, sql, params) per active filter.

    Kept as a list rather than a single string so facet queries can drop one
    dimension. Everything is parameterised; no user input is interpolated.
    """
    out: list[tuple[str, str, list[Any]]] = []

    if f.q:
        like = f"%{f.q}%"
        out.append(("q", "(title LIKE ? OR description LIKE ? OR company LIKE ?)",
                    [like, like, like]))

    if f.roles:
        # found_via_roles is a comma-joined group_concat; match on membership.
        sql = "(" + " OR ".join(["(',' || found_via_roles || ',') LIKE ?"] * len(f.roles)) + ")"
        out.append(("roles", sql, [f"%,{v},%" for v in f.roles]))

    if f.locations:
        ph = ",".join("?" * len(f.locations))
        # Qualified as j.* because facet queries JOIN jobs against
        # job_provenance / job_location, and all three carry source_job_id.
        # Every query below therefore aliases the view as `j`.
        out.append(("locations",
                    f"j.source_job_id IN (SELECT source_job_id FROM job_location "
                    f"WHERE location IN ({ph}))", list(f.locations)))

    if f.hide_expired:
        # `expired` is NULL where the source publishes no expiry (Wellfound).
        # NULL means unknown, never "expired", so those rows must survive.
        out.append(("hide_expired", "(expired IS NULL OR expired = 0)", []))

    for dim, col, vals in (("sources", "source", f.sources),
                           ("remote", "remote_label", f.remote),
                           ("sizes", "company_size", f.sizes),
                           ("job_types", "job_type", f.job_types),
                           ("companies", "company", f.companies)):
        if vals:
            out.append((dim, f"({' OR '.join([f'{col} = ?'] * len(vals))})", list(vals)))

    if f.salary_min is not None:
        # "include unlisted" matters: ~47% of rows have no salary at all, and
        # dropping them silently would hide most of the corpus.
        if f.include_unlisted_salary:
            out.append(("salary",
                        "(salary_max >= ? OR salary_min >= ? OR salary_raw IS NULL)",
                        [f.salary_min, f.salary_min]))
        else:
            out.append(("salary", "(salary_max >= ? OR salary_min >= ?)",
                        [f.salary_min, f.salary_min]))

    if f.has_equity:
        out.append(("has_equity", "equity_raw IS NOT NULL", []))
    if f.has_ats:
        out.append(("has_ats", "ats_source IS NOT NULL", []))
    if f.max_days_old is not None:
        out.append(("max_days_old", "days_old <= ?", [f.max_days_old]))

    if f.statuses:
        out.append(("statuses", "(" + " OR ".join(["status = ?"] * len(f.statuses)) + ")",
                    list(f.statuses)))
    elif not f.show_hidden:
        out.append(("statuses", "status != 'hidden'", []))

    return out


def _where(f: Filters, exclude: str | None = None) -> tuple[str, list[Any]]:
    parts = [(sql, p) for dim, sql, p in _clauses(f) if dim != exclude]
    if not parts:
        return "", []
    return " WHERE " + " AND ".join(s for s, _ in parts), [x for _, p in parts for x in p]


class CrawlJob:
    """One crawl at a time, in a background thread.

    Serialised deliberately: concurrency 1 to Wellfound is a conduct rule, and
    two overlapping crawls would break the rate limit no matter how polite each
    one is on its own.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.state: dict[str, Any] = {"running": False, "roles": [], "done": [],
                                      "current": None, "pages": 0, "jobs_new": 0,
                                      "stale": 0, "error": None, "finished_at": None}

    def start(self, cfg: Config, roles: list[str], location: str, max_pages: int) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        self.state = {"running": True, "roles": roles, "done": [], "current": None,
                      "pages": 0, "jobs_new": 0, "stale": 0, "error": None,
                      "finished_at": None}
        self.thread = threading.Thread(
            target=self._run, args=(cfg, roles, location, max_pages), daemon=True)
        self.thread.start()
        return True

    def start_fetch(self, cfg: Config, roles: list[str],
                    names: list[str]) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        self.state = {"running": True, "roles": roles, "done": [], "current": None,
                      "pages": 0, "jobs_new": 0, "stale": 0, "error": None,
                      "finished_at": None, "mode": "fetch"}
        self.thread = threading.Thread(
            target=self._run_fetch, args=(cfg, roles, names), daemon=True)
        self.thread.start()
        return True

    def _run_fetch(self, cfg: Config, roles: list[str], names: list[str]) -> None:
        from ..roles import fetch_role, sources_for

        try:
            conn = S.connect(cfg.db_path)
            f = Fetcher(cfg.cache_dir, cfg.user_agent, cfg.delay_range)
            f.on_response = lambda r: (S.log_request(conn, r), conn.commit())
            use = names or sources_for(cfg)
            for role in roles:
                self.state["current"] = role
                for r in fetch_role(
                        f, conn, cfg, role, use,
                        on_event=lambda d: self.state.update(
                            pages=self.state["pages"] + 1,
                            stale=self.state["stale"] + d.get("stale_skipped", 0))):
                    self.state["jobs_new"] += r["new"]
                    self.state["done"].append({
                        "role": f"{role} · {r['source']}", "jobs": r["seen"],
                        "new": r["new"], "stale": r["stale_skipped"],
                        "filtered_out": r["filtered_out"],
                        "filter_mode": r["filter_mode"],
                        "reason": r.get("reason"),
                        "pages": r["pages"], "ended": r.get("ended_reason"),
                        "claimed": r.get("claimed")})
        except Exception as e:
            self.state["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            self.state["running"] = False
            self.state["current"] = None
            self.state["finished_at"] = S.now()
            self.lock.release()

    def start_ingest(self, cfg: Config, names: list[str]) -> bool:
        """Shares the crawl lock deliberately: concurrency 1 to the network is a
        process-wide rule, not a per-source one."""
        if not self.lock.acquire(blocking=False):
            return False
        self.state = {"running": True, "roles": names, "done": [], "current": None,
                      "pages": 0, "jobs_new": 0, "stale": 0, "error": None,
                      "finished_at": None, "mode": "ingest"}
        self.thread = threading.Thread(
            target=self._run_ingest, args=(cfg, names), daemon=True)
        self.thread.start()
        return True

    def _run_ingest(self, cfg: Config, names: list[str]) -> None:
        from .. import sources as SRC

        try:
            conn = S.connect(cfg.db_path)
            f = Fetcher(cfg.cache_dir, cfg.user_agent, cfg.delay_range)
            f.on_response = lambda r: (S.log_request(conn, r), conn.commit())
            reg = SRC.registry()
            for name in names:
                self.state["current"] = name
                mod = reg[name]
                for target in cfg.source_targets(name):
                    r = mod.ingest(
                        f, conn, target, cutoff_ts=cfg.cutoff_ts(),
                        max_pages=cfg.source_max_pages(name),
                        on_page=lambda d: self.state.update(
                            pages=self.state["pages"] + 1,
                            stale=self.state["stale"] + d["stale_skipped"]))
                    self.state["jobs_new"] += r["new"]
                    self.state["done"].append(
                        {"role": f"{name}:{r['target']}", "jobs": r["seen"],
                         "new": r["new"], "stale": r["stale_skipped"],
                         "pages": r["pages"], "ended": r["ended_reason"],
                         "claimed": r.get("claimed")})
            S.rebuild_locations(conn)
        except Exception as e:
            self.state["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            self.state["running"] = False
            self.state["current"] = None
            self.state["finished_at"] = S.now()
            self.lock.release()

    def _run(self, cfg: Config, roles: list[str], location: str, max_pages: int) -> None:
        from ..crawl import crawl_slice, validate_role

        try:
            conn = S.connect(cfg.db_path)
            f = Fetcher(cfg.cache_dir, cfg.user_agent, cfg.delay_range)
            f.on_response = lambda r: (S.log_request(conn, r), conn.commit())
            run_at = S.now()
            for role in roles:
                self.state["current"] = role
                v = validate_role(f, conn, role, location)
                if not v["valid"]:
                    # Never silently fall back to an unfiltered search.
                    raise RuntimeError(f"{role}: {v['reason']}")
                res = crawl_slice(
                    f, conn, role, location, max_pages, cfg.yield_floor,
                    resume=True, run_at=run_at,
                    on_page=lambda d: self.state.update(
                        pages=self.state["pages"] + 1,
                        stale=self.state["stale"] + d["stale_skipped"]),
                    cutoff_ts=cfg.cutoff_ts())
                self.state["jobs_new"] += res.jobs_new
                self.state["done"].append(
                    {"role": role, "jobs": res.jobs_seen, "new": res.jobs_new,
                     "stale": res.jobs_stale_skipped,
                     "pages": res.pages_walked, "ended": res.ended_reason,
                     "claimed": res.total_claimed})
            # crawl_slice() is called directly here rather than crawl(), so the
            # re-derivation that crawl() performs must happen explicitly.
            S.rebuild_locations(conn)
        except Exception as e:
            self.state["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            self.state["running"] = False
            self.state["current"] = None
            self.state["finished_at"] = S.now()
            self.lock.release()


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(title="jobsearch")
    enrich_lock = threading.Lock()
    job = CrawlJob()

    def db() -> sqlite3.Connection:
        return S.connect(cfg.db_path)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/meta")
    def meta():
        conn = db()
        c = S.counts(conn)
        last = conn.execute("SELECT MAX(run_at) r FROM slice_stats").fetchone()["r"]
        slices = [dict(r) for r in conn.execute(
            "SELECT role_slug,location,total_claimed,jobs_recovered,pages_walked,"
            "stale_skipped,ended_reason FROM slice_stats ORDER BY run_at DESC LIMIT 60")]
        for s in slices:
            # Reachability, not keep-rate: stale jobs WERE reached, we chose not
            # to store them. Adding them back is what keeps this comparable to
            # runs made before the cutoff existed.
            reached = (s["jobs_recovered"] or 0) + (s["stale_skipped"] or 0)
            s["reached"] = reached
            s["recovery"] = (round(reached / s["total_claimed"], 3)
                             if s["total_claimed"] else None)
        crawled = {r[0] for r in conn.execute(
            "SELECT DISTINCT role_slug FROM job_provenance")}
        return {
            # Every configured role, flagged with whether it has been crawled.
            "roles": [{"slug": r, "crawled": r in crawled} for r in cfg.roles],
            "counts": c, "last_crawl": last, "slices": slices,
            "sorts": list(SORTS), "max_pages": cfg.max_pages_per_slice,
            "sources": sorted(cfg.sources) + ["wellfound"],
            # How each source will filter this role -- shown in the UI so
            # "fetched for role X" never implies filtering that did not happen.
            # RemoteOK is "local": role fetch deliberately skips its ?tag=
            # endpoints because they serve an archive, not the live feed.
            "role_filtering": {
                r: {"wellfound": "server",
                    "remoteok": "local" if cfg.role_keywords(r) else "skipped",
                    "himalayas": "local" if cfg.role_keywords(r) else "skipped"}
                for r in cfg.roles},
            "delay_range": list(cfg.delay_range),
            "max_age_days": cfg.max_age_days,
        }

    @app.post("/api/facets")
    def facets(f: Filters):
        """Options for every dimension, counted against the CURRENT filter set.

        Each dimension is computed with its own selection excluded, so choosing
        "Pune" narrows company/size/remote but leaves the other cities visible
        and selectable. Values that would yield zero rows are omitted entirely --
        that is the "filters update as I select" behaviour.
        """
        conn = db()
        out: dict[str, Any] = {}

        def simple(dim: str, col: str, limit: int = 400):
            where, p = _where(f, exclude=dim)
            rows = conn.execute(
                f"SELECT {col} AS v, COUNT(*) n FROM jobs j{where} "
                f"{'AND' if where else 'WHERE'} {col} IS NOT NULL AND {col} <> '' "
                f"GROUP BY v ORDER BY n DESC, v LIMIT {limit}", p).fetchall()
            out[dim] = [{"value": r["v"], "count": r["n"]} for r in rows]

        # Location joins the derived table; everything else is a column.
        where, p = _where(f, exclude="locations")
        rows = conn.execute(
            f"SELECT l.location AS v, l.kind AS kind, COUNT(DISTINCT j.source_job_id) n "
            f"FROM jobs j JOIN job_location l ON l.source_job_id = j.source_job_id{where} "
            f"GROUP BY l.location, l.kind ORDER BY n DESC, v LIMIT 500", p).fetchall()
        merged: dict[str, dict] = {}
        for r in rows:
            e = merged.setdefault(r["v"], {"value": r["v"], "count": 0, "kinds": []})
            e["count"] += r["n"]
            e["kinds"].append(r["kind"])
        out["locations"] = sorted(merged.values(), key=lambda x: (-x["count"], x["value"]))

        simple("sources", "source")
        simple("remote", "remote_label")
        simple("sizes", "company_size")
        simple("job_types", "job_type")
        simple("companies", "company", limit=300)

        where, p = _where(f, exclude="roles")
        rows = conn.execute(
            f"SELECT p.role_slug v, COUNT(DISTINCT p.source_job_id) n FROM job_provenance p "
            f"JOIN jobs j ON j.source_job_id = p.source_job_id{where} "
            f"GROUP BY v ORDER BY n DESC", p).fetchall()
        out["roles"] = [{"value": r["v"], "count": r["n"]} for r in rows]

        where, p = _where(f, exclude="statuses")
        rows = conn.execute(
            f"SELECT status v, COUNT(*) n FROM jobs j{where} GROUP BY v", p).fetchall()
        out["statuses"] = [{"value": r["v"], "count": r["n"]} for r in rows]

        where, p = _where(f)
        out["total"] = conn.execute(f"SELECT COUNT(*) FROM jobs j{where}", p).fetchone()[0]
        return out

    @app.post("/api/jobs")
    def jobs(f: Filters):
        conn = db()
        where, params = _where(f)
        total = conn.execute(f"SELECT COUNT(*) FROM jobs j{where}", params).fetchone()[0]
        n_enriched = conn.execute(
            f"SELECT COALESCE(SUM(enriched),0) FROM jobs j{where}", params).fetchone()[0]
        order = SORTS.get(f.sort, SORTS["posted"])
        rows = conn.execute(
            f"SELECT j.* FROM jobs j{where} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [min(f.limit, 500), f.offset]).fetchall()
        return {"total": total, "enriched": n_enriched, "rows": [dict(r) for r in rows]}

    @app.post("/api/ids")
    def ids(f: Filters):
        conn = db()
        where, params = _where(f)
        return {"ids": [r[0] for r in conn.execute(
            f"SELECT j.source_job_id FROM jobs j{where}", params)]}

    @app.post("/api/status")
    def status(s: StatusIn):
        if s.status not in ("new", "shortlisted", "applied", "hidden"):
            raise HTTPException(400, f"bad status {s.status!r}")
        conn = db()
        S.set_status(conn, s.job_id, s.status, s.note)
        conn.commit()
        return {"ok": True, "job_id": s.job_id, "status": s.status}

    @app.post("/api/crawl")
    def crawl_start(c: CrawlIn):
        unknown = [r for r in c.roles if r not in cfg.roles]
        if unknown:
            raise HTTPException(400, f"unknown role slug(s): {unknown}. "
                                     f"Add them to targets.yaml first.")
        if not c.roles:
            raise HTTPException(400, "no roles selected")
        started = job.start(cfg, c.roles, c.location,
                            c.max_pages or cfg.max_pages_per_slice)
        if not started:
            raise HTTPException(409, "a crawl is already running")
        lo, hi = cfg.delay_range
        pages = (c.max_pages or cfg.max_pages_per_slice) * len(c.roles)
        return {"started": True, "roles": c.roles,
                "est_minutes": round(pages * (lo + hi) / 2 / 60, 1)}

    @app.post("/api/fetch")
    def fetch_start(i: FetchIn):
        """The main workflow: pick role(s), fetch from every enabled source."""
        unknown = [r for r in i.roles if r not in cfg.roles]
        if unknown:
            raise HTTPException(400, f"unknown role slug(s): {unknown}. "
                                     f"Add them to targets.yaml first.")
        if not i.roles:
            raise HTTPException(400, "no roles selected")
        if not job.start_fetch(cfg, i.roles, i.sources):
            raise HTTPException(409, "a crawl, ingest or fetch is already running")
        lo, hi = cfg.delay_range
        # Wellfound pages + one RemoteOK request + the Himalayas page budget.
        per_role = cfg.max_pages_per_slice + 1 + cfg.source_max_pages("himalayas")
        return {"started": True, "roles": i.roles,
                "est_minutes": round(len(i.roles) * per_role * (lo + hi) / 2 / 60, 1)}

    @app.post("/api/ingest")
    def ingest_start(i: IngestIn):
        from .. import sources as SRC

        reg = SRC.registry()
        unknown = [n for n in i.sources if n not in reg or n == "wellfound"]
        if unknown:
            raise HTTPException(400, f"not ingestable here: {unknown} "
                                     f"(wellfound uses /api/crawl)")
        if not i.sources:
            raise HTTPException(400, "no sources selected")
        if not job.start_ingest(cfg, i.sources):
            raise HTTPException(409, "a crawl or ingest is already running")
        return {"started": True, "sources": i.sources}

    @app.get("/api/crawl/status")
    def crawl_status():
        return job.state

    @app.post("/api/enrich")
    def enrich(e: EnrichIn):
        if not e.ids:
            return {"enriched": 0, "requested": 0}
        if not enrich_lock.acquire(blocking=False):
            raise HTTPException(409, "an enrichment run is already in progress")
        try:
            conn = db()
            f = Fetcher(cfg.cache_dir, cfg.user_agent, cfg.delay_range)
            f.on_response = lambda r: (S.log_request(conn, r), conn.commit())
            return enrich_ids(f, conn, e.ids, max_age_days=cfg.max_age_days)
        finally:
            enrich_lock.release()

    @app.get("/api/estimate")
    def estimate(n: int):
        lo, hi = cfg.delay_range
        return {"n": n, "seconds": round(n * (lo + hi) / 2),
                "minutes": round(n * (lo + hi) / 2 / 60, 1)}

    return app

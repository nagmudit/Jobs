"""Command line entry point.

    python -m src.cli roles --location bangalore
    python -m src.cli crawl
    python -m src.cli crawl --roles ai-engineer --locations remote
    python -m src.cli enrich --ids 123,456
    python -m src.cli stats
    python -m src.cli serve
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback

from . import assertions as A
from . import store as S
from .config import Config, TARGETS_PATH
from .crawl import crawl, validate_role
from .enrich import enrich_ids
from .fetch import Fetcher, search_url


def _wire(cfg: Config):
    conn = S.connect(cfg.db_path)
    f = Fetcher(cfg.cache_dir, cfg.user_agent, cfg.delay_range)
    f.on_response = lambda r: (S.log_request(conn, r), conn.commit())
    return conn, f


def _slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


# Role titles that are category buckets, not searchable roles.
TITLE_STOPLIST = {"other", "engineering", "operations", "sales", "marketing", "design"}


def cmd_roles(args, cfg: Config) -> int:
    """Discover + validate role slugs.

    Candidates come from slugifying `primaryRoleTitle` on job nodes already in
    the DB -- Wellfound's own canonical role names for jobs it actually returned.
    The footer link blocks are NOT used: they are an identical generic 14-slug
    list on every page, so crawling them is a dead end.
    """
    conn, f = _wire(cfg)
    location = args.location or (cfg.locations[0] if cfg.locations else None)
    if not location:
        print("need --location (or a location in targets.yaml)", file=sys.stderr)
        return 2

    rows = conn.execute(
        "SELECT DISTINCT json_extract(raw_json,'$.primaryRoleTitle') AS t FROM job_raw"
    ).fetchall()
    cands = sorted({
        _slugify(r["t"]) for r in rows
        if r["t"] and _slugify(r["t"]) and _slugify(r["t"]) not in TITLE_STOPLIST
    })
    # Anything already configured is worth (re)validating too.
    for r in cfg.roles:
        if r not in cands:
            cands.append(r)

    if not cands:
        print("No candidates. Run `crawl` first so there are job nodes to harvest "
              "primaryRoleTitle from.")
        return 1

    print(f"Validating {len(cands)} candidate role slugs against location={location!r}\n")
    valid, rejected = [], []
    for slug in cands:
        try:
            v = validate_role(f, conn, slug, location)
        except A.MitigationDetected as e:
            print(f"\n!! HALTED: {e}", file=sys.stderr)
            return 2
        conn.execute(
            "INSERT INTO role_slug (role_slug,valid,total_job_count,location_probed,"
            "reason,checked_at) VALUES (?,?,?,?,?,?) ON CONFLICT(role_slug) DO UPDATE SET "
            "valid=excluded.valid, total_job_count=excluded.total_job_count, "
            "location_probed=excluded.location_probed, reason=excluded.reason, "
            "checked_at=excluded.checked_at",
            (slug, int(v["valid"]), v["total_job_count"], location, v["reason"], S.now()))
        conn.commit()
        (valid if v["valid"] else rejected).append(v)
        mark = "OK  " if v["valid"] else "REJ "
        print(f"  {mark} {slug:36} jobs={str(v['total_job_count']):>7}"
              f"{'' if v['valid'] else '  ' + (v['reason'] or '')[:60]}")

    print(f"\n{len(valid)} valid, {len(rejected)} rejected.")
    if valid:
        print("\nAdd to targets.yaml:\nroles:")
        for v in sorted(valid, key=lambda x: -(x["total_job_count"] or 0)):
            print(f"  - {v['role']}   # {v['total_job_count']} jobs @ {location}")
    return 0


def cmd_crawl(args, cfg: Config) -> int:
    conn, f = _wire(cfg)
    slices = cfg.slices()
    if not slices:
        print("no roles x locations to crawl; set them in targets.yaml or pass "
              "--roles/--locations", file=sys.stderr)
        return 2

    print(f"Crawling {len(slices)} slices "
          f"({len(cfg.roles)} roles x {len(cfg.locations)} locations), "
          f"max {cfg.max_pages_per_slice} pages each, {cfg.delay_range[0]}-"
          f"{cfg.delay_range[1]}s apart.")
    try:
        out = crawl(f, conn, slices, cfg.max_pages_per_slice, cfg.yield_floor,
                    resume=not args.no_resume, validate=not args.no_validate)
    except A.CorpusIntegrityError as e:
        print(f"\n!! CRAWL ABORTED: {type(e).__name__}: {e}", file=sys.stderr)
        _print_stats(conn)
        return 2

    print("\n" + "=" * 70)
    for s in out["slices"]:
        print(f"  {s['role']:32} {s['location']:10} jobs={s['jobs_seen']:>5} "
              f"new={s['jobs_new']:>5} pages={s['pages_walked']:>3} "
              f"{s['ended_reason']:<10} recovery={s['recovery_ratio']}")
    if out["halted"]:
        print(f"\n  !! HALTED: {out['halted']}")
    _print_stats(conn)
    return 0


def cmd_enrich(args, cfg: Config) -> int:
    conn, f = _wire(cfg)
    if args.ids:
        ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    else:
        ids = [r["source_job_id"] for r in conn.execute(
            "SELECT source_job_id FROM jobs WHERE enriched=0 LIMIT ?", (args.limit,))]
    if not ids:
        print("nothing to enrich")
        return 0
    lo, hi = cfg.delay_range
    print(f"Enriching {len(ids)} jobs (~{len(ids)*(lo+hi)/2/60:.1f} min at "
          f"{lo}-{hi}s per request)")
    out = enrich_ids(f, conn, ids, refresh=args.refresh,
                     on_progress=lambda d: print(f"  {d['done']}/{d['total']}", end="\r"))
    print(f"\n{out}")
    return 2 if out["halted"] else 0


def _print_stats(conn) -> None:
    c = S.counts(conn)
    print("\nCORPUS")
    for k, v in c.items():
        print(f"  {k:14} {v}")
    row = conn.execute(
        "SELECT COUNT(*) n,"
        " SUM(salary_raw IS NOT NULL) sal,"
        " SUM(equity_raw IS NOT NULL) eq,"
        " SUM(enriched) enr,"
        " SUM(status='shortlisted') sl, SUM(status='applied') ap,"
        " SUM(status='hidden') hd FROM jobs").fetchone()
    if row and row["n"]:
        n = row["n"]
        print("\nFILL RATES")
        print(f"  salary        {row['sal'] or 0}/{n} ({100*(row['sal'] or 0)/n:.1f}%)")
        print(f"  equity        {row['eq'] or 0}/{n} ({100*(row['eq'] or 0)/n:.1f}%)")
        print(f"  enriched      {row['enr'] or 0}/{n} ({100*(row['enr'] or 0)/n:.1f}%)")
        print(f"\n  shortlisted {row['sl'] or 0} | applied {row['ap'] or 0} | hidden {row['hd'] or 0}")

    sl = conn.execute(
        "SELECT role_slug,location,total_claimed,jobs_recovered,pages_walked,ended_reason"
        " FROM slice_stats ORDER BY run_at DESC, role_slug LIMIT 40").fetchall()
    if sl:
        print("\nSLICE TRUNCATION (low ratio => needs sub-partitioning)")
        for s in sl:
            ratio = (s["jobs_recovered"] / s["total_claimed"]) if s["total_claimed"] else None
            print(f"  {s['role_slug']:30} {s['location']:10} "
                  f"{s['jobs_recovered']:>5}/{str(s['total_claimed']):>6} "
                  f"p{s['pages_walked']:<3} {s['ended_reason']:<10} "
                  f"{('%.2f' % ratio) if ratio else '-'}")

    ev = conn.execute(
        "SELECT COUNT(*) c FROM request_log WHERE cf_mitigated IS NOT NULL "
        "OR status IN (403,429,503)").fetchone()["c"]
    print(f"\n  mitigation events: {ev}")


def cmd_stats(args, cfg: Config) -> int:
    conn, _ = _wire(cfg)
    _print_stats(conn)
    return 0


def cmd_serve(args, cfg: Config) -> int:
    import uvicorn

    from .web.app import create_app

    app = create_app(cfg)
    print(f"http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jobsearch", description="Wellfound job corpus tool")
    ap.add_argument("--targets", default=None, help="path to targets.yaml")
    ap.add_argument("--roles", default=None, help="comma-separated, overrides targets.yaml")
    ap.add_argument("--locations", default=None, help="comma-separated, overrides targets.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def targeted(p):
        """--roles/--locations accepted after the subcommand too, since that is
        the natural place to type them."""
        p.add_argument("--roles", dest="roles2", default=None)
        p.add_argument("--locations", dest="locations2", default=None)
        return p

    p = targeted(sub.add_parser("roles", help="discover + validate role slugs"))
    p.add_argument("--location", default=None)
    p.set_defaults(fn=cmd_roles)

    p = targeted(sub.add_parser("crawl", help="breadth crawl of every role x location slice"))
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--no-validate", action="store_true")
    p.set_defaults(fn=cmd_crawl)

    p = sub.add_parser("enrich", help="fetch detail pages for specific jobs")
    p.add_argument("--ids", default=None, help="comma-separated source_job_ids")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(fn=cmd_enrich)

    p = sub.add_parser("stats", help="corpus size, fill rates, slice truncation")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("serve", help="local web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    roles = getattr(args, "roles2", None) or args.roles
    locations = getattr(args, "locations2", None) or args.locations
    cfg = Config.load(args.targets or TARGETS_PATH, roles, locations)

    try:
        return args.fn(args, cfg)
    except A.CorpusIntegrityError as e:
        print(f"\n!! {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

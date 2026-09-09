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
import shutil
import sys
import time
import traceback
from pathlib import Path

from . import assertions as A
from . import store as S
from .config import Config, TARGETS_PATH
from .crawl import crawl, validate_role
from .enrich import enrich_ids
from .fetch import Fetcher, search_url


def _wire(cfg: Config):
    conn = S.connect(cfg.db_path)
    f = Fetcher(cfg.cache_dir, cfg.user_agent, cfg.delay_range,
                    listing_ttl=cfg.cache_ttl_seconds(),
                    robots_overrides=cfg.robots_overrides,
                    user_agents=cfg.user_agents)
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

    age = (f"keeping jobs <= {cfg.max_age_days}d old"
           if cfg.max_age_days else "no age cutoff")
    print(f"Crawling {len(slices)} slices "
          f"({len(cfg.roles)} roles x {len(cfg.locations)} locations), "
          f"max {cfg.max_pages_per_slice} pages each, {cfg.delay_range[0]}-"
          f"{cfg.delay_range[1]}s apart, {age}.")
    try:
        out = crawl(f, conn, slices, cfg.max_pages_per_slice, cfg.yield_floor,
                    resume=not args.no_resume, validate=not args.no_validate,
                    cutoff_ts=cfg.cutoff_ts())
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

    st = S.application_stats(conn)
    if st["applied_total"]:
        fn = st["funnel"]
        print("\nAPPLICATIONS")
        print(f"  last 7d       {st['applied_7d']}")
        print(f"  last 30d      {st['applied_30d']}")
        print(f"  all time      {st['applied_total']}")
        print(f"  funnel        shortlisted {fn['shortlisted']} -> applied "
              f"{fn['applied']} -> interviewing {fn['interviewing']} -> "
              f"offer {fn['offer']} / rejected {fn['rejected']}")
        pct = 100 * st["responded"] / st["applied_total"]
        line = f"  responded     {st['responded']}/{st['applied_total']} ({pct:.0f}%)"
        if st["median_days_to_reply"] is not None:
            line += f", median {st['median_days_to_reply']}d to reply"
        print(line)

    sl = conn.execute(
        "SELECT role_slug,location,total_claimed,jobs_recovered,pages_walked,"
        "stale_skipped,ended_reason FROM slice_stats ORDER BY run_at DESC, role_slug"
        " LIMIT 40").fetchall()
    if sl:
        print("\nSLICE TRUNCATION  kept+stale=reached/claimed")
        print("  low ratio => the page cap truncated it; high stale => the age "
              "cutoff is doing the work")
        for s in sl:
            stale = s["stale_skipped"] or 0
            # Reachability, not keep-rate: stale jobs WERE reached, we chose not
            # to store them. Mixing the two makes this number unreadable.
            reached = (s["jobs_recovered"] or 0) + stale
            ratio = (reached / s["total_claimed"]) if s["total_claimed"] else None
            print(f"  {s['role_slug']:30} {s['location']:10} "
                  f"{s['jobs_recovered']:>4}+{stale:<4}={reached:>5}"
                  f"/{str(s['total_claimed']):>6} "
                  f"p{s['pages_walked']:<3} {s['ended_reason']:<10} "
                  f"{('%.2f' % ratio) if ratio else '-'}")

    ev = conn.execute(
        "SELECT COUNT(*) c FROM request_log WHERE cf_mitigated IS NOT NULL "
        "OR status IN (403,429,503)").fetchone()["c"]
    print(f"\n  mitigation events: {ev}")


def cmd_fetch(args, cfg: Config) -> int:
    """Fetch one or more roles from every enabled source.

    This is the role-driven workflow: one role in, each source queried in its
    own shape. Sources differ in HOW the role is applied, so every line reports
    its filter_mode -- see src/roles.py and ADR-009.
    """
    from .roles import describe, fetch_role, progress_line, sources_for

    conn, f = _wire(cfg)
    roles = [r.strip() for r in (args.roles2 or args.roles or "").split(",") if r.strip()]
    if not roles:
        roles = list(cfg.roles)
    names = ([n.strip() for n in args.sources.split(",") if n.strip()]
             if args.sources else sources_for(cfg))

    age = f"keeping jobs <= {cfg.max_age_days}d" if cfg.max_age_days else "no age cutoff"
    print(f"Fetching {len(roles)} role(s) from {names} at "
          f"{cfg.delay_range[0]}-{cfg.delay_range[1]}s, {age}.")

    for role in roles:
        print(f"\n=== {role} ===")
        try:
            res = fetch_role(
                f, conn, cfg, role, names,
                on_event=lambda d: print(progress_line(d), flush=True))
        except A.CorpusIntegrityError as e:
            print(f"  !! HALTED: {type(e).__name__}: {e}", file=sys.stderr)
            _print_stats(conn)
            return 2
        print(describe(res))

    _print_stats(conn)
    return 0


def cmd_ingest(args, cfg: Config) -> int:
    """Pull from the JSON-API sources (RemoteOK, Himalayas).

    Separate from `crawl` because these are documented APIs with their own
    paging, not Wellfound search pages: none of the slice machinery or the four
    Wellfound-specific assertions apply.
    """
    from . import sources as SRC

    conn, f = _wire(cfg)
    reg = SRC.registry()
    names = ([n.strip() for n in args.sources.split(",") if n.strip()]
             if args.sources else
             [n for n in reg if n != "wellfound" and cfg.source_enabled(n)])
    unknown = [n for n in names if n not in reg]
    if unknown:
        print(f"unknown source(s): {unknown}. Known: {sorted(reg)}", file=sys.stderr)
        return 2

    cutoff = cfg.cutoff_ts()
    age = f"keeping jobs <= {cfg.max_age_days}d" if cfg.max_age_days else "no age cutoff"
    print(f"Ingesting {names} at {cfg.delay_range[0]}-{cfg.delay_range[1]}s, {age}.")

    results = []
    for name in names:
        mod = reg[name]
        if not hasattr(mod, "ingest"):
            print(f"  {name}: no ingest() -- skipped (view-only source)")
            continue
        for target in cfg.source_targets(name):
            label = target.get("tag") or target.get("name") or "all"
            print(f"\n  {name} :: {label}")
            try:
                r = mod.ingest(
                    f, conn, target, cutoff_ts=cutoff,
                    max_pages=cfg.source_max_pages(name),
                    on_page=lambda d: print(
                        f"    p{d['page']:<3} got={d['jobs']:<4} kept={d['kept']:<5} "
                        f"stale={d['stale_skipped']}"))
            except A.CorpusIntegrityError as e:
                print(f"    !! HALTED: {type(e).__name__}: {e}", file=sys.stderr)
                _print_stats(conn)
                return 2
            results.append(r)
            print(f"    -> {r['seen']} kept ({r['new']} new), "
                  f"{r['stale_skipped']} too old, {r['pages']} pages, "
                  f"claimed={r.get('claimed')}, ended={r['ended_reason']}")

    S.rebuild_locations(conn)
    _print_stats(conn)
    return 0


def cmd_prune(args, cfg: Config) -> int:
    conn, _ = _wire(cfg)
    if not cfg.max_age_days:
        print("no age cutoff configured (max_age_days is 0/null); nothing to prune")
        return 0
    out = S.prune_stale(conn, cfg.max_age_days, dry_run=not args.apply)
    if out["dry_run"]:
        print(f"Would remove {out['would_remove']} of {out['total']} jobs "
              f"older than {cfg.max_age_days}d.")
        print("Re-run with --apply to delete. The raw pages stay in cache/, so a "
              "wider cutoff can be re-ingested with `crawl --no-resume` and no "
              "network.")
    else:
        S.rebuild_locations(conn)
        print(f"Removed {out['removed']} jobs older than {cfg.max_age_days}d.")
        _print_stats(conn)
    return 0


def cmd_reconcile(args, cfg: Config) -> int:
    """Apply the role filter retroactively to pre-ADR-009 rows."""
    conn, _ = _wire(cfg)
    kw = {r: cfg.role_keywords(r) for r in cfg.roles if cfg.role_keywords(r)}
    if not kw:
        print("no role keywords configured in targets.yaml; refusing to judge "
              "any row as off-role")
        return 1
    out = S.reconcile_roles(conn, kw, dry_run=not args.apply)
    if out["dry_run"]:
        print(f"{out['scanned']} job(s) carry no configured role slug "
              f"(ingested before the role filter existed).")
        print(f"  would re-tag with a real role : {out['would_retag']}")
        print(f"  would remove as off-role      : {out['would_remove']}")
        print(f"  protected by user_state       : {out['protected']}")
        print("Re-run with --apply. Removed rows stay in cache/ and can be "
              "re-ingested without network.")
    else:
        S.rebuild_locations(conn)
        print(f"Re-tagged {out['retagged']}, removed {out['removed']}, "
              f"protected {out['protected']}.")
        _print_stats(conn)
    return 0


def cmd_stats(args, cfg: Config) -> int:
    conn, _ = _wire(cfg)
    _print_stats(conn)
    return 0


def cmd_sync(args, cfg: Config) -> int:
    """Adopt a freshly downloaded corpus without losing local marks.

    The daily workflow publishes a corpus with `user_state` stripped, so pulling
    it down and using it directly would silently discard every applied and
    shortlisted mark. This carries them across, upholding the same invariant
    `prune_stale` states: a job you marked applied keeps that mark.

    Deliberately does NO network. Download it yourself -- the repo is public:

        gh release download corpus --pattern jobs.db --dir /tmp
        python -m src.cli sync /tmp/jobs.db --apply

    Keeping the download out of Python is what keeps `src/fetch.py` the only
    module that can reach the network (AGENTS.md).
    """
    incoming = Path(args.incoming)
    if not incoming.exists():
        print(f"no such corpus: {incoming}", file=sys.stderr)
        return 2

    marks: list = []
    events: list = []
    if Path(cfg.db_path).exists():
        local = S.connect(cfg.db_path)
        marks = S.export_user_state(local)
        # The dated history goes across too. Without this every refresh of the
        # published corpus silently wipes it -- and unlike a mark, an event
        # cannot be re-created by clicking the button again.
        events = S.export_events(local)
        local.close()

    if not args.apply:
        print(f"Would merge {len(marks)} mark(s) and {len(events)} event(s) "
              f"into {incoming} and move it "
              f"to {cfg.db_path}, backing up the current corpus. "
              f"Re-run with --apply to do it.")
        return 0

    conn = S.connect(incoming)
    n = S.import_user_state(conn, marks)
    e = S.import_events(conn, events)
    conn.commit()
    # The published corpus is pruned by age and knows nothing about marks, so a
    # job you applied to a month ago is simply absent from it. Bring those rows
    # back, or user_state ends up pointing at jobs that no longer exist.
    carried = S.carry_forward_marked(conn, cfg.db_path)
    conn.close()

    db = Path(cfg.db_path)
    if db.exists():
        backup = db.with_name(f"{db.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            db.replace(backup)
        except OSError as e:
            # Windows refuses to move a file another process still holds open.
            # The raw errno here says nothing useful; the cause is almost always
            # a running `serve`.
            print(f"cannot replace {db}: {e}. Something is holding the corpus "
                  f"open -- stop `serve` (or any open sqlite session) and "
                  f"re-run. Nothing has been changed.", file=sys.stderr)
            return 2
        print(f"Previous corpus -> {backup.name}")
        # WAL sidecars belong to the file that just moved; leaving them beside
        # the new corpus would look like its own uncheckpointed writes.
        for suffix in ("-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                side.unlink()
    shutil.move(str(incoming), str(db))
    print(f"Synced. {n} mark(s) and {e} history event(s) carried over, "
          f"{carried} marked job(s) preserved from the previous corpus.")
    return 0


def cmd_export(args, cfg: Config) -> int:
    """Write a PUBLISHABLE copy of the corpus: no user marks.

    The hosted deployment is a public URL. It serves third-party job listings;
    shortlist/applied/hidden are the user's own and stay on their machine. The
    daily workflow publishes this output, never jobs.db itself.
    """
    out = Path(args.out)
    src = Path(cfg.db_path)
    if not src.exists():
        print(f"no corpus at {src}", file=sys.stderr)
        return 2

    # WAL mode means a plain copy can miss rows still sitting in jobs.db-wal.
    conn = S.connect(src)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)

    pub = S.connect(out)
    n = S.clear_user_state(pub)
    # The corpus is published to a public repo. This log says which jobs the
    # owner applied to and when -- strictly more revealing than the marks.
    ev = S.clear_events(pub)
    # Commit BEFORE checkpointing or vacuuming: the DELETE holds a write
    # transaction, and both fail inside one with "database table is locked".
    pub.commit()
    # SQLite does not hand deleted pages back to the OS, so a pruned corpus is
    # exactly as large as an unpruned one until this runs. On the real corpus
    # this was the difference between 59.7 MB and 46.0 MB -- the whole reason
    # pruning keeps the published file under Vercel's 250 MB bundle limit.
    pub.execute("VACUUM")
    pub.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    pub.close()
    for suffix in ("-wal", "-shm"):
        side = out.with_name(out.name + suffix)
        if side.exists():
            side.unlink()

    print(f"Exported {out} ({out.stat().st_size} bytes), "
          f"{n} mark(s) and {ev} history event(s) stripped.")
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
    ap.add_argument("--max-age-days", type=int, default=None, dest="max_age",
                    help="age cutoff in days; 0 disables it (default from targets.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def targeted(p):
        """--roles/--locations/--max-age-days accepted after the subcommand too,
        since that is the natural place to type them."""
        p.add_argument("--roles", dest="roles2", default=None)
        p.add_argument("--locations", dest="locations2", default=None)
        p.add_argument("--max-age-days", type=int, dest="max_age2", default=None)
        return p

    p = targeted(sub.add_parser("roles", help="discover + validate role slugs"))
    p.add_argument("--location", default=None)
    p.set_defaults(fn=cmd_roles)

    p = targeted(sub.add_parser(
        "fetch", help="fetch role(s) from every enabled source (the main workflow)"))
    p.add_argument("--sources", default=None,
                   help="comma-separated; default = all enabled sources")
    p.set_defaults(fn=cmd_fetch)

    p = targeted(sub.add_parser("crawl", help="Wellfound-only role x location slice crawl"))
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--no-validate", action="store_true")
    p.set_defaults(fn=cmd_crawl)

    p = sub.add_parser("enrich", help="fetch detail pages for specific jobs")
    p.add_argument("--ids", default=None, help="comma-separated source_job_ids")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(fn=cmd_enrich)

    p = sub.add_parser("ingest", help="pull from JSON-API sources (remoteok, himalayas)")
    p.add_argument("--sources", default=None,
                   help="comma-separated; default = all enabled non-wellfound sources")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("sync", help="adopt a downloaded corpus, keeping local marks")
    p.add_argument("incoming", help="path to the downloaded jobs.db")
    p.add_argument("--apply", action="store_true",
                   help="actually swap it in; without this it only reports")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("export", help="write a publishable corpus with no user marks")
    p.add_argument("--out", required=True, help="destination path")
    p.set_defaults(fn=cmd_export)

    p = sub.add_parser("prune", help="remove jobs older than the age cutoff")
    p.add_argument("--apply", action="store_true",
                   help="actually delete; without this it only reports")
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser("reconcile",
                       help="apply the role filter to rows ingested before it "
                            "existed (re-tag matches, drop off-role)")
    p.add_argument("--apply", action="store_true",
                   help="actually change rows; without this it only reports")
    p.set_defaults(fn=cmd_reconcile)

    p = sub.add_parser("stats", help="corpus size, fill rates, slice truncation")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("serve", help="local web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)

    args = ap.parse_args(argv)
    roles = getattr(args, "roles2", None) or args.roles
    locations = getattr(args, "locations2", None) or args.locations
    max_age = getattr(args, "max_age2", None)
    if max_age is None:
        max_age = args.max_age
    cfg = Config.load(args.targets or TARGETS_PATH, roles, locations, max_age)

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

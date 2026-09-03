"""Stage A -- role slug catalogue.

Recall is capped by this catalogue. There is no published list of valid role
slugs, and an invalid one does not 404 -- it silently serves an unfiltered
location-wide search. So every candidate is validated by reading the GraphQL
cache key the server embeds in the page, which records what it ACTUALLY
filtered on.

Discovery: seed slug -> fetch its role pages -> harvest sibling role links from
footer blocks -> repeat to depth 2. Validation: cross every discovered slug
against every requested location and record totalJobCount.

A slug that fails validation is recorded with the reason rather than dropped --
the rejected list is itself a finding (it tells you which plausible-looking
slugs silently degrade).
"""

from __future__ import annotations

import json
import re
from typing import Any

from .. import assertions as A
from ..cache import write_results
from ..store import connect, mark_unit, now, unit_done
from . import common as C

STAGE = "a_catalogue"


def probe_slug(conn, role: str, location: str, refresh: bool = False) -> dict[str, Any]:
    """Fetch one (role, location) landing page and decide whether the role
    filter was actually applied. Never trusts the URL."""
    url = C.search_url(role, location, 1)
    rec: dict[str, Any] = {
        "role_slug": role,
        "location_slug": location,
        "url": url,
        "valid": False,
        "total_job_count": None,
        "page_count": None,
        "page_type": None,
        "reason": None,
    }
    resp = C.get(conn, STAGE, url, refresh=refresh)
    if resp.status == 404:
        rec["reason"] = "HTTP 404 -- slug does not exist"
        return rec
    if not resp.ok:
        rec["reason"] = f"HTTP {resp.status}"
        return rec

    nd = C.next_data(resp.text, url)
    rec["page_type"] = nd.get("page")
    store = C.apollo(nd, url)
    key, node = C.search_node(store, url)
    args = A.parse_query_key(key)
    rec["gql_args"] = args
    rec["total_job_count"] = node.get("totalJobCount")
    rec["page_count"] = node.get("pageCount")
    rec["per_page"] = node.get("perPage")

    if "role" not in args:
        rec["reason"] = (
            f"SILENT FALLBACK -- server args {args} carry no 'role'; page rendered as "
            f"{nd.get('page')!r}. Results would be location-wide."
        )
        return rec
    if args["role"] != role:
        rec["reason"] = f"server filtered on role={args['role']!r}, not {role!r}"
        return rec

    # The remote variant must actually be remote-scoped.
    if location == "remote" and not args.get("remote"):
        rec["reason"] = f"expected remote:true in args, got {args}"
        return rec

    rec["valid"] = True
    return rec


def slugify_role_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


# Role titles that are category buckets rather than searchable roles. Including
# them just burns requests on guaranteed silent-fallbacks.
TITLE_STOPLIST = {"other", "engineering", "operations", "sales", "marketing", "design"}


def discover(conn, seeds: list[str], locations: list[str], depth: int = 2,
             max_candidates: int = 60, refresh: bool = False) -> dict[str, dict]:
    """Breadth-first role-slug harvest from two channels.

    (1) Footer link blocks. MEASURED LOW YIELD: Wellfound serves an identical
        generic 14-slug block on every role page (account-manager, data-analyst,
        ... software-engineer). It is not a related-roles widget, so it never
        expands into the AI family. Kept because it is free -- the page is
        already fetched -- but it is not the productive channel.

    (2) `primaryRoleTitle` on the JobListingSearchResult nodes. These are
        Wellfound's own canonical role names for the jobs actually returned, so
        slugifying them yields real, in-family slugs (Machine Learning Engineer
        -> machine-learning-engineer). This is the channel that works.
    """
    found: dict[str, dict] = {s: {"found_via": "seed", "depth": 0} for s in seeds}
    frontier = list(seeds)
    channel_stats = {"footer": 0, "primary_role_title": 0}

    for d in range(depth):
        if not frontier:
            break
        next_frontier: list[str] = []
        for slug in frontier:
            if len(found) >= max_candidates:
                break
            url = C.search_url(slug, "remote", 1)
            resp = C.get(conn, STAGE, url, refresh=refresh)
            if not resp.ok:
                continue

            for cand, _loc in C.harvest_role_links(resp.text):
                if cand not in found and len(found) < max_candidates:
                    found[cand] = {"found_via": f"footer:{slug}", "depth": d + 1}
                    next_frontier.append(cand)
                    channel_stats["footer"] += 1

            try:
                store = C.apollo(C.next_data(resp.text, url), url)
            except A.SchemaDrift:
                store = {}
            for node in store.values():
                if not (isinstance(node, dict)
                        and node.get("__typename") == "JobListingSearchResult"):
                    continue
                title = (node.get("primaryRoleTitle") or "").strip()
                if not title:
                    continue
                cand = slugify_role_title(title)
                if not cand or cand in TITLE_STOPLIST or cand in found:
                    continue
                if len(found) < max_candidates:
                    found[cand] = {"found_via": f"primaryRoleTitle:{slug}", "depth": d + 1}
                    next_frontier.append(cand)
                    channel_stats["primary_role_title"] += 1

            print(f"  depth {d+1}: {slug} -> {len(found)} candidates known")
        frontier = next_frontier

    print(f"  discovery channels: {channel_stats}")

    for slug, meta in found.items():
        conn.execute(
            "INSERT OR IGNORE INTO role_candidate (role_slug,found_via,depth,seen_at) "
            "VALUES (?,?,?,?)",
            (slug, meta["found_via"], meta["depth"], now()),
        )
    conn.commit()
    return found


def run(
    seeds: list[str],
    locations: list[str],
    depth: int = 2,
    max_candidates: int = 60,
    refresh: bool = False,
    resume: bool = True,
    **_: Any,
) -> dict[str, Any]:
    conn = connect()
    print(f"\n=== STAGE A: role catalogue ===")
    print(f"  seeds={seeds} locations={locations} depth={depth}")

    found = discover(conn, seeds, locations, depth, max_candidates, refresh)
    print(f"  discovered {len(found)} candidate role slugs")

    rows: list[dict[str, Any]] = []
    for slug in sorted(found):
        for loc in locations:
            unit = f"{slug}|{loc}"
            if resume and unit_done(conn, STAGE, unit):
                r = conn.execute(
                    "SELECT * FROM role_slug WHERE role_slug=? AND location_slug=?",
                    (slug, loc),
                ).fetchone()
                if r:
                    rows.append(dict(r))
                    continue
            rec = probe_slug(conn, slug, loc, refresh=refresh)
            conn.execute(
                "INSERT INTO role_slug (role_slug,location_slug,valid,total_job_count,"
                "page_count,reason,page_type,checked_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(role_slug,location_slug) DO UPDATE SET "
                "valid=excluded.valid, total_job_count=excluded.total_job_count, "
                "page_count=excluded.page_count, reason=excluded.reason, "
                "page_type=excluded.page_type, checked_at=excluded.checked_at",
                (slug, loc, int(rec["valid"]), rec["total_job_count"], rec["page_count"],
                 rec["reason"], rec["page_type"], now()),
            )
            mark_unit(conn, STAGE, unit, "done", rec["reason"], rec["total_job_count"])
            conn.commit()
            rows.append(rec)
            flag = "OK  " if rec["valid"] else "SKIP"
            print(
                f"  {flag} {slug:34} {loc:10} jobs={str(rec['total_job_count']):>7} "
                f"{'' if rec['valid'] else (rec['reason'] or '')[:52]}"
            )

    valid = [r for r in rows if r["valid"]]
    invalid = [r for r in rows if not r["valid"]]
    by_loc: dict[str, int] = {}
    for r in valid:
        by_loc[r["location_slug"]] = by_loc.get(r["location_slug"], 0) + (r["total_job_count"] or 0)

    result = {
        "stage": "a_catalogue",
        "seeds": seeds,
        "locations": locations,
        "n_candidates": len(found),
        "n_valid_pairs": len(valid),
        "n_invalid_pairs": len(invalid),
        "valid": sorted(valid, key=lambda r: -(r["total_job_count"] or 0)),
        "invalid": invalid,
        "addressable_job_count_by_location": by_loc,
        "addressable_job_count_total_with_overlap": sum(by_loc.values()),
    }
    write_results("role_catalogue.json", result)
    return result

"""Fetch one role from every enabled source.

The three sources filter by role very differently, and this module's job is to
dispatch each one correctly *and* report honestly which kind of filtering
actually happened. That is what `filter_mode` is for:

    "server"        Wellfound. /role/{slug} really is role-filtered server-side.
    "local"         RemoteOK and Himalayas. Neither can filter by role usefully,
                    so the whole filter is ours:
                      * Himalayas ignores every filter parameter outright.
                      * RemoteOK's ?tag= "works" but its tag endpoints serve an
                        archive (median age 112-144 days) while the unfiltered
                        feed is fresh (median 5 days) -- so a tag plus the age
                        cutoff yields nothing. We take the live feed instead.
    "server+local"  Reserved for a source where a real server-side filter is
                    worth combining with ours. Nothing uses it today.
    "skipped"       The source has no usable query for this role.

The UI must surface that field. "Fetched 40 jobs for artificial-intelligence-
engineer" is misleading if the reader assumes the source did the filtering.

Kept out of `crawl.py` deliberately: that module is the Wellfound ingester and
carries the slice machinery and the four Wellfound-specific assertions, none of
which mean anything to a JSON API.
"""

from __future__ import annotations

from typing import Any, Callable

from . import store as S
from .config import Config

WELLFOUND = "wellfound"


def sources_for(cfg: Config) -> list[str]:
    """Registered sources that are enabled, wellfound first."""
    from . import sources as SRC

    names = [n for n in SRC.registry() if cfg.source_enabled(n)]
    return sorted(names, key=lambda n: (n != WELLFOUND, n))


def fetch_role(
    fetcher,
    conn,
    cfg: Config,
    role: str,
    sources: list[str] | None = None,
    cutoff_ts: int | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> list[dict[str, Any]]:
    """Fetch `role` from each source. Returns one result dict per source.

    Never raises for a source that simply cannot serve the role -- that is
    recorded as `filter_mode: "skipped"` with a reason. Integrity and
    mitigation errors DO propagate: those must stop the run.
    """
    from . import sources as SRC
    from .crawl import crawl_slice, validate_role

    reg = SRC.registry()
    names = sources if sources is not None else sources_for(cfg)
    cutoff = cutoff_ts if cutoff_ts is not None else cfg.cutoff_ts()
    keywords = cfg.role_keywords(role)
    categories = cfg.role_categories(role)
    out: list[dict[str, Any]] = []

    for name in names:
        if name not in reg:
            out.append(_skipped(name, role, f"unknown source {name!r}"))
            continue

        if name == WELLFOUND:
            if role not in cfg.roles:
                out.append(_skipped(name, role,
                                    "not in targets.yaml roles; refusing to "
                                    "crawl an unvalidated slug"))
                continue
            v = validate_role(fetcher, conn, role, "anywhere")
            if not v["valid"]:
                # Never silently fall back to an unfiltered search.
                out.append(_skipped(name, role, v["reason"] or "role not applied"))
                continue
            r = crawl_slice(
                fetcher, conn, role, "anywhere", cfg.max_pages_per_slice,
                cfg.yield_floor, resume=True, cutoff_ts=cutoff,
                on_page=lambda d: on_event and on_event({**d, "source": name}))
            out.append({
                "source": name, "role": role, "filter_mode": "server",
                "seen": r.jobs_seen, "new": r.jobs_new,
                "stale_skipped": r.jobs_stale_skipped, "filtered_out": 0,
                "pages": r.pages_walked, "claimed": r.total_claimed,
                "ended_reason": r.ended_reason,
            })
            continue

        # JSON-API sources.
        # RemoteOK: deliberately NO tag. Its tag endpoints return an archive
        # (median age 112-144 days) while the unfiltered feed is fresh (median
        # 5 days), so a tag plus the age cutoff yields nothing. Filter locally
        # against the live feed instead. See src/sources/remoteok.py.
        tag = None if name == "remoteok" else cfg.role_query(role, name)
        if not keywords and name != WELLFOUND:
            out.append(_skipped(name, role,
                                "no keywords in role_map; a local filter with no "
                                "keywords would keep everything"))
            continue

        target = {"role": role, "tag": tag, "keywords": keywords,
                  "categories": categories, "name": role}
        r = reg[name].ingest(
            fetcher, conn, target, cutoff_ts=cutoff,
            max_pages=cfg.source_max_pages(name),
            on_page=lambda d: on_event and on_event({**d, "source": name}))
        out.append({
            "source": name, "role": role,
            "filter_mode": r.get("filter_mode", "local"),
            "seen": r["seen"], "new": r["new"],
            "stale_skipped": r.get("stale_skipped", 0),
            "filtered_out": r.get("filtered_out", 0),
            "pages": r.get("pages", 0), "claimed": r.get("claimed"),
            "ended_reason": r.get("ended_reason"), "query": tag,
        })

    S.rebuild_locations(conn)
    return out


def _skipped(source: str, role: str, reason: str) -> dict[str, Any]:
    return {"source": source, "role": role, "filter_mode": "skipped",
            "reason": reason, "seen": 0, "new": 0, "stale_skipped": 0,
            "filtered_out": 0, "pages": 0, "claimed": None,
            "ended_reason": "skipped"}


def describe(results: list[dict[str, Any]]) -> str:
    """One line per source, for the CLI and the UI status box."""
    lines = []
    for r in results:
        if r["filter_mode"] == "skipped":
            lines.append(f"  {r['source']:10} skipped — {r.get('reason','')}")
            continue
        scanned = r["seen"] + r["filtered_out"] + r["stale_skipped"]
        lines.append(
            f"  {r['source']:10} {r['seen']:>4} kept / {scanned:>5} scanned "
            f"({r['new']} new, {r['filtered_out']} off-role, "
            f"{r['stale_skipped']} too old) "
            f"[{r['filter_mode']}]")
    return "\n".join(lines)

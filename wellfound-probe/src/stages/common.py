"""Shared fetch + parse path for every Phase 2 stage.

Not in the brief's file list, but every stage needs the same three things and
duplicating them is how one stage quietly ends up without an assertion:

  * fetch through the Phase 1 polite cache, log cf-ray/cf-mitigated on EVERY
    response, and halt on the first sign of mitigation
  * extract __NEXT_DATA__ with a schema-drift check
  * pull raw Apollo nodes out verbatim (no normalization -- that is derive.py's
    job, and it happens in SQL)
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

from .. import assertions as A
from ..cache import fetch, Response
from ..robots import assert_allowed
from ..store import log_request

BASE = "https://wellfound.com"


def get(conn, stage: str, url: str, *, refresh: bool = False, verbose: bool = False) -> Response:
    """Robots-gated, cache-backed, fully logged, mitigation-halting GET."""
    assert_allowed(url)
    resp = fetch(url, refresh=refresh, verbose=verbose)
    log_request(conn, stage, resp)
    conn.commit()
    if resp.error:
        raise RuntimeError(f"{url}: transport failure: {resp.error}")
    A.check_mitigation(resp, url)
    return resp


def next_data(html: str, url: str) -> dict:
    A.check_schema(html, url)
    tag = BeautifulSoup(html, "lxml").find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        raise A.SchemaDrift(f"{url}: __NEXT_DATA__ tag present-but-empty")
    return json.loads(tag.string)


def apollo(nd: dict, url: str) -> dict:
    try:
        return nd["props"]["pageProps"]["apolloState"]["data"]
    except (KeyError, TypeError) as e:
        raise A.SchemaDrift(f"{url}: no props.pageProps.apolloState.data ({e})") from e


def search_node(store: dict, url: str) -> tuple[str, dict]:
    """Find the seoLandingPageJobSearchResults entry and return (cache_key, node)."""
    try:
        root = store["ROOT_QUERY"]["talent"]
    except (KeyError, TypeError) as e:
        raise A.SchemaDrift(f"{url}: no ROOT_QUERY.talent ({e})") from e
    for k, v in root.items():
        if "seoLandingPageJobSearchResults" in k:
            return k, v
    raise A.SchemaDrift(f"{url}: no seoLandingPageJobSearchResults in Apollo cache")


def deref(store: dict, ref: Any) -> dict | None:
    if isinstance(ref, dict) and "__ref" in ref:
        return store.get(ref["__ref"])
    return ref if isinstance(ref, dict) else None


def ref_id(ref: Any) -> str | None:
    if isinstance(ref, dict) and "__ref" in ref:
        return ref["__ref"].split(":", 1)[-1]
    return None


def extract_startups_and_jobs(
    store: dict, node: dict
) -> list[tuple[dict, list[tuple[str, dict]]]]:
    """[(startup_raw, [(job_id, job_raw), ...]), ...] -- all nodes verbatim."""
    out = []
    for sref in node.get("startups", []) or []:
        startup = deref(store, sref)
        if not startup:
            continue
        jobs = []
        for jref in startup.get("highlightedJobListings", []) or []:
            j = deref(store, jref)
            jid = ref_id(jref)
            if j and jid:
                jobs.append((jid, j))
        out.append((startup, jobs))
    return out


def search_url(role: str | None, location: str | None, page: int = 1) -> str:
    """Wellfound's three SEO landing shapes.

    location == 'remote' maps to /role/r/{role}, which is a DIFFERENT page type
    (remoteSearch) whose GraphQL args carry remote:true rather than a location.
    """
    q = f"?page={page}" if page > 1 else ""
    if role and location and location != "remote":
        return f"{BASE}/role/l/{role}/{location}{q}"
    if role and location == "remote":
        return f"{BASE}/role/r/{role}{q}"
    if role:
        return f"{BASE}/role/r/{role}{q}"
    if location:
        return f"{BASE}/location/{location}{q}"
    raise ValueError("need a role or a location")


ROLE_HREF_RE = re.compile(r"^/role/(?:l/)?([a-z0-9][a-z0-9-]*)(?:/([a-z0-9-]+))?/?$")


def harvest_role_links(html: str) -> list[tuple[str, str | None]]:
    """Footer/related-link blocks list sibling roles and role-in-location pairs.
    This is the only discovery surface for role slugs -- there is no published list."""
    out: list[tuple[str, str | None]] = []
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        href = a["href"].split("?")[0]
        m = ROLE_HREF_RE.match(href)
        if not m:
            continue
        slug, loc = m.group(1), m.group(2)
        if slug in ("r", "l"):
            continue
        out.append((slug, loc))
    return out

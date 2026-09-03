"""__NEXT_DATA__ / Apollo extraction.

Wellfound is a Next.js Pages Router app that ships a normalized Apollo cache in
the initial HTML. Everything we need is in there; no JS execution required.

Shape (verified against live nodes):

    props.pageProps.apolloState.data
      ROOT_QUERY.talent
        seoLandingPageJobSearchResults({"location":..,"page":N,"role":..})
          -> {totalJobCount, totalStartupCount, perPage, pageCount, startups:[__ref]}
      StartupResult:<id>
          -> {name, slug, companySize, highConcept, badges:[__ref], highlightedJobListings:[__ref]}
      JobListingSearchResult:<id>
          -> {title, description, compensation, locationNames, remote, remoteConfig,
              liveStartAt, slug, jobType, atsSource, primaryRoleTitle, autoPosted}

Nothing here normalizes or reshapes: raw nodes go to storage verbatim and the
filterable columns are derived in SQL. Re-deriving a column is free; re-crawling
is not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from bs4 import BeautifulSoup

from . import assertions as A


@dataclass
class SearchPage:
    query_key: str
    args: dict
    total_job_count: int | None
    total_startup_count: int | None
    per_page: int | None
    page_count: int | None
    page_type: str | None
    # [(startup_raw, [(job_id, job_raw), ...]), ...]
    startups: list[tuple[dict, list[tuple[str, dict]]]] = field(default_factory=list)

    @property
    def n_jobs(self) -> int:
        return sum(len(js) for _, js in self.startups)

    @property
    def n_companies(self) -> int:
        return len(self.startups)


def next_data(html: str, url: str) -> dict:
    A.check_schema(html, url)
    tag = BeautifulSoup(html, "lxml").find("script", id="__NEXT_DATA__")
    if tag is None or not tag.string:
        raise A.SchemaDrift(f"{url}: __NEXT_DATA__ tag present but empty")
    return json.loads(tag.string)


def apollo_store(nd: dict, url: str) -> dict:
    try:
        return nd["props"]["pageProps"]["apolloState"]["data"]
    except (KeyError, TypeError) as e:
        raise A.SchemaDrift(f"{url}: no props.pageProps.apolloState.data ({e})") from e


def _deref(store: dict, ref: Any) -> dict | None:
    if isinstance(ref, dict) and "__ref" in ref:
        return store.get(ref["__ref"])
    return ref if isinstance(ref, dict) else None


def _ref_id(ref: Any) -> str | None:
    if isinstance(ref, dict) and "__ref" in ref:
        return ref["__ref"].split(":", 1)[-1]
    return None


def parse_search_page(html: str, url: str) -> SearchPage:
    """Full parse of one search page. Raises SchemaDrift if the shape moved."""
    nd = next_data(html, url)
    store = apollo_store(nd, url)
    try:
        root = store["ROOT_QUERY"]["talent"]
    except (KeyError, TypeError) as e:
        raise A.SchemaDrift(f"{url}: no ROOT_QUERY.talent ({e})") from e

    key = node = None
    for k, v in root.items():
        if "seoLandingPageJobSearchResults" in k:
            key, node = k, v
            break
    if key is None:
        raise A.SchemaDrift(f"{url}: no seoLandingPageJobSearchResults in Apollo cache")

    page = SearchPage(
        query_key=key,
        args=A.parse_query_key(key),
        total_job_count=node.get("totalJobCount"),
        total_startup_count=node.get("totalStartupCount"),
        per_page=node.get("perPage"),
        page_count=node.get("pageCount"),
        page_type=nd.get("page"),
    )

    for sref in node.get("startups", []) or []:
        startup = _deref(store, sref)
        if not startup:
            continue
        jobs: list[tuple[str, dict]] = []
        for jref in startup.get("highlightedJobListings", []) or []:
            j = _deref(store, jref)
            jid = _ref_id(jref)
            if j and jid:
                jobs.append((jid, j))
        # Badges are __refs; resolve them into the stored company node so the
        # raw JSON is self-contained and the view can read them without a join.
        badges = [
            b.get("label") or b.get("name")
            for b in (_deref(store, r) for r in startup.get("badges", []) or [])
            if b
        ]
        startup = dict(startup)
        startup["_badges"] = [b for b in badges if b]
        page.startups.append((startup, jobs))

    return page

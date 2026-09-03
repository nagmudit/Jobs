"""Synthetic __NEXT_DATA__ pages for the assertion tests.

Built by hand rather than saved from the wire: a real search page is ~500KB of
mostly irrelevant markup, and the failure modes we care about are all shapes of
the Apollo cache. These fixtures reproduce each shape exactly and run offline.
"""

from __future__ import annotations

import json


def _job(jid: str, title: str, **over) -> dict:
    d = {
        "__typename": "JobListingSearchResult",
        "title": title,
        "slug": title.lower().replace(" ", "-"),
        "description": f"{title} description",
        "compensation": "$25k – $50k",
        "locationNames": ["Bengaluru"],
        "acceptedRemoteLocationNames": [],
        "remote": False,
        "remoteConfig": {"__typename": "RemoteConfig", "kind": "ONSITE",
                         "wfhFlexible": False},
        "liveStartAt": 1787731575,
        "jobType": "full-time",
        "atsSource": None,
        "primaryRoleTitle": "Software Engineer",
        "autoPosted": False,
    }
    d.update(over)
    return d


def page(role: str | None = "ai-engineer", location: str | None = "bangalore",
         page_no: int = 1, n_companies: int = 2, jobs_per_company: int = 2,
         total_job_count: int = 215, page_count: int = 11,
         remote: bool = False) -> str:
    """Render a minimal but structurally faithful search page."""
    args: dict = {"page": page_no}
    if location and not remote:
        args["location"] = location
    if remote:
        args["remote"] = True
    if role:
        args["role"] = role
    # Apollo cache keys serialise args with sorted keys.
    key = f"seoLandingPageJobSearchResults({json.dumps(args, sort_keys=True)})"

    store: dict = {"ROOT_QUERY": {"__typename": "Query", "talent": {"__typename": "Talent"}}}
    startup_refs = []
    jid = 1000 * page_no
    for c in range(n_companies):
        sid = f"S{page_no}{c}"
        job_refs = []
        for j in range(jobs_per_company):
            jid += 1
            store[f"JobListingSearchResult:{jid}"] = _job(str(jid), f"Engineer {jid}")
            job_refs.append({"__ref": f"JobListingSearchResult:{jid}"})
        store[f"StartupResult:{sid}"] = {
            "__typename": "StartupResult", "id": sid, "name": f"Company {sid}",
            "slug": f"company-{sid.lower()}", "companySize": "SIZE_11_50",
            "highConcept": "We do things", "badges": [],
            "highlightedJobListings": job_refs,
        }
        startup_refs.append({"__ref": f"StartupResult:{sid}"})

    store["ROOT_QUERY"]["talent"][key] = {
        "__typename": "JobSearchResults",
        "totalJobCount": total_job_count,
        "totalStartupCount": 514,
        "perPage": 20,
        "pageCount": page_count,
        "startups": startup_refs,
    }

    nd = {
        "props": {"pageProps": {"apolloState": {"data": store}}},
        "page": "/seoLanding/roleLocationSearch" if role else "/seoLanding/locationSearch",
        "query": {}, "buildId": "test-build",
    }
    return (
        "<!doctype html><html><head><title>t</title></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
        "</body></html>"
    )


def app_router_page() -> str:
    """Post-migration shape: no __NEXT_DATA__, RSC flight chunks instead."""
    return (
        "<!doctype html><html><body>"
        '<script>self.__next_f.push([1,"a:[\\"$\\",\\"div\\"]"])</script>'
        '<script>self.__next_f.push([1,"b:more"])</script>'
        "</body></html>"
    )


def unknown_page() -> str:
    return "<!doctype html><html><body><p>maintenance</p></body></html>"


def detail_page(equity: str = "0.0% – 1.0%", salary: str = "$25k – $50k",
                with_jsonld: bool = True) -> str:
    ld = {
        "@context": "https://schema.org", "@type": "JobPosting",
        "title": "AI Engineer",
        "description": "<p>Build <b>things</b></p>",
        "datePosted": "2026-08-21T16:31:01Z",
        "hiringOrganization": {"@type": "Organization", "name": "Smart Audit"},
        # Wellfound puts the COMPANY here, not a job id -- the parser must not use it.
        "identifier": {"@type": "PropertyValue", "name": "Smart Audit"},
    }
    block = (f'<script type="application/ld+json">{json.dumps(ld)}</script>'
             if with_jsonld else "")
    return (
        f"<!doctype html><html><head>{block}</head><body>"
        f"<div>AI Engineer {salary} • {equity} | Remote</div></body></html>"
    )

"""P1 -- Static HTTP + embedded JSON.

Plain httpx GET against the public SEO landing pages. Wellfound is a Next.js
Pages Router app that ships a normalized Apollo cache inside __NEXT_DATA__, so
the listing data is fully present in the initial HTML -- no JS execution, no
login, no anti-bot handling required.

Two traps this probe explicitly guards against:

1. SILENT ROLE FALLBACK. /role/l/{role}/{location} with an unrecognized role
   slug does NOT 404. It quietly serves an unfiltered location-wide search
   (page becomes /seoLanding/locationSearch and the GraphQL cache key loses its
   "role" member). You get 200s and plausible-looking jobs that are not
   filtered by role at all. We detect this and fail loudly.

2. PAGINATION PARAM. The site paginates via ?page=N, but on the fallback page
   the canonical next-link drops the role (/location/{loc}?page=2). We verify
   the returned cache key actually carries the page we asked for.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..cache import fetch, write_results, Response
from ..models import Job, coverage
from ..robots import assert_allowed

BASE = "https://wellfound.com"

# Signatures of an anti-bot interstitial. Detection only -- we never attempt
# to satisfy a challenge. Note that a Turnstile *script tag* being present in
# the bundle is not the same as being served a challenge; we separate the two.
CHALLENGE_MARKERS = [
    ("cf_interstitial", r"cf-browser-verification|cf_chl_opt|/cdn-cgi/challenge-platform/.*orchestrate"),
    ("cf_blocked", r"Attention Required!|Sorry, you have been blocked"),
    ("turnstile_widget_ref", r"turnstile|cf-turnstile"),
    ("captcha", r"recaptcha|hcaptcha"),
]


class SilentRoleFallback(RuntimeError):
    """Raised when Wellfound ignores the role slug and serves location-wide results."""


def page_url(role: str | None, location: str | None, page: int) -> str:
    q = f"?page={page}" if page > 1 else ""
    if role and location:
        return f"{BASE}/role/l/{role}/{location}{q}"
    if role:
        return f"{BASE}/role/r/{role}{q}"
    if location:
        return f"{BASE}/location/{location}{q}"
    raise ValueError("need at least one of role/location")


def classify(resp: Response) -> dict[str, Any]:
    """What did we actually get back? Page, challenge, block, or nothing."""
    out: dict[str, Any] = {
        "status": resp.status,
        "error": resp.error,
        "final_url": resp.final_url,
        "bytes": len(resp.text),
        "server": resp.headers.get("server"),
        "cf_ray": resp.headers.get("cf-ray"),
        "cf_mitigated": resp.headers.get("cf-mitigated"),
        "content_type": resp.headers.get("content-type"),
        "markers": [],
    }
    for name, pat in CHALLENGE_MARKERS:
        if re.search(pat, resp.text, re.I):
            out["markers"].append(name)
    if resp.text:
        try:
            txt = BeautifulSoup(resp.text, "lxml").get_text(" ", strip=True)
        except Exception as e:  # parser failure is itself a finding
            txt = f"<html parse failed: {type(e).__name__}: {e}>"
        out["text_snippet"] = txt[:400]
    return out


def extract_next_data(html: str) -> dict | None:
    tag = BeautifulSoup(html, "lxml").find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    return json.loads(tag.string)  # loud on malformed JSON, by design


def search_result_key(apollo_root: dict) -> tuple[str, dict] | tuple[None, None]:
    for k, v in apollo_root.items():
        if "seoLandingPageJobSearchResults" in k:
            return k, v
    return None, None


def parse_apollo(nd: dict, url: str) -> dict[str, Any]:
    """Pull jobs out of the normalized Apollo cache, joining company via the
    StartupResult parent (a job object alone has no company reference)."""
    pp = nd["props"]["pageProps"]
    store = pp["apolloState"]["data"]
    root = store["ROOT_QUERY"]["talent"]
    key, res = search_result_key(root)
    if key is None:
        raise RuntimeError(f"no seoLandingPageJobSearchResults in Apollo cache for {url}")

    # The cache key is literal JSON of the GraphQL args -- our ground truth for
    # what the server ACTUALLY filtered on, regardless of what the URL implied.
    args = json.loads(key[key.index("(") + 1 : key.rindex(")")])

    def deref(ref: Any) -> dict | None:
        if isinstance(ref, dict) and "__ref" in ref:
            return store.get(ref["__ref"])
        return ref if isinstance(ref, dict) else None

    jobs: list[Job] = []
    companies: list[dict[str, Any]] = []
    for sref in res.get("startups", []):
        startup = deref(sref)
        if not startup:
            continue
        # atsSource is a free, first-party hint about which ATS a company uses
        # (e.g. "AtsIntegration::Greenhouse::Listing"). It is ground truth for
        # validating P6's slug guessing, and lets P6 try the right provider first.
        ats_hints: list[str] = []
        job_refs = startup.get("highlightedJobListings", []) or []
        for jref in job_refs:
            j = deref(jref)
            if not j:
                continue
            src = j.get("atsSource")
            if src:
                prov = str(src).split("::")[1] if "::" in str(src) else str(src)
                ats_hints.append(prov)
            jid = None
            if isinstance(jref, dict) and "__ref" in jref:
                jid = jref["__ref"].split(":", 1)[-1]
            jobs.append(to_job(j, jid, startup, url))
        companies.append(
            {
                "id": startup.get("id"),
                "name": startup.get("name"),
                "slug": startup.get("slug"),
                "company_size": startup.get("companySize"),
                "high_concept": startup.get("highConcept"),
                "ats_hints": sorted(set(ats_hints)),
            }
        )

    return {
        "gql_args": args,
        "role_applied": "role" in args,
        "page_returned": args.get("page"),
        "total_job_count": res.get("totalJobCount"),
        "total_startup_count": res.get("totalStartupCount"),
        "per_page": res.get("perPage"),
        "page_count": res.get("pageCount"),
        "jobs": jobs,
        "companies": companies,
        "next_page_type": nd.get("page"),
    }


def to_job(j: dict, jid: str | None, startup: dict, url: str) -> Job:
    locs = j.get("locationNames") or []
    remote_locs = j.get("acceptedRemoteLocationNames") or []
    loc_raw = ", ".join(str(x) for x in locs) or ", ".join(str(x) for x in remote_locs) or None
    slug = j.get("slug")
    ts = j.get("liveStartAt")
    posted = None
    if isinstance(ts, (int, float)):
        from datetime import datetime, timezone

        posted = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return Job(
        source="p1_static",
        source_job_id=jid,
        company=startup.get("name"),
        title=j.get("title"),
        location_raw=loc_raw,
        remote=j.get("remote"),
        description=j.get("description"),
        salary_raw=j.get("compensation"),
        equity_raw=None,  # not present in JobListingSearchResult -- measured, not assumed
        posted_at=posted,
        apply_url=urljoin(url, f"/jobs/{jid}-{slug}") if (jid and slug) else None,
    )


def run(
    role: str,
    location: str,
    pages: int = 2,
    refresh: bool = False,
    strict_role: bool = True,
) -> dict[str, Any]:
    print(f"\n=== P1 static HTTP :: role={role} location={location} pages={pages} ===")
    findings: list[dict[str, Any]] = []
    all_jobs: list[Job] = []
    all_companies: dict[str, dict] = {}

    for p in range(1, pages + 1):
        url = page_url(role, location, p)
        assert_allowed(url)
        resp = fetch(url, refresh=refresh)
        f = classify(resp)
        f["url"] = url
        f["requested_page"] = p

        if resp.ok:
            nd = extract_next_data(resp.text)
            if nd is None:
                f["parse"] = "NO __NEXT_DATA__ SCRIPT TAG"
            else:
                parsed = parse_apollo(nd, url)
                f.update(
                    {
                        "gql_args": parsed["gql_args"],
                        "role_applied": parsed["role_applied"],
                        "page_returned": parsed["page_returned"],
                        "page_matches_request": parsed["page_returned"] == p,
                        "total_job_count": parsed["total_job_count"],
                        "page_count": parsed["page_count"],
                        "per_page": parsed["per_page"],
                        "n_jobs_on_page": len(parsed["jobs"]),
                        "n_companies_on_page": len(parsed["companies"]),
                        "next_page_type": parsed["next_page_type"],
                    }
                )
                if strict_role and not parsed["role_applied"]:
                    raise SilentRoleFallback(
                        f"Wellfound IGNORED role={role!r} for {url}. "
                        f"GraphQL args were {parsed['gql_args']} (no 'role' member) and the "
                        f"page rendered as {parsed['next_page_type']!r}. Results would be "
                        f"location-wide, not role-filtered. Use a valid role slug."
                    )
                all_jobs.extend(parsed["jobs"])
                for c in parsed["companies"]:
                    if c.get("slug"):
                        all_companies[c["slug"]] = c
        findings.append(f)
        print(
            f"  p{p}: status={f['status']} bytes={f['bytes']} "
            f"role_applied={f.get('role_applied')} page_returned={f.get('page_returned')} "
            f"jobs={f.get('n_jobs_on_page')} companies={f.get('n_companies_on_page')} "
            f"total={f.get('total_job_count')}"
        )

    result = {
        "probe": "p1_static",
        "role": role,
        "location": location,
        "pages_requested": pages,
        "verdict": verdict(findings, all_jobs),
        "findings": findings,
        "n_jobs": len(all_jobs),
        "n_companies": len(all_companies),
        "companies": sorted(all_companies.values(), key=lambda c: c.get("name") or ""),
        "coverage": coverage(all_jobs),
        "jobs": [j.to_dict() for j in all_jobs],
    }
    write_results("p1_static.json", result)
    return result


def verdict(findings: list[dict], jobs: list[Job]) -> str:
    if not jobs:
        statuses = sorted({f["status"] for f in findings if f["status"]})
        return f"FAILED -- no jobs; statuses={statuses}"
    pages_ok = all(f.get("page_matches_request") for f in findings if f.get("status") == 200)
    roles_ok = all(f.get("role_applied") for f in findings if f.get("status") == 200)
    note = []
    if not pages_ok:
        note.append("PAGINATION NOT HONORED")
    if not roles_ok:
        note.append("ROLE NOT APPLIED")
    return "WORKS" + (" -- but " + "; ".join(note) if note else "")

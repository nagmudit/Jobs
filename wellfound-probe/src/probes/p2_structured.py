"""P2 -- Structured data (JSON-LD) + robots/sitemap surface.

Two separate questions:

  (a) Do Wellfound pages carry schema.org/JobPosting JSON-LD? If so, what
      fields does it give us versus the 12 we need? JSON-LD is published
      deliberately for Google for Jobs, which makes it the most durable
      contract on the site -- breaking it costs them search traffic.

  (b) What does robots.txt/sitemap.xml expose as a crawlable URL surface?
      Wellfound advertises GZIPPED sitemaps (sitemap.xml.gz), not the plain
      .xml the brief assumed, so we handle both and decompress.

Note the two page families behave differently and that is the headline:
  * /role/l/... search pages  -> Next.js, __NEXT_DATA__, NO JSON-LD
  * /jobs/{id}-{slug} detail  -> server-rendered, JSON-LD, NO __NEXT_DATA__
"""

from __future__ import annotations

import gzip
import io
import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..cache import fetch, write_results
from ..models import Job, coverage
from ..robots import assert_allowed

BASE = "https://wellfound.com"

# What JSON-LD JobPosting can supply, mapped onto our 12-field Job.
LD_TO_JOB = {
    "title": "title",
    "hiringOrganization": "company",
    "jobLocation": "location_raw",
    "jobLocationType": "remote",
    "description": "description",
    "baseSalary": "salary_raw",
    "datePosted": "posted_at",
    "identifier": "source_job_id",
    "url": "apply_url",
}


def extract_jsonld(html: str) -> list[dict]:
    """All application/ld+json blocks, flattened through @graph."""
    out: list[dict] = []
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # Loud: a malformed JSON-LD block is a real finding about fragility.
            out.append({"__parse_error__": str(e), "__raw_head__": raw[:200]})
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict) and "@graph" in it:
                out.extend(x for x in it["@graph"] if isinstance(x, dict))
            elif isinstance(it, dict):
                out.append(it)
    return out


def _text(v: Any) -> str | None:
    """JSON-LD values are wildly polymorphic; flatten to something printable."""
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, dict):
        for k in ("name", "value", "@value", "text"):
            if k in v:
                return _text(v[k])
        if "address" in v:
            a = v["address"]
            if isinstance(a, dict):
                parts = [
                    a.get("addressLocality"),
                    a.get("addressRegion"),
                    a.get("addressCountry") if isinstance(a.get("addressCountry"), str) else None,
                ]
                s = ", ".join(p for p in parts if p)
                return s or None
        return json.dumps(v)[:300]
    if isinstance(v, list):
        vals = [_text(x) for x in v]
        return ", ".join(x for x in vals if x) or None
    return str(v)


JOB_ID_RE = re.compile(r"/jobs/(\d+)-")
EQUITY_RE = re.compile(r"(\d+(?:\.\d+)?\s*%\s*[–—-]\s*\d+(?:\.\d+)?\s*%)")
SALARY_RE = re.compile(r"(\$\s?\d[\d,]*k?\s*[–—-]\s*\$?\s?\d[\d,]*k?)")


def parse_detail_html(html: str) -> dict[str, str | None]:
    """Recover the fields JSON-LD omits.

    equity is the one field of the 12 that appears NOWHERE in structured data
    or in the search-page Apollo cache -- it exists only as rendered text on
    the job detail page ("$25k - $50k * 0.0% - 1.0%"). Salary is captured here
    too because JSON-LD baseSalary is frequently absent even when the page
    displays a band.
    """
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
    eq = EQUITY_RE.search(text)
    sal = SALARY_RE.search(text)
    return {
        "equity_raw": eq.group(1).strip() if eq else None,
        "salary_raw": sal.group(1).strip() if sal else None,
    }


def job_from_ld(ld: dict, url: str) -> Job:
    sal = ld.get("baseSalary")
    salary_raw = None
    if isinstance(sal, dict):
        val = sal.get("value")
        cur = sal.get("currency") or ""
        if isinstance(val, dict):
            lo, hi = val.get("minValue"), val.get("maxValue")
            unit = val.get("unitText") or ""
            if lo or hi:
                salary_raw = f"{cur} {lo}-{hi} {unit}".strip()
            elif val.get("value"):
                salary_raw = f"{cur} {val.get('value')} {unit}".strip()
        else:
            salary_raw = _text(sal)
    elif sal is not None:
        salary_raw = _text(sal)

    loc_type = ld.get("jobLocationType")
    remote = True if (isinstance(loc_type, str) and "telecommute" in loc_type.lower()) else None

    # JSON-LD `identifier` on Wellfound is a PropertyValue whose name is the
    # COMPANY, not the job id -- taking it verbatim silently yields a wrong id.
    # The URL is the reliable source.
    m = JOB_ID_RE.search(url)

    return Job(
        source="p2_jsonld",
        source_job_id=m.group(1) if m else None,
        company=_text(ld.get("hiringOrganization")),
        title=_text(ld.get("title")),
        location_raw=_text(ld.get("jobLocation")),
        remote=remote,
        description=_text(ld.get("description")),
        salary_raw=salary_raw,
        equity_raw=None,  # JSON-LD JobPosting has no equity concept
        posted_at=_text(ld.get("datePosted")),
        apply_url=_text(ld.get("url")) or url,
    )


def probe_sitemaps(max_children: int = 3) -> dict[str, Any]:
    """Walk robots-advertised sitemaps. Handles .gz. Reports URL families."""
    out: dict[str, Any] = {"robots_sitemaps": [], "fetched": []}
    r = fetch(f"{BASE}/robots.txt")
    out["robots_status"] = r.status
    sitemaps = re.findall(r"(?im)^\s*Sitemap:\s*(\S+)", r.text)
    out["robots_sitemaps"] = sitemaps
    # The brief guessed /sitemap.xml; check it too so the report can say what
    # the plain path actually does.
    candidates = list(dict.fromkeys(sitemaps + [f"{BASE}/sitemap.xml"]))

    for sm in candidates[: max_children + 1]:
        assert_allowed(sm)
        resp = fetch(sm)
        entry: dict[str, Any] = {"url": sm, "status": resp.status, "bytes": len(resp.text)}
        body = resp.text
        if resp.ok and sm.endswith(".gz"):
            # httpx gives us .text of gzipped bytes; re-fetch raw via content
            try:
                raw = resp.text.encode("utf-8", errors="replace")
                body = gzip.GzipFile(fileobj=io.BytesIO(raw)).read().decode("utf-8", "replace")
                entry["gunzipped"] = True
            except (OSError, EOFError) as e:
                entry["gunzip_error"] = f"{type(e).__name__}: {e}"
                entry["note"] = (
                    "cached text is a lossy decode of gzip bytes; see raw_gz probe below"
                )
        if resp.ok:
            locs = re.findall(r"<loc>([^<]+)</loc>", body)
            entry["n_locs"] = len(locs)
            entry["sample"] = locs[:8]
            entry["is_index"] = "<sitemapindex" in body
            fams: dict[str, int] = {}
            for L in locs:
                m = re.match(r"https?://[^/]+/([^/?]+)", L)
                fams[m.group(1) if m else "/"] = fams.get(m.group(1) if m else "/", 0) + 1
            entry["url_families"] = dict(sorted(fams.items(), key=lambda x: -x[1])[:12])
        out["fetched"].append(entry)
    return out


def probe_gz_sitemap_binary(url: str) -> dict[str, Any]:
    """Sitemaps are gzip BYTES; the text cache mangles them. Fetch binary once,
    uncached, purely to characterize the sitemap surface."""
    import httpx

    from ..cache import USER_AGENT

    try:
        with httpx.Client(follow_redirects=True, timeout=60.0) as c:
            r = c.get(url, headers={"User-Agent": USER_AGENT})
        info: dict[str, Any] = {"url": url, "status": r.status_code, "bytes": len(r.content)}
        if r.status_code != 200:
            return info
        data = r.content
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
            info["gunzipped_bytes"] = len(data)
        body = data.decode("utf-8", "replace")
        locs = re.findall(r"<loc>([^<]+)</loc>", body)
        info["is_index"] = "<sitemapindex" in body
        info["n_locs"] = len(locs)
        info["sample"] = locs[:10]
        fams: dict[str, int] = {}
        for L in locs:
            m = re.match(r"https?://[^/]+/([^/?]+)", L)
            k = m.group(1) if m else "/"
            fams[k] = fams.get(k, 0) + 1
        info["url_families"] = dict(sorted(fams.items(), key=lambda x: -x[1])[:15])
        return info
    except httpx.HTTPError as e:
        return {"url": url, "error": f"{type(e).__name__}: {e}"}


def run(
    role: str,
    location: str,
    job_urls: list[str] | None = None,
    sample: int = 5,
    refresh: bool = False,
) -> dict[str, Any]:
    print(f"\n=== P2 structured data (JSON-LD + sitemaps) ===")

    # 1. Does the SEARCH page carry JSON-LD?
    search_url = f"{BASE}/role/l/{role}/{location}"
    assert_allowed(search_url)
    sr = fetch(search_url, refresh=refresh)
    search_ld = extract_jsonld(sr.text) if sr.ok else []
    search_types = [d.get("@type") for d in search_ld]
    print(f"  search page {sr.status}: {len(search_ld)} JSON-LD blocks types={search_types}")

    # 2. Do JOB DETAIL pages carry JobPosting JSON-LD?
    if job_urls is None:
        try:
            p1 = json.load(open("results/p1_static.json", encoding="utf-8"))
            job_urls = [j["apply_url"] for j in p1["jobs"] if j.get("apply_url")][:sample]
        except FileNotFoundError as e:
            raise RuntimeError(
                "P2 needs job URLs; run p1 first or pass job_urls explicitly"
            ) from e

    detail: list[dict[str, Any]] = []
    jobs: list[Job] = []
    for u in job_urls[:sample]:
        assert_allowed(u)
        r = fetch(u, refresh=refresh)
        lds = extract_jsonld(r.text) if r.ok else []
        postings = [d for d in lds if str(d.get("@type", "")).endswith("JobPosting")]
        rec = {
            "url": u,
            "status": r.status,
            "bytes": len(r.text),
            "has_next_data": "__NEXT_DATA__" in r.text,
            "n_ld_blocks": len(lds),
            "ld_types": [d.get("@type") for d in lds],
            "n_jobpostings": len(postings),
        }
        if postings:
            rec["jobposting_keys"] = sorted(postings[0].keys())
            job = job_from_ld(postings[0], u)
            # Fill the JSON-LD gaps (equity, and salary when baseSalary is absent)
            # from the rendered detail HTML.
            extra = parse_detail_html(r.text)
            rec["html_recovered"] = {k: v for k, v in extra.items() if v}
            job.equity_raw = extra["equity_raw"]
            if not job.salary_raw:
                job.salary_raw = extra["salary_raw"]
            jobs.append(job)
        detail.append(rec)
        print(
            f"  detail {r.status} ld_blocks={len(lds)} jobposting={len(postings)} "
            f"types={rec['ld_types']}"
        )

    # 3. Sitemap surface
    sm = probe_sitemaps()
    gz = [
        probe_gz_sitemap_binary(u)
        for u in sm["robots_sitemaps"]
        if u.endswith(".gz")
    ]
    for g in gz:
        print(
            f"  sitemap {g.get('url')} -> status={g.get('status')} "
            f"index={g.get('is_index')} locs={g.get('n_locs')} "
            f"families={list((g.get('url_families') or {}).items())[:5]}"
        )

    result = {
        "probe": "p2_structured",
        "verdict": verdict(search_ld, jobs, detail),
        "search_page": {
            "url": search_url,
            "status": sr.status,
            "n_ld_blocks": len(search_ld),
            "ld_types": search_types,
            "has_jobposting": any(
                str(d.get("@type", "")).endswith("JobPosting") for d in search_ld
            ),
        },
        "detail_pages": detail,
        "sitemaps": sm,
        "sitemaps_binary": gz,
        "n_jobs": len(jobs),
        "coverage": coverage(jobs),
        "jobs": [j.to_dict() for j in jobs],
    }
    write_results("p2_structured.json", result)
    return result


def verdict(search_ld: list[dict], jobs: list[Job], detail: list[dict]) -> str:
    has_detail_ld = any(d.get("n_jobpostings", 0) > 0 for d in detail)
    search_has = any(str(d.get("@type", "")).endswith("JobPosting") for d in search_ld)
    if has_detail_ld and not search_has:
        return (
            "PARTIAL -- JobPosting JSON-LD exists on JOB DETAIL pages only; "
            "search/listing pages carry no JobPosting, so JSON-LD cannot enumerate "
            "results (1 extra request per job)"
        )
    if has_detail_ld and search_has:
        return "WORKS -- JSON-LD on both listing and detail pages"
    return "FAILED -- no JobPosting JSON-LD found"

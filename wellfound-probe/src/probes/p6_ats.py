"""P6 -- ATS fallback.

Take the company names P1 recovered, resolve each to its public ATS board, and
pull full job data from keyless JSON endpoints. The number that matters is the
HIT RATE: of N companies seen on Wellfound, how many resolve to a live board?

Method, and why it is built this way:

  * Slug candidates are generated from both the Wellfound company slug and the
    display name, with common corporate suffixes stripped. Ordered most- to
    least-likely so the first hit is usually the right one.

  * Every candidate is VERIFIED, not just status-checked. A 200 from a slug
    guess can easily be a DIFFERENT company that happens to own that slug
    (Greenhouse "ai", Lever "labs"). We compare the board's self-reported name
    against the Wellfound name and record a confidence. Counting unverified
    200s would inflate the hit rate, which is the one number that must be right.

  * Providers are tried in an order biased by Wellfound's own atsSource hint
    when present -- free first-party evidence of which ATS a company uses.

  * Boards with zero live postings are reported separately from boards that do
    not exist. "Resolved but empty" is a different architectural fact from
    "unresolvable".
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Callable, Iterable

from ..cache import fetch, write_results
from ..models import Job, coverage

# Public, keyless, documented job-board endpoints. No auth, no anti-bot.
PROVIDERS: dict[str, str] = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
}

# ATS APIs are machine-facing and built for polling; still serial and cached.
ATS_MIN_DELAY = 0.4
ATS_MAX_DELAY = 0.9

# Escalating backoff for HTTP 429. Workable rate-limits noticeably harder than
# Greenhouse/Lever/Ashby, which all tolerated sub-second polling without complaint.
RETRY_DELAYS = [4.0, 10.0, 20.0]

SUFFIXES = [
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "gmbh", "bv",
    "pvt", "private", "plc", "sa", "ag", "srl", "oy", "ab", "as", "technologies",
    "technology", "tech", "labs", "lab", "software", "solutions", "systems",
    "group", "holdings", "ventures", "studio", "studios", "digital", "global",
    "the", "io", "ai", "app", "hq",
]


def slugify(name: str, sep: str = "-") -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = re.sub(r"[^\w\s-]", " ", n.lower())
    return sep.join(t for t in re.split(r"[\s_-]+", n) if t)


def slug_candidates(name: str, wellfound_slug: str | None = None) -> list[str]:
    """Ordered, de-duplicated slug guesses. Wellfound's own slug goes first --
    it is a real, curated identifier rather than a transformation of a display
    name, so it hits far more often than anything we can synthesize."""
    out: list[str] = []

    def add(s: str | None) -> None:
        if s and s not in out and len(s) > 1:
            out.append(s)

    add(wellfound_slug)
    hyphen = slugify(name, "-")
    tight = slugify(name, "")
    add(hyphen)
    add(tight)

    toks = [t for t in hyphen.split("-") if t]
    stripped = [t for t in toks if t not in SUFFIXES]
    if stripped and stripped != toks:
        add("-".join(stripped))
        add("".join(stripped))
    if stripped:
        add(stripped[0])
    return out[:6]


# --- provider response parsers ------------------------------------------------
# Each returns (board_name_or_None, list_of_raw_job_dicts) or raises ValueError.


def _p_greenhouse(d: Any) -> tuple[str | None, list[dict]]:
    if not isinstance(d, dict) or "jobs" not in d:
        raise ValueError("no 'jobs' key")
    return None, [j for j in d["jobs"] if isinstance(j, dict)]


def _p_lever(d: Any) -> tuple[str | None, list[dict]]:
    if not isinstance(d, list):
        raise ValueError("expected list")
    return None, [j for j in d if isinstance(j, dict)]


def _p_ashby(d: Any) -> tuple[str | None, list[dict]]:
    if not isinstance(d, dict) or "jobs" not in d:
        raise ValueError("no 'jobs' key")
    return d.get("organizationName"), [j for j in d["jobs"] if isinstance(j, dict)]


def _p_workable(d: Any) -> tuple[str | None, list[dict]]:
    if not isinstance(d, dict):
        raise ValueError("expected object")
    if "jobs" not in d:
        raise ValueError("no 'jobs' key")
    return (d.get("name") or d.get("description")), [
        j for j in d["jobs"] if isinstance(j, dict)
    ]


PARSERS: dict[str, Callable[[Any], tuple[str | None, list[dict]]]] = {
    "greenhouse": _p_greenhouse,
    "lever": _p_lever,
    "ashby": _p_ashby,
    "workable": _p_workable,
}


# --- job normalization --------------------------------------------------------


def _strip_html(s: str | None) -> str | None:
    if not s:
        return None
    import html as _html

    return _html.unescape(re.sub(r"<[^>]+>", " ", s)).strip() or None


def normalize(provider: str, slug: str, raw: dict, company: str) -> Job:
    if provider == "greenhouse":
        loc = (raw.get("location") or {}).get("name")
        return Job(
            source="p6_ats:greenhouse", source_job_id=str(raw.get("id")), company=company,
            title=raw.get("title"), location_raw=loc,
            remote=bool(loc and "remote" in loc.lower()),
            description=_strip_html(raw.get("content")),
            salary_raw=None, equity_raw=None,
            posted_at=raw.get("updated_at") or raw.get("first_published"),
            apply_url=raw.get("absolute_url"),
        )
    if provider == "lever":
        cats = raw.get("categories") or {}
        loc = cats.get("location")
        return Job(
            source="p6_ats:lever", source_job_id=str(raw.get("id")), company=company,
            title=raw.get("text"), location_raw=loc,
            remote=bool(loc and "remote" in str(loc).lower()),
            description=_strip_html(raw.get("descriptionPlain") or raw.get("description")),
            salary_raw=(raw.get("salaryRange") or {}).get("currency") and json.dumps(raw["salaryRange"]) or None,
            equity_raw=None,
            posted_at=raw.get("createdAt"), apply_url=raw.get("hostedUrl"),
        )
    if provider == "ashby":
        return Job(
            source="p6_ats:ashby", source_job_id=str(raw.get("id")), company=company,
            title=raw.get("title"), location_raw=raw.get("location"),
            remote=raw.get("isRemote"),
            description=_strip_html(raw.get("descriptionHtml") or raw.get("descriptionPlain")),
            salary_raw=json.dumps(raw["compensation"]) if raw.get("compensation") else None,
            equity_raw=None,
            posted_at=raw.get("publishedAt") or raw.get("updatedAt"),
            apply_url=raw.get("jobUrl") or raw.get("applyUrl"),
        )
    if provider == "workable":
        return Job(
            source="p6_ats:workable", source_job_id=str(raw.get("shortcode") or raw.get("id")),
            company=company, title=raw.get("title"),
            location_raw=", ".join(
                str(x) for x in [raw.get("city"), raw.get("country")] if x
            ) or None,
            remote=raw.get("telecommuting"),
            description=_strip_html(raw.get("description")),
            salary_raw=None, equity_raw=None,
            posted_at=raw.get("published_on") or raw.get("created_at"),
            apply_url=raw.get("url") or raw.get("application_url"),
        )
    raise ValueError(f"unknown provider {provider}")


# --- verification -------------------------------------------------------------


def name_match(a: str, b: str | None) -> float:
    """Cheap similarity on normalized names. Used to reject slug collisions.

    Substring containment is scored by LENGTH RATIO, not a flat 0.9. A bare
    first-token slug guess makes short generic boards look like matches --
    "Smart Audit" vs a Workable account literally named "smart", or
    "Riddhi Siddhi Career Point" vs "Riddhi". Those are almost certainly
    different organizations, and scoring them 0.9 inflated the hit rate.
    """
    if not b:
        return 0.0
    x, y = slugify(a, ""), slugify(b, "")
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if x in y or y in x:
        # e.g. "moshimoshi" in "moshimoshimedia" -> 10/15 = 0.67 (believable)
        #      "smart" in "smartaudit"           ->  5/10 = 0.50 (rejected)
        return round(min(len(x), len(y)) / max(len(x), len(y)), 2)
    from difflib import SequenceMatcher

    return round(SequenceMatcher(None, x, y).ratio(), 2)


def verify(company: str, board_name: str | None, jobs: list[dict], provider: str, slug: str) -> dict:
    """Decide whether a 200 really is THIS company's board."""
    score = name_match(company, board_name) if board_name else 0.0
    reason = f"board_name={board_name!r} score={score}"
    if board_name and score >= 0.85:
        return {"confidence": "high", "why": f"board name matches ({reason})"}
    if board_name and score >= 0.6:
        return {"confidence": "medium", "why": f"board name partially matches ({reason})"}
    if board_name:
        return {"confidence": "rejected", "why": f"board name MISMATCH ({reason})"}
    # Providers that expose no org name (Greenhouse, Lever): fall back to the
    # apply URL, which embeds the real board slug, plus exact-slug agreement.
    urls = " ".join(str(j.get("absolute_url") or j.get("hostedUrl") or "") for j in jobs[:5])
    exact = slug == slugify(company, "-") or slug == slugify(company, "")
    if exact:
        return {"confidence": "high", "why": f"exact slug match on {provider} ({reason})"}
    if slug and slug in urls:
        return {"confidence": "medium", "why": f"slug present in apply URLs ({reason})"}
    return {"confidence": "low", "why": f"200 but unverified ({reason})"}


# --- resolver -----------------------------------------------------------------

_resolve_cache: dict[str, dict] = {}


def resolve_ats(
    company_name: str,
    wellfound_slug: str | None = None,
    ats_hints: Iterable[str] = (),
    refresh: bool = False,
    verbose: bool = True,
) -> dict[str, Any]:
    """Try slug candidates against each provider until a verified board is found.

    Returns a record with the outcome, every attempt made, and the jobs found.
    Never raises on a miss -- a miss is data.
    """
    ck = f"{company_name}|{wellfound_slug}"
    if ck in _resolve_cache and not refresh:
        return _resolve_cache[ck]

    hints = [h.lower() for h in ats_hints]
    order = [p for p in PROVIDERS if p in hints] + [p for p in PROVIDERS if p not in hints]

    cands = slug_candidates(company_name, wellfound_slug)
    attempts: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None

    for provider in order:
        for slug in cands:
            url = PROVIDERS[provider].format(slug=slug)
            r = fetch(url, refresh=refresh, verbose=False,
                      min_delay=ATS_MIN_DELAY, max_delay=ATS_MAX_DELAY,
                      headers={"Accept": "application/json"})

            # Workable rate-limits harder than the others. A 429 is NOT a miss --
            # counting it as one silently deflates the hit rate, which is the
            # single number this probe exists to produce. Back off and retry.
            attempt_n = 0
            while r.status == 429 and attempt_n < len(RETRY_DELAYS):
                d = RETRY_DELAYS[attempt_n]
                r = fetch(url, refresh=True, verbose=False,
                          min_delay=d, max_delay=d + 2.0,
                          headers={"Accept": "application/json"})
                attempt_n += 1

            rec: dict[str, Any] = {"provider": provider, "slug": slug, "status": r.status}
            if attempt_n:
                rec["retries_after_429"] = attempt_n
            if r.error:
                rec["error"] = r.error
                attempts.append(rec)
                continue
            if not r.ok:
                attempts.append(rec)
                continue
            try:
                data = json.loads(r.text)
            except json.JSONDecodeError as e:
                rec["parse_error"] = f"{e}"        # loud: a 200 that is not JSON
                attempts.append(rec)
                continue
            try:
                board_name, raw_jobs = PARSERS[provider](data)
            except ValueError as e:
                rec["shape_error"] = str(e)
                attempts.append(rec)
                continue

            v = verify(company_name, board_name, raw_jobs, provider, slug)
            rec.update({"n_jobs": len(raw_jobs), "board_name": board_name, **v})
            attempts.append(rec)

            if v["confidence"] == "rejected":
                continue
            cand = {
                "provider": provider, "slug": slug, "url": url,
                "board_name": board_name, "n_jobs": len(raw_jobs),
                "confidence": v["confidence"], "why": v["why"],
                "raw_jobs": raw_jobs,
            }
            # Rank by (confidence, has-live-jobs). A company can own a real but
            # EMPTY board on one provider while its actual openings live on
            # another (observed: Ethos has an empty Greenhouse board and a
            # populated Workable one). Stopping at the first high-confidence
            # match would silently lose those jobs, so we only stop early on a
            # high-confidence board that actually has postings.
            rank = {"high": 3, "medium": 2, "low": 1}
            key = (rank[cand["confidence"]], 1 if cand["n_jobs"] > 0 else 0)
            best_key = (
                (rank[best["confidence"]], 1 if best["n_jobs"] > 0 else 0)
                if best
                else (0, 0)
            )
            if key > best_key:
                best = cand
            if v["confidence"] == "high" and len(raw_jobs) > 0:
                break
        if best and best["confidence"] == "high" and best["n_jobs"] > 0:
            break

    out = {
        "company": company_name,
        "wellfound_slug": wellfound_slug,
        "ats_hints": list(ats_hints),
        "candidates_tried": cands,
        "n_attempts": len(attempts),
        "attempts": attempts,
        "resolved": best is not None,
        "match": {k: v for k, v in best.items() if k != "raw_jobs"} if best else None,
        "_best": best,
    }
    _resolve_cache[ck] = out
    if verbose:
        if best:
            print(
                f"  HIT  {company_name[:30]:30} -> {best['provider']}/{best['slug']} "
                f"({best['n_jobs']} jobs, {best['confidence']})"
            )
        else:
            print(f"  miss {company_name[:30]:30} ({len(attempts)} attempts)")
    return out


def run(role: str, location: str, limit: int | None = None, refresh: bool = False, **_: Any) -> dict[str, Any]:
    print("\n=== P6 ATS fallback ===")
    try:
        p1 = json.load(open("results/p1_static.json", encoding="utf-8"))
    except FileNotFoundError as e:
        raise RuntimeError("P6 needs company names from P1; run --probe p1 first") from e

    companies = p1["companies"]
    if limit:
        companies = companies[:limit]
    print(f"  resolving {len(companies)} companies seen on Wellfound...")

    records, jobs = [], []
    for c in companies:
        rec = resolve_ats(c["name"], c.get("slug"), c.get("ats_hints") or [], refresh=refresh)
        best = rec.pop("_best", None)
        if best:
            for raw in best["raw_jobs"]:
                jobs.append(normalize(best["provider"], best["slug"], raw, c["name"]))
        records.append(rec)

    resolved = [r for r in records if r["resolved"]]
    high = [r for r in resolved if r["match"]["confidence"] == "high"]
    med = [r for r in resolved if r["match"]["confidence"] == "medium"]
    low = [r for r in resolved if r["match"]["confidence"] == "low"]
    nonempty = [r for r in resolved if r["match"]["n_jobs"] > 0]

    by_provider: dict[str, int] = {}
    for r in resolved:
        p = r["match"]["provider"]
        by_provider[p] = by_provider.get(p, 0) + 1

    # Validate slug guessing against Wellfound's own atsSource hint.
    hinted = [r for r in records if r["ats_hints"]]
    hinted_ok = [
        r for r in hinted
        if r["resolved"] and r["match"]["provider"] in [h.lower() for h in r["ats_hints"]]
    ]

    n = len(records)
    stats = {
        "n_companies": n,
        "n_resolved_any": len(resolved),
        "n_resolved_high_conf": len(high),
        "n_resolved_medium_conf": len(med),
        "n_resolved_low_conf": len(low),
        "n_resolved_with_live_jobs": len(nonempty),
        "hit_rate_any_pct": round(100.0 * len(resolved) / n, 1) if n else 0.0,
        "hit_rate_high_conf_pct": round(100.0 * len(high) / n, 1) if n else 0.0,
        "hit_rate_with_live_jobs_pct": round(100.0 * len(nonempty) / n, 1) if n else 0.0,
        "by_provider": by_provider,
        "total_ats_jobs_recovered": len(jobs),
        "n_with_wellfound_ats_hint": len(hinted),
        "n_hint_confirmed_by_resolver": len(hinted_ok),
        "total_http_attempts": sum(r["n_attempts"] for r in records),
    }

    print(
        f"\n  HIT RATE: {stats['n_resolved_any']}/{n} = {stats['hit_rate_any_pct']}% (any) | "
        f"{stats['n_resolved_high_conf']}/{n} = {stats['hit_rate_high_conf_pct']}% (high conf) | "
        f"{stats['n_resolved_with_live_jobs']}/{n} = {stats['hit_rate_with_live_jobs_pct']}% (live jobs)"
    )
    print(f"  by provider: {by_provider} | ATS jobs recovered: {len(jobs)}")

    result = {
        "probe": "p6_ats",
        "verdict": (
            f"HIT RATE {stats['hit_rate_high_conf_pct']}% high-confidence "
            f"({stats['n_resolved_high_conf']}/{n}); "
            f"{stats['hit_rate_with_live_jobs_pct']}% with live postings"
        ),
        "stats": stats,
        "records": records,
        "n_jobs": len(jobs),
        "coverage": coverage(jobs),
        "jobs": [j.to_dict() for j in jobs[:200]],
    }
    write_results("p6_ats.json", result)
    return result

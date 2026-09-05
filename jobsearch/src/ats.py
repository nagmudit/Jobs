"""Resolve a company to its ATS board token, and cache the answer.

ATS APIs are company-scoped with no cross-board search, so before anything can
be fetched the board token has to be discovered. A miss costs several requests,
so both hits and misses are cached in `company_ats`.

## What the hint buys

Wellfound's own `atsSource` field names the provider a company uses
(`AtsIntegration::Greenhouse::Listing`). That removes the provider guessing that
dominated the Phase 1 probe, where 345 requests resolved 38 companies at 31.6%.
Knowing the provider, only the token has to be found.

Measured 2026-09-04 on 20 Greenhouse-hinted companies, using the Wellfound
company slug verbatim: **9/20 = 45%**. The misses are mostly mechanical and are
what the candidate list below is for:

    alloy-2, assemblyai-1, 10a-labs-1   Wellfound's own disambiguating suffix
    afresh-technologies                 company is just "Afresh"
    arize-ai                            "Arize AI" -> arizeai
    aavaz  (company "Enterpret")        slug and name have diverged entirely
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from . import store as S

# Provider -> the module that knows how to talk to it. Only providers with an
# implemented source appear here; the rest are recorded but not attempted.
IMPLEMENTED = {"greenhouse", "ashby", "workable", "lever"}

SUFFIXES = {
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "gmbh", "bv",
    "pvt", "private", "plc", "technologies", "technology", "labs", "lab",
    "software", "solutions", "systems", "group", "holdings", "the", "ai", "io",
}

# Wellfound appends -1/-2 to disambiguate duplicate company names. The suffix is
# ours, not the company's, so it never appears in a board token.
DISAMBIGUATOR = re.compile(r"-\d+$")


def provider_of(ats_source: str | None) -> str | None:
    """'AtsIntegration::Greenhouse::Listing' -> 'greenhouse'."""
    if not ats_source:
        return None
    parts = str(ats_source).split("::")
    return parts[1].lower() if len(parts) > 1 else str(ats_source).lower()


def _slug(name: str, sep: str = "-") -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = re.sub(r"[^\w\s-]", " ", n.lower())
    return sep.join(t for t in re.split(r"[\s_-]+", n) if t)


def token_candidates(company_slug: str, company_name: str | None = None) -> list[str]:
    """Ordered board-token guesses, most likely first."""
    out: list[str] = []

    def add(v: str | None) -> None:
        if v and v not in out and len(v) > 1:
            out.append(v)

    add(company_slug)
    add(DISAMBIGUATOR.sub("", company_slug))          # alloy-2 -> alloy
    add(company_slug.replace("-", ""))

    if company_name:
        add(_slug(company_name, "-"))
        add(_slug(company_name, ""))
        toks = [t for t in _slug(company_name, "-").split("-") if t]
        kept = [t for t in toks if t not in SUFFIXES]
        if kept and kept != toks:
            add("-".join(kept))
            add("".join(kept))
    return out[:6]


def cached(conn, company_slug: str, provider: str) -> dict | None:
    r = conn.execute(
        "SELECT * FROM company_ats WHERE company_slug=? AND provider=?",
        (company_slug, provider)).fetchone()
    return dict(r) if r else None


def resolve(
    fetcher,
    conn,
    company_slug: str,
    company_name: str | None,
    provider: str,
    refresh: bool = False,
) -> dict[str, Any]:
    """Find the board token for one company. Cached, including misses.

    A cached miss is honoured: re-probing six candidates for a company that had
    no board last time is pure cost against a third party.
    """
    if not refresh:
        hit = cached(conn, company_slug, provider)
        if hit is not None:
            return hit

    from . import sources as SRC

    mod = SRC.registry().get(provider)
    if provider not in IMPLEMENTED or mod is None:
        rec = {"company_slug": company_slug, "provider": provider,
               "board_token": None, "resolved": 0, "n_jobs": None,
               "attempts": json.dumps([f"provider {provider!r} not implemented"])}
        _store(conn, rec)
        return rec

    attempts: list[dict] = []
    token = None
    n_jobs = None
    for cand in token_candidates(company_slug, company_name):
        resp = fetcher.get(mod.board_url(cand, content=False),
                           max_age=fetcher.listing_ttl)
        if not resp.ok:
            attempts.append({"token": cand, "status": resp.status})
            continue
        try:
            data = mod.parse(resp.text, mod.board_url(cand))
        except Exception as e:                      # shape change is a finding
            attempts.append({"token": cand, "error": f"{type(e).__name__}: {e}"})
            continue
        # We bind the FIRST candidate that answers 200, so a token collision
        # would silently attach another company's whole board to this one.
        # Providers that return the account name can rule that out; those that
        # don't (Greenhouse, Ashby) have no hook and are unaffected.
        verify = getattr(mod, "verify_account", None)
        if verify is not None and not verify(data, company_name):
            attempts.append({"token": cand, "status": 200,
                             "rejected": f"account {data.get('name')!r} "
                                         f"is not {company_name!r}"})
            continue
        n_jobs = len(data.get("jobs", []))
        attempts.append({"token": cand, "status": 200, "jobs": n_jobs})
        token = cand
        break

    rec = {"company_slug": company_slug, "provider": provider,
           "board_token": token, "resolved": int(token is not None),
           "n_jobs": n_jobs, "attempts": json.dumps(attempts)}
    _store(conn, rec)
    return rec


def _store(conn, rec: dict) -> None:
    conn.execute(
        "INSERT INTO company_ats (company_slug,provider,board_token,resolved,"
        "n_jobs,attempts,checked_at) VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(company_slug,provider) DO UPDATE SET "
        "board_token=excluded.board_token, resolved=excluded.resolved, "
        "n_jobs=excluded.n_jobs, attempts=excluded.attempts, "
        "checked_at=excluded.checked_at",
        (rec["company_slug"], rec["provider"], rec["board_token"],
         rec["resolved"], rec["n_jobs"], rec["attempts"], S.now()))
    conn.commit()


def companies_for_role(conn, role: str, provider: str, limit: int | None = None
                       ) -> list[dict[str, Any]]:
    """Companies already in the corpus that have a job found via `role` and
    declare `provider` as their ATS.

    This is what bounds the expansion: it never walks the whole company table,
    only the companies the role already touched.
    """
    q = """
        SELECT DISTINCT j.company_slug, j.company
        FROM jobs j JOIN job_provenance p ON p.source_job_id = j.source_job_id
        WHERE p.role_slug = ? AND lower(j.ats_source) LIKE ?
          AND j.company_slug IS NOT NULL AND j.company_slug <> ''
        ORDER BY j.company_slug
    """
    rows = conn.execute(q, (role, f"%{provider.lower()}%")).fetchall()
    out = [{"company_slug": r["company_slug"], "company": r["company"]} for r in rows]
    return out[:limit] if limit else out

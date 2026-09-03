"""Depth tier: job detail pages, on demand only.

Equity exists nowhere in the search payload -- not in the Apollo node, not in
any ATS. It lives only on the job detail page, in JSON-LD (~80% of postings) and
as a rendered string like "$25k - $50k * 0.0% - 1.0%".

One request per job, so this is NEVER run over the whole corpus: 10k jobs is
11+ hours. The UI filters down to a handful of rows and enriches exactly those.

Detail pages are a different render path from search pages -- server-rendered,
no __NEXT_DATA__ -- so the search parser does not apply here.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable

from bs4 import BeautifulSoup

from . import store as S
from .fetch import Fetcher

# "$25k – $50k • 0.0% – 1.0%" -- equity is the trailing percent range.
EQUITY_RE = re.compile(r"(\d+(?:\.\d+)?\s*%\s*[–—-]\s*\d+(?:\.\d+)?\s*%)")
SALARY_RE = re.compile(
    r"([$₹€£]\s?\d[\d,]*(?:\.\d+)?\s*[kKlLmM]?(?:\s*[–—-]\s*[$₹€£]?\s?\d[\d,]*(?:\.\d+)?\s*[kKlLmM]?)?)"
)


def extract_jsonld(html: str) -> list[dict]:
    out: list[dict] = []
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", type=re.compile(r"application/ld\+json", re.I)):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            # Loud: a malformed JSON-LD block is a real finding, not noise.
            out.append({"__parse_error__": str(e), "__raw_head__": raw[:200]})
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict) and "@graph" in it:
                out.extend(x for x in it["@graph"] if isinstance(x, dict))
            elif isinstance(it, dict):
                out.append(it)
    return out


def parse_detail(html: str) -> dict[str, Any]:
    """Everything the detail page adds over the search node."""
    lds = extract_jsonld(html)
    posting = next(
        (d for d in lds if str(d.get("@type", "")).endswith("JobPosting")), None
    )
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)

    eq = EQUITY_RE.search(text)
    sal = SALARY_RE.search(text)

    desc = None
    if posting and isinstance(posting.get("description"), str):
        # JSON-LD description is HTML; flatten it but keep the whole thing.
        desc = BeautifulSoup(posting["description"], "lxml").get_text("\n", strip=True)

    return {
        "json_ld": json.dumps(posting) if posting else None,
        "equity_raw": eq.group(1).strip() if eq else None,
        "salary_raw": sal.group(1).strip() if sal else None,
        "description_full": desc,
        "has_jobposting": posting is not None,
    }
    # NOTE: JSON-LD `identifier` on Wellfound is the COMPANY name, not a job id.
    # We never read it -- ids come from the URL / our own DB.


def enrich_ids(
    fetcher: Fetcher,
    conn,
    job_ids: Iterable[str],
    refresh: bool = False,
    on_progress: Callable[[dict], None] | None = None,
    max_age_days: int | None = None,
) -> dict[str, Any]:
    ids = [str(i) for i in job_ids]
    done, failed, skipped, stale = 0, 0, 0, 0
    halted: str | None = None

    for jid in ids:
        row = conn.execute(
            "SELECT source_job_id, apply_url, days_old FROM jobs WHERE source_job_id=?",
            (jid,)).fetchone()
        if row is None:
            skipped += 1
            continue
        # A stale job is never worth a rate-limited request: it is excluded from
        # the corpus view anyway.
        if max_age_days and row["days_old"] is not None and row["days_old"] > max_age_days:
            stale += 1
            continue
        if not refresh and conn.execute(
            "SELECT 1 FROM job_detail WHERE source_job_id=?", (jid,)
        ).fetchone():
            skipped += 1
            continue

        try:
            resp = fetcher.get(row["apply_url"], refresh=refresh)
        except Exception as e:
            # Mitigation or transport failure: stop, keep what we have.
            halted = f"{type(e).__name__}: {e}"
            break

        if not resp.ok:
            failed += 1
            continue

        d = parse_detail(resp.text)
        conn.execute(
            "INSERT INTO job_detail (source_job_id,json_ld,equity_raw,salary_raw,"
            "description_full,fetched_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(source_job_id) DO UPDATE SET json_ld=excluded.json_ld,"
            "equity_raw=excluded.equity_raw, salary_raw=excluded.salary_raw,"
            "description_full=excluded.description_full, fetched_at=excluded.fetched_at",
            (jid, d["json_ld"], d["equity_raw"], d["salary_raw"],
             d["description_full"], S.now()),
        )
        conn.commit()
        done += 1
        if on_progress:
            on_progress({"job_id": jid, "done": done, "total": len(ids)})

    return {
        "requested": len(ids), "enriched": done, "skipped": skipped,
        "stale_skipped": stale, "failed": failed, "halted": halted,
    }

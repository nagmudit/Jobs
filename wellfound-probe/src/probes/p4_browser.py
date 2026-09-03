"""P4 -- Real browser, real session (Playwright + persistent profile).

Scope note: P1 already recovers 11/12 fields with a plain HTTP GET, so this
probe exists to answer a narrower question -- does a real browser with YOUR
logged-in session recover anything P1 cannot, and does behaviour change under
repeated navigation?

Login is manual and one-time, by design. Run:

    python -m src.run --probe p4 --login

which opens a headed browser against the persistent profile at
`browser-profile/`. Log in by hand, close the window, and every later run
reuses that profile. We never automate credential entry and never touch a
challenge widget -- if a challenge appears, the probe records it and stops.

Constraints honoured: navigation is paced 3-5s, concurrency 1, and the
persistent profile means we behave like one returning human rather than a
fleet of fresh contexts.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from ..cache import ROOT, write_results
from ..models import Job, coverage
from ..robots import assert_allowed

PROFILE_DIR = ROOT / "browser-profile"
BASE = "https://wellfound.com"

CHALLENGE_SIGNS = [
    "cf-browser-verification", "cf_chl_opt", "Just a moment",
    "Verify you are human", "cf-turnstile", "Attention Required",
]


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa
    except ImportError as e:
        raise RuntimeError(
            "playwright not installed. pip install playwright && playwright install chromium"
        ) from e
    return sync_playwright


def login(timeout_s: int = 300) -> None:
    """Open a headed browser so the user can log in once, by hand."""
    sync_playwright = _require_playwright()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Opening headed browser with profile {PROFILE_DIR}")
    print("Log in to Wellfound manually, then close the browser window.")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, args=["--disable-blink-features=AutomationControlled"]
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(f"{BASE}/login", wait_until="domcontentloaded")
        deadline = time.time() + timeout_s
        while time.time() < deadline and ctx.pages:
            time.sleep(2)
        ctx.close()
    print("Profile saved.")


def detect_challenge(html: str) -> list[str]:
    return [s for s in CHALLENGE_SIGNS if re.search(re.escape(s), html, re.I)]


def run(
    role: str,
    location: str,
    pages: int = 2,
    headless: bool = True,
    refresh: bool = False,
    **_: Any,
) -> dict[str, Any]:
    print("\n=== P4 real browser (Playwright, persistent profile) ===")
    sync_playwright = _require_playwright()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    logged_in_profile = any(PROFILE_DIR.iterdir()) if PROFILE_DIR.exists() else False
    print(f"  profile={'present' if logged_in_profile else 'EMPTY (cold, logged-out)'} headless={headless}")

    navigations: list[dict[str, Any]] = []
    jobs: list[Job] = []
    err: str | None = None

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            raise RuntimeError(
                f"could not launch chromium: {type(e).__name__}: {e}. "
                "Run: playwright install chromium"
            ) from e

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            for i in range(1, pages + 1):
                q = f"?page={i}" if i > 1 else ""
                url = f"{BASE}/role/l/{role}/{location}{q}"
                assert_allowed(url)
                t0 = time.monotonic()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1500)
                html = page.content()
                nav = {
                    "n": i,
                    "url": url,
                    "status": resp.status if resp else None,
                    "bytes": len(html),
                    "ms": int((time.monotonic() - t0) * 1000),
                    "challenge": detect_challenge(html),
                    "title": page.title(),
                    "logged_in": bool(
                        page.locator("a[href*='/profile'], a[href*='/logout']").count()
                    ),
                }

                # Reuse the P1 parser: the browser sees the same __NEXT_DATA__.
                from .p1_static import extract_next_data, parse_apollo

                nd = extract_next_data(html)
                nav["has_next_data"] = nd is not None
                if nd is not None:
                    parsed = parse_apollo(nd, url)
                    nav.update(
                        {
                            "role_applied": parsed["role_applied"],
                            "page_returned": parsed["page_returned"],
                            "n_jobs": len(parsed["jobs"]),
                            "total_job_count": parsed["total_job_count"],
                        }
                    )
                    for j in parsed["jobs"]:
                        j.source = "p4_browser"
                    jobs.extend(parsed["jobs"])
                navigations.append(nav)
                print(
                    f"  nav{i}: status={nav['status']} bytes={nav['bytes']} "
                    f"challenge={nav['challenge'] or '-'} next_data={nav['has_next_data']} "
                    f"jobs={nav.get('n_jobs')} logged_in={nav['logged_in']}"
                )
                if nav["challenge"]:
                    print("  challenge detected -> stopping (no evasion attempted)")
                    break
                time.sleep(random.uniform(3.0, 5.0))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            print(f"  ERROR: {err}")
        finally:
            ctx.close()

    result = {
        "probe": "p4_browser",
        "verdict": verdict(navigations, jobs, err),
        "profile_present": logged_in_profile,
        "headless": headless,
        "error": err,
        "navigations": navigations,
        "n_jobs": len(jobs),
        "coverage": coverage(jobs),
        "jobs": [j.to_dict() for j in jobs[:50]],
    }
    write_results("p4_browser.json", result)
    return result


def verdict(navs: list[dict], jobs: list[Job], err: str | None) -> str:
    if err and not navs:
        return f"FAILED -- {err}"
    if any(n["challenge"] for n in navs):
        return "BLOCKED -- anti-bot challenge served; not pursued"
    if jobs:
        return (
            f"WORKS -- {len(navs)} navigations, {len(jobs)} jobs, no challenge; "
            "but recovers the same __NEXT_DATA__ P1 already gets over plain HTTP"
        )
    return "REACHABLE BUT NO DATA"

"""P3 -- RSS / Atom / JSON feeds.

Expected to be a dead end; the point is to confirm it cheaply and move on.
Two angles:
  (a) declared feeds -- <link rel="alternate"> in the head of pages we already
      have cached (free, no extra requests)
  (b) conventional feed paths -- guessed, and checked against robots first

A "feed" that returns 200 with text/html is not a feed. We assert on the
content type and on the body actually parsing as XML/JSON, so a soft-404 that
serves the SPA shell is reported as the miss it is.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

from ..cache import fetch, load, write_results
from ..models import Job, coverage
from ..robots import allowed

BASE = "https://wellfound.com"

CANDIDATE_PATHS = [
    "/jobs.rss",
    "/jobs.atom",
    "/jobs.json",
    "/feed",
    "/feed.xml",
    "/rss",
    "/rss.xml",
    "/atom.xml",
    "/jobs/feed",
    "/role/l/artificial-intelligence-engineer/bangalore.rss",
    "/location/bangalore.rss",
]

FEED_CT = re.compile(r"(rss|atom|xml|json)", re.I)


def declared_feeds(html: str, page: str) -> list[dict[str, Any]]:
    out = []
    soup = BeautifulSoup(html, "lxml")
    for link in soup.find_all("link", rel=True):
        rels = " ".join(link.get("rel") or []).lower()
        typ = (link.get("type") or "").lower()
        if "alternate" in rels and FEED_CT.search(typ):
            out.append({"page": page, "href": link.get("href"), "type": typ, "title": link.get("title")})
    return out


def looks_like_feed(text: str, content_type: str) -> tuple[bool, str]:
    ct_ok = bool(FEED_CT.search(content_type)) and "html" not in content_type.lower()
    head = text.lstrip()[:200].lower()
    if head.startswith("<?xml") or "<rss" in head or "<feed" in head:
        return True, "xml-feed-root"
    if head.startswith("{") or head.startswith("["):
        try:
            json.loads(text)
            return True, "json-parsed"
        except json.JSONDecodeError as e:
            return False, f"json-parse-failed: {e}"
    if ct_ok:
        return False, f"content-type {content_type!r} but body is not a feed root"
    return False, f"not a feed (content-type={content_type!r}, body starts {head[:60]!r})"


def run(role: str, location: str, refresh: bool = False, **_: Any) -> dict[str, Any]:
    print("\n=== P3 feeds (RSS/Atom/JSON) ===")

    # (a) Declared feeds from pages already in cache -- costs zero requests.
    declared: list[dict[str, Any]] = []
    for u in [
        f"{BASE}/role/l/{role}/{location}",
        f"{BASE}/jobs/4235671-ai-engineer",
    ]:
        c = load(u)
        if c and c.text:
            declared.extend(declared_feeds(c.text, u))
    print(f"  <link rel=alternate> feeds declared in cached pages: {len(declared)}")

    # (b) Conventional paths.
    probed: list[dict[str, Any]] = []
    for path in CANDIDATE_PATHS:
        url = BASE + path
        ok_robots, rule = allowed(url)
        if not ok_robots:
            probed.append({"url": url, "skipped": f"robots disallow {rule}"})
            print(f"  SKIP (robots {rule}): {path}")
            continue
        r = fetch(url, refresh=refresh, verbose=False)
        ct = r.headers.get("content-type", "")
        if r.error:
            is_feed, why = False, f"transport error: {r.error}"
        elif not r.ok:
            # Cloudflare serves its error pages as application/xml starting with
            # <?xml, which trips a naive sniff. A non-2xx is never a feed.
            is_feed, why = False, f"HTTP {r.status} (not a feed; error page)"
        else:
            is_feed, why = looks_like_feed(r.text, ct)
        rec = {
            "url": url,
            "status": r.status,
            "content_type": ct,
            "bytes": len(r.text),
            "is_feed": is_feed,
            "reason": why,
            "body_head": r.text[:120] if r.text else None,
        }
        probed.append(rec)
        print(f"  {r.status} {path:56} feed={is_feed} ct={ct.split(';')[0]!r}")

    hits = [p for p in probed if p.get("is_feed")]
    jobs: list[Job] = []  # nothing to parse if there are no feeds

    result = {
        "probe": "p3_feeds",
        "verdict": (
            f"WORKS -- {len(hits)} feed(s) found"
            if hits
            else "DEAD END -- no RSS/Atom/JSON feed declared or discoverable"
        ),
        "declared_feeds": declared,
        "probed_paths": probed,
        "n_feed_hits": len(hits),
        "n_jobs": 0,
        "coverage": coverage(jobs),
        "jobs": [],
    }
    write_results("p3_feeds.json", result)
    return result

"""URL-keyed response cache + polite HTTP fetcher.

Design rules from the brief:
  * every raw response lands in cache/ keyed by a hash of the URL
  * re-runs MUST hit the cache, not the network (--refresh to override)
  * 1 request per 3-5s, concurrency 1, honest UA (contact optional, off by default)
  * fail loudly: no bare excepts, errors are recorded and re-surfaced

Errors are cached too. A 403 from Cloudflare is *evidence*, and re-fetching it
on every iteration is both slow and rude.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache"

# Declares itself as a research probe and never spoofs a browser. It carries NO
# contact by default: nothing here should tie back to a person or an account
# (owner's decision, 2026-09-08). Set PROBE_CONTACT to add one back for a run.
#
# This module is frozen research and is not executed by `jobsearch/`; the string
# is edited here only so a public repo does not carry an identifier.
CONTACT = os.environ.get("PROBE_CONTACT", "").strip()
USER_AGENT = (
    "wellfound-feasibility-probe/0.1 (personal job-search research"
    + (f"; contact: {CONTACT}" if CONTACT else "")
    + ")"
)

MIN_DELAY = 3.0
MAX_DELAY = 5.0

_last_request_at: float = 0.0


@dataclass
class Response:
    url: str
    final_url: str
    status: int | None
    headers: dict[str, str]
    text: str
    elapsed_ms: int
    fetched_at: str
    from_cache: bool = False
    error: str | None = None          # transport-level failure, if any

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    def summary(self) -> str:
        if self.error:
            return f"{self.url} -> ERROR {self.error}"
        return (
            f"{self.url} -> {self.status} "
            f"({len(self.text)} bytes, {self.elapsed_ms}ms"
            f"{', cached' if self.from_cache else ''})"
        )


def _key(url: str, method: str = "GET") -> str:
    return hashlib.sha256(f"{method} {url}".encode("utf-8")).hexdigest()[:24]


def _paths(url: str, method: str = "GET") -> tuple[Path, Path]:
    k = _key(url, method)
    host = (urlparse(url).hostname or "unknown").replace(":", "_")
    d = CACHE_DIR / host
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{k}.meta.json", d / f"{k}.body"


def load(url: str, method: str = "GET") -> Response | None:
    meta_p, body_p = _paths(url, method)
    if not meta_p.exists():
        return None
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    body = body_p.read_text(encoding="utf-8", errors="replace") if body_p.exists() else ""
    return Response(from_cache=True, **{**meta, "text": body})


def store(resp: Response) -> None:
    meta_p, body_p = _paths(resp.url)
    d = asdict(resp)
    body = d.pop("text")
    d.pop("from_cache", None)
    meta_p.write_text(json.dumps(d, indent=2), encoding="utf-8")
    body_p.write_text(body, encoding="utf-8", errors="replace")


def _throttle(min_delay: float | None = None, max_delay: float | None = None) -> None:
    """Serial, human-paced. Sleeps out the remainder of a 3-5s window.

    The 3-5s floor is aimed at Wellfound -- a bot-protected consumer site we are
    a guest on. Public ATS job-board APIs (Greenhouse/Lever/Ashby/Workable) are
    documented, keyless, machine-facing endpoints intended for polling; probes
    against those pass a shorter delay. Still strictly serial, still cached.
    """
    global _last_request_at
    lo = MIN_DELAY if min_delay is None else min_delay
    hi = MAX_DELAY if max_delay is None else max_delay
    now = time.monotonic()
    wait = random.uniform(lo, hi) - (now - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def fetch(
    url: str,
    *,
    refresh: bool = False,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    verbose: bool = True,
    min_delay: float | None = None,
    max_delay: float | None = None,
) -> Response:
    """GET a URL through the cache. Never raises on HTTP status or transport
    failure -- returns a Response carrying the evidence instead."""
    if not refresh:
        cached = load(url)
        if cached is not None:
            if verbose:
                print(f"  [cache] {cached.summary()}")
            return cached

    h = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        h.update(headers)

    _throttle(min_delay, max_delay)
    t0 = time.monotonic()
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, http2=False) as c:
            r = c.get(url, headers=h)
        resp = Response(
            url=url,
            final_url=str(r.url),
            status=r.status_code,
            headers={k.lower(): v for k, v in r.headers.items()},
            text=r.text,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )
    except httpx.HTTPError as e:
        # Loud, typed, recorded. Transport failure is a finding, not a nuisance.
        resp = Response(
            url=url,
            final_url=url,
            status=None,
            headers={},
            text="",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            fetched_at=datetime.now(timezone.utc).isoformat(),
            error=f"{type(e).__name__}: {e}",
        )
    store(resp)
    if verbose:
        print(f"  [net]   {resp.summary()}")
    return resp


def results_path(name: str) -> Path:
    p = ROOT / "results"
    p.mkdir(parents=True, exist_ok=True)
    return p / name


def write_results(name: str, payload: Any) -> Path:
    p = results_path(name)
    p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"  -> wrote {p.relative_to(ROOT)}")
    return p

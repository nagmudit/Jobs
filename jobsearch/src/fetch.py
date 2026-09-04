"""HTTP layer: robots + rate limit + disk cache + Cloudflare logging.

Every request to Wellfound goes through here. There is no other way out to the
network, which is what makes the conduct rules enforceable rather than
aspirational:

  * robots.txt is parsed and honoured, and re-checked per path family
  * 3-5 s between requests, concurrency 1
  * honest User-Agent carrying a contact address
  * cf-ray / cf-mitigated recorded on EVERY response
  * responses cached to disk by URL hash; re-runs hit cache

## Cache freshness

Listing responses (search pages, API feeds, ATS boards) carry a `max_age`; job
detail pages do not. A job description does not change, so re-fetching it is
pure cost -- but a *listing* that never expires means the tool can never see a
job posted since the last run, which is what `fetch --roles X` exists to do.
See ADR-011.

One carve-out, and it is a conduct rule rather than a performance one: **a
cached mitigation never expires.** A 403/429/503 or a `cf-mitigated` header
stays sticky and keeps halting, however old it is. Letting the TTL clear it
would turn this into a backoff-and-continue path, which is precisely what is
forbidden. Clearing one is a deliberate human act: delete the cache entry.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import random
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import assertions as A

BASE = "https://wellfound.com"


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
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    @property
    def cf_ray(self) -> str | None:
        return self.headers.get("cf-ray")

    @property
    def cf_mitigated(self) -> str | None:
        return self.headers.get("cf-mitigated")


class RobotsDisallowed(RuntimeError):
    """A path family robots.txt forbids. Raised rather than silently skipped."""


# robots.txt is re-read daily. It was cached permanently, so a site could tighten
# its rules and we would never see it. Longer than the listing TTL because rules
# change rarely, short enough that we notice within a day -- and still readable
# offline in between.
ROBOTS_TTL = 24 * 3600.0


def _is_mitigation(status: int | None, headers: dict) -> bool:
    """Whether a cached response records Cloudflare acting on us.

    Kept in step with `assertions.check_mitigation`: these are the responses that
    must NEVER be aged out of the cache, because re-requesting them on a timer is
    a retry-through-mitigation by another name.
    """
    return bool((headers or {}).get("cf-mitigated")) or status in (403, 429, 503)


class Fetcher:
    def __init__(self, cache_dir: Path, user_agent: str,
                 delay_range: tuple[float, float] = (3.0, 5.0),
                 listing_ttl: float | None = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self.delay_range = delay_range
        # How stale a LISTING may be before it is re-fetched. Callers that fetch
        # listings pass `max_age=fetcher.listing_ttl` explicitly, so which
        # requests are freshness-sensitive stays greppable. None = never expire.
        self.listing_ttl = listing_ttl
        self._last_request = 0.0
        # Keyed by origin. A single shared rule set would apply one site's
        # robots.txt to another host -- potentially permitting a fetch that host
        # forbids. Every origin gets its own fetch and its own rules.
        self._robots: dict[str, list[tuple[bool, str]]] = {}
        # Filled by the caller (crawl/enrich) so every response lands in request_log.
        self.on_response = None

    # --- cache ---------------------------------------------------------------

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()[:24]
        host = (urlparse(url).hostname or "unknown").replace(":", "_")
        d = self.cache_dir / host
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{key}.meta.json", d / f"{key}.body"

    def _load(self, url: str, max_age: float | None = None) -> Response | None:
        """The cached response, or None when there is none or it has aged out.

        Age comes from the recorded `fetched_at` rather than the file mtime: it
        is the real fetch time and it travels with the cache directory.
        """
        meta_p, body_p = self._paths(url)
        if not meta_p.exists():
            return None
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        body = body_p.read_text(encoding="utf-8", errors="replace") if body_p.exists() else ""
        resp = Response(from_cache=True, **{**meta, "text": body})
        # A recorded mitigation is sticky regardless of age. See the module docstring.
        if max_age is not None and not _is_mitigation(resp.status, resp.headers):
            if self._age_of(resp) > max_age:
                return None
        return resp

    @staticmethod
    def _age_of(resp: Response) -> float:
        """Seconds since the response was fetched. An unparseable or missing
        timestamp counts as infinitely old -- a cache entry we cannot date is one
        we cannot vouch for, so it is re-fetched rather than trusted."""
        try:
            when = datetime.fromisoformat(resp.fetched_at)
        except (TypeError, ValueError):
            return float("inf")
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - when).total_seconds()

    def _store(self, resp: Response) -> None:
        meta_p, body_p = self._paths(resp.url)
        d = asdict(resp)
        body = d.pop("text")
        d.pop("from_cache", None)
        meta_p.write_text(json.dumps(d, indent=2), encoding="utf-8")
        body_p.write_text(body, encoding="utf-8", errors="replace")

    # --- robots --------------------------------------------------------------

    def _load_robots(self, origin: str) -> list[tuple[bool, str]]:
        """Rules for ONE origin, cached per origin.

        A failure here is never cached: a host whose robots.txt we could not read
        must keep raising, not silently become permissive on a later call. That
        is what stops us crawling a site blind -- naukri.com serves 403 on its
        own robots.txt, and this is the check that refuses it.
        """
        if origin in self._robots:
            return self._robots[origin]
        resp = self._raw_get(f"{origin}/robots.txt", max_age=ROBOTS_TTL)
        if not resp.ok:
            raise RuntimeError(
                f"{origin}: could not read robots.txt (HTTP {resp.status}); "
                f"refusing to crawl blind"
            )
        rules: list[tuple[bool, str]] = []
        applies = False
        for line in resp.text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                applies = v == "*"
            elif applies and k in ("disallow", "allow") and v:
                rules.append((k == "allow", v))
        self._robots[origin] = rules
        return rules

    def allowed(self, url: str) -> tuple[bool, str | None]:
        p = urlparse(url)
        rules = self._load_robots(f"{p.scheme}://{p.netloc}")
        path = p.path or "/"
        if p.query:
            path += "?" + p.query
        best: tuple[int, bool, str] | None = None
        for is_allow, pattern in rules:
            pat = pattern if "*" in pattern or pattern.endswith("$") else pattern + "*"
            if fnmatch.fnmatchcase(path, pat):
                score = len(pattern)
                if best is None or score > best[0] or (score == best[0] and is_allow):
                    best = (score, is_allow, pattern)
        if best is None:
            return True, None
        return best[1], best[2]

    def assert_allowed(self, url: str) -> None:
        ok, rule = self.allowed(url)
        if not ok:
            origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
            raise RobotsDisallowed(
                f"{origin}/robots.txt disallows {url} (Disallow: {rule})")

    # --- transport -----------------------------------------------------------

    def _throttle(self) -> None:
        lo, hi = self.delay_range
        wait = random.uniform(lo, hi) - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _raw_get(self, url: str, timeout: float = 30.0,
                 max_age: float | None = None) -> Response:
        """Uncached, un-robots-checked. Only robots.txt itself uses this directly."""
        cached = self._load(url, max_age)
        if cached is not None:
            return cached
        self._throttle()
        t0 = time.monotonic()
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            with httpx.Client(follow_redirects=True, timeout=timeout) as c:
                r = c.get(url, headers=headers)
            resp = Response(
                url=url, final_url=str(r.url), status=r.status_code,
                headers={k.lower(): v for k, v in r.headers.items()},
                text=r.text, elapsed_ms=int((time.monotonic() - t0) * 1000),
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )
        except httpx.HTTPError as e:
            resp = Response(
                url=url, final_url=url, status=None, headers={}, text="",
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                fetched_at=datetime.now(timezone.utc).isoformat(),
                error=f"{type(e).__name__}: {e}",
            )
        self._store(resp)
        return resp

    def get(self, url: str, refresh: bool = False,
            max_age: float | None = None) -> Response:
        """The only public way to reach the network. Robots-gated, logged,
        and halting on the first Cloudflare mitigation.

        `max_age` in seconds re-fetches a cache entry older than that. Listing
        callers pass `max_age=self.listing_ttl`; detail callers pass nothing.
        A cached mitigation ignores it and keeps halting -- see `_is_mitigation`.
        """
        self.assert_allowed(url)
        if refresh:
            meta_p, body_p = self._paths(url)
            meta_p.unlink(missing_ok=True)
            body_p.unlink(missing_ok=True)
        resp = self._raw_get(url, max_age=max_age)

        if self.on_response is not None:
            self.on_response(resp)

        if resp.error:
            raise RuntimeError(f"{url}: transport failure: {resp.error}")
        # Cached non-2xx responses are replayed as evidence, not re-requested.
        A.check_mitigation(resp.status, resp.headers, url, resp.text)
        return resp


# --- URL builders -------------------------------------------------------------


# Location tokens that are not places. Both map to their own page type with
# their own GraphQL arg shape, verified against the live site 2026-09-04.
ANYWHERE = "anywhere"
REMOTE = "remote"


def search_url(role: str, location: str, page: int = 1) -> str:
    """Wellfound's three SEO landing shapes.

    | location    | URL                        | page type            | args              |
    |-------------|----------------------------|----------------------|-------------------|
    | "anywhere"  | /role/{role}               | roleSearch           | role              |
    | "remote"    | /role/r/{role}             | roleRemoteSearch     | role, remote:true |
    | <a place>   | /role/l/{role}/{location}  | roleLocationSearch   | role, location    |

    "anywhere" is the widest: no location filter at all. It is what the UI
    crawls by default, because location filtering happens locally against the
    real `locationNames` values rather than by asking Wellfound to pre-filter.
    """
    q = f"?page={page}" if page > 1 else ""
    if location == ANYWHERE:
        return f"{BASE}/role/{role}{q}"
    if location == REMOTE:
        return f"{BASE}/role/r/{role}{q}"
    return f"{BASE}/role/l/{role}/{location}{q}"


def job_url(job_id: str, slug: str) -> str:
    return f"{BASE}/jobs/{job_id}-{slug}"

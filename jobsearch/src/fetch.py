"""HTTP layer: robots + rate limit + disk cache + Cloudflare logging.

Every request to Wellfound goes through here. There is no other way out to the
network, which is what makes the conduct rules enforceable rather than
aspirational:

  * robots.txt is parsed and honoured, and re-checked per path family
  * 3-5 s between requests, concurrency 1
  * honest User-Agent carrying a contact address
  * cf-ray / cf-mitigated recorded on EVERY response
  * responses cached to disk by URL hash; re-runs hit cache
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


class Fetcher:
    def __init__(self, cache_dir: Path, user_agent: str,
                 delay_range: tuple[float, float] = (3.0, 5.0)):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.user_agent = user_agent
        self.delay_range = delay_range
        self._last_request = 0.0
        self._robots: dict[str, list[tuple[bool, str]]] | None = None
        # Filled by the caller (crawl/enrich) so every response lands in request_log.
        self.on_response = None

    # --- cache ---------------------------------------------------------------

    def _paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()[:24]
        host = (urlparse(url).hostname or "unknown").replace(":", "_")
        d = self.cache_dir / host
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{key}.meta.json", d / f"{key}.body"

    def _load(self, url: str) -> Response | None:
        meta_p, body_p = self._paths(url)
        if not meta_p.exists():
            return None
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        body = body_p.read_text(encoding="utf-8", errors="replace") if body_p.exists() else ""
        return Response(from_cache=True, **{**meta, "text": body})

    def _store(self, resp: Response) -> None:
        meta_p, body_p = self._paths(resp.url)
        d = asdict(resp)
        body = d.pop("text")
        d.pop("from_cache", None)
        meta_p.write_text(json.dumps(d, indent=2), encoding="utf-8")
        body_p.write_text(body, encoding="utf-8", errors="replace")

    # --- robots --------------------------------------------------------------

    def _load_robots(self, origin: str) -> list[tuple[bool, str]]:
        if self._robots is not None:
            return self._robots
        resp = self._raw_get(f"{origin}/robots.txt")
        rules: list[tuple[bool, str]] = []
        if resp.ok:
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
        else:
            raise RuntimeError(
                f"could not read robots.txt (HTTP {resp.status}); refusing to crawl blind"
            )
        self._robots = rules
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
            raise RobotsDisallowed(f"robots.txt disallows {url} (Disallow: {rule})")

    # --- transport -----------------------------------------------------------

    def _throttle(self) -> None:
        lo, hi = self.delay_range
        wait = random.uniform(lo, hi) - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _raw_get(self, url: str, timeout: float = 30.0) -> Response:
        """Uncached, un-robots-checked. Only robots.txt itself uses this directly."""
        cached = self._load(url)
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

    def get(self, url: str, refresh: bool = False) -> Response:
        """The only public way to reach the network. Robots-gated, logged,
        and halting on the first Cloudflare mitigation."""
        self.assert_allowed(url)
        if refresh:
            meta_p, body_p = self._paths(url)
            meta_p.unlink(missing_ok=True)
            body_p.unlink(missing_ok=True)
        resp = self._raw_get(url)

        if self.on_response is not None:
            self.on_response(resp)

        if resp.error:
            raise RuntimeError(f"{url}: transport failure: {resp.error}")
        # Cached non-2xx responses are replayed as evidence, not re-requested.
        A.check_mitigation(resp.status, resp.headers, url, resp.text)
        return resp


# --- URL builders -------------------------------------------------------------


def search_url(role: str, location: str, page: int = 1) -> str:
    """Wellfound's SEO landing shapes.

    'remote' is not a location -- it maps to /role/r/{role}, a different page
    type whose GraphQL args carry remote:true instead of a location member.
    """
    q = f"?page={page}" if page > 1 else ""
    if location == "remote":
        return f"{BASE}/role/r/{role}{q}"
    return f"{BASE}/role/l/{role}/{location}{q}"


def job_url(job_id: str, slug: str) -> str:
    return f"{BASE}/jobs/{job_id}-{slug}"

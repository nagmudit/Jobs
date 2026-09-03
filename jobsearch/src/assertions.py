"""Hard assertions against Wellfound's silent-failure modes.

All of these return HTTP 200 with plausible-looking data. Nothing crashes; the
corpus just quietly becomes wrong. So they raise. They are never warnings and
are never swallowed inside a crawl loop.
"""

from __future__ import annotations

import json
import re
from typing import Any


class CorpusIntegrityError(RuntimeError):
    """Something returned 200 but the data cannot be trusted."""


class SilentRoleFallback(CorpusIntegrityError):
    """Invalid role slug -> unfiltered location-wide search, still HTTP 200."""


class PageWrap(CorpusIntegrityError):
    """Past the last real page the server re-serves page 1, byte-identical.

    Raised so the crawl loop can catch it and close the slice cleanly as
    ended_reason='page_wrap'. This is the normal end of a slice, not a bug --
    it is an exception because continuing would silently duplicate page 1.
    """


class YieldFloor(CorpusIntegrityError):
    """A page yielded implausibly few jobs -- the parse path likely broke."""


class SchemaDrift(CorpusIntegrityError):
    """__NEXT_DATA__ absent but RSC flight chunks present -> App Router migration."""


class MitigationDetected(CorpusIntegrityError):
    """Cloudflare acted on us. Halt the crawl; never retry through it."""


CACHE_KEY_RE = re.compile(r"seoLandingPageJobSearchResults\((.*)\)$")


def parse_query_key(key: str) -> dict:
    """The GraphQL cache key embeds the args the server ACTUALLY filtered on.
    That is ground truth; the URL we requested is not."""
    m = CACHE_KEY_RE.search(key)
    if not m:
        raise CorpusIntegrityError(f"unrecognised search cache key: {key!r}")
    return json.loads(m.group(1))


def check_schema(html: str, url: str) -> None:
    if "__NEXT_DATA__" in html:
        return
    flight = len(re.findall(r"self\.__next_f\.push", html))
    if flight:
        raise SchemaDrift(
            f"{url}: no __NEXT_DATA__ but {flight} self.__next_f.push flight chunks. "
            "Wellfound migrated to the Next.js App Router; src/parse.py must be "
            "rewritten against the RSC payload."
        )
    raise SchemaDrift(
        f"{url}: no __NEXT_DATA__ and no RSC flight chunks; page shape unrecognised "
        f"({len(html)} bytes)."
    )


def check_mitigation(status: int | None, headers: dict, url: str, body: str = "") -> None:
    """Halt on the first sign Cloudflare is acting on us."""
    mitigated = (headers or {}).get("cf-mitigated")
    ray = (headers or {}).get("cf-ray")
    if mitigated:
        raise MitigationDetected(
            f"{url}: cf-mitigated={mitigated!r} cf-ray={ray} status={status}. "
            "HALTING -- polite-traffic ceiling reached."
        )
    if status in (403, 429, 503):
        raise MitigationDetected(
            f"{url}: HTTP {status} cf-ray={ray}. HALTING. "
            f"Body: {body[:300].replace(chr(10), ' ')!r}"
        )


def check_role_applied(args: dict, requested_role: str, url: str) -> None:
    if "role" not in args:
        raise SilentRoleFallback(
            f"{url}: requested role={requested_role!r} but the server's GraphQL args "
            f"were {args} -- no 'role' member. Results are location-wide, NOT "
            f"role-filtered. This is a 200, not an error, and would poison the corpus."
        )
    if args["role"] != requested_role:
        raise SilentRoleFallback(
            f"{url}: requested role={requested_role!r}, server filtered on {args['role']!r}"
        )


def check_page(args: dict, requested_page: int, url: str) -> None:
    got = args.get("page")
    if got != requested_page:
        raise PageWrap(
            f"{url}: requested page={requested_page}, server returned page={got}. "
            "End of slice (Wellfound wraps to page 1 past the last real page)."
        )


def check_yield(n_jobs: int, floor: int, url: str, prev_yield: int | None = None) -> None:
    """A parse that silently starts returning 0 looks exactly like a genuinely
    empty result. If earlier pages in this slice yielded plenty and this one
    yields nothing, that is a broken parse, not an empty page."""
    if n_jobs >= floor:
        return
    if prev_yield is not None and prev_yield >= 10:
        raise YieldFloor(
            f"{url}: parsed {n_jobs} jobs (floor={floor}) but the previous page "
            f"yielded {prev_yield}. The extraction path is broken, not exhausted."
        )

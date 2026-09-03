"""Hard assertions against Wellfound's silent-failure modes.

Every one of these returns HTTP 200 with plausible-looking data. That is the
whole problem: nothing crashes, the corpus just quietly becomes wrong. So these
raise. They are never warnings, and they are never caught-and-logged inside a
stage loop -- a stage that trips one halts.

Phase 1 measured all four; see REPORT.md section 5.
"""

from __future__ import annotations

import json
import re
from typing import Any


class CorpusIntegrityError(RuntimeError):
    """Base: something returned 200 but the data cannot be trusted."""


class SilentRoleFallback(CorpusIntegrityError):
    """Bad role slug -> unfiltered location-wide search, still HTTP 200."""


class PageWrap(CorpusIntegrityError):
    """Past the last real page the server re-serves page 1, byte-identical."""


class YieldFloor(CorpusIntegrityError):
    """Page parsed but yielded implausibly few jobs -- likely a broken parse."""


class SchemaDrift(CorpusIntegrityError):
    """__NEXT_DATA__ gone, RSC flight chunks present -> App Router migration."""


class MitigationDetected(CorpusIntegrityError):
    """Cloudflare acted on us. Halt the stage; do not retry through it."""


# --- schema / transport gates -------------------------------------------------


def check_schema(html: str, url: str) -> None:
    """Detect a Next.js Pages->App Router migration before parsing fails weirdly."""
    if "__NEXT_DATA__" in html:
        return
    flight = len(re.findall(r"self\.__next_f\.push", html))
    if flight:
        raise SchemaDrift(
            f"{url}: no __NEXT_DATA__ but {flight} self.__next_f.push flight chunks -- "
            "Wellfound migrated to the Next.js App Router. The Apollo-cache parse path "
            "in stages/b_search_union.py must be rewritten against the RSC payload."
        )
    raise SchemaDrift(
        f"{url}: no __NEXT_DATA__ and no RSC flight chunks. Page shape is unrecognised "
        f"({len(html)} bytes). Refusing to parse."
    )


def check_mitigation(resp: Any, url: str) -> None:
    """Halt on the first sign Cloudflare is acting on us.

    Phase 2 is a 50-100x traffic increase over Phase 1 and the polite-traffic
    ceiling is unknown. Finding it is a legitimate result; pushing past it is not.
    """
    mitigated = (resp.headers or {}).get("cf-mitigated")
    if mitigated:
        raise MitigationDetected(
            f"{url}: cf-mitigated={mitigated!r} cf-ray={(resp.headers or {}).get('cf-ray')} "
            f"status={resp.status}. HALTING -- polite-traffic ceiling reached."
        )
    if resp.status in (403, 429, 503):
        snippet = (resp.text or "")[:300].replace("\n", " ")
        raise MitigationDetected(
            f"{url}: HTTP {resp.status} cf-ray={(resp.headers or {}).get('cf-ray')}. "
            f"HALTING. Body: {snippet!r}"
        )


# --- query-key gates ----------------------------------------------------------

CACHE_KEY_RE = re.compile(r"seoLandingPageJobSearchResults\((.*)\)$")


def parse_query_key(key: str) -> dict:
    """The GraphQL cache key embeds the args the server ACTUALLY filtered on.
    That is our ground truth -- the URL is not."""
    m = CACHE_KEY_RE.search(key)
    if not m:
        raise CorpusIntegrityError(f"unrecognised search cache key: {key!r}")
    return json.loads(m.group(1))


def check_role_applied(args: dict, requested_role: str | None, url: str) -> None:
    if requested_role is None:
        return
    if "role" not in args:
        raise SilentRoleFallback(
            f"{url}: requested role={requested_role!r} but the server's GraphQL args "
            f"were {args} -- no 'role' member. Results are location-wide, NOT "
            f"role-filtered. This is a 200, not an error, and would silently poison "
            f"the corpus."
        )
    if args["role"] != requested_role:
        raise SilentRoleFallback(
            f"{url}: requested role={requested_role!r} but server filtered on "
            f"role={args['role']!r}"
        )


def check_page(args: dict, requested_page: int, url: str) -> None:
    got = args.get("page")
    if got != requested_page:
        raise PageWrap(
            f"{url}: requested page={requested_page} but server returned page={got}. "
            f"Wellfound wraps to page 1 past the last real page -- continuing would "
            f"re-ingest page 1 indefinitely."
        )


def check_yield(n_jobs: int, floor: int, url: str, *, allow_empty: bool = False) -> None:
    """A parse path that silently starts returning 0 looks exactly like a
    genuinely empty result. Callers that can legitimately see an empty page
    (the tail of a result set) pass allow_empty and handle termination."""
    if allow_empty and n_jobs == 0:
        return
    if n_jobs < floor:
        raise YieldFloor(
            f"{url}: parsed only {n_jobs} jobs (floor={floor}). Either the page shape "
            f"changed or the extraction path is broken."
        )

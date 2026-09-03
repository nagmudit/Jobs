"""robots.txt enforcement.

Not in the brief's file list, but the hard constraints require respecting
robots.txt, and I'd rather that be a gate every probe passes through than a
rule I remember to follow by hand. Wellfound's rules use wildcard patterns
(`/*?role=*`) that stdlib urllib.robotparser handles inconsistently, so the
matcher is implemented directly.
"""

from __future__ import annotations

import fnmatch
from urllib.parse import urlparse

from .cache import fetch

_rules_cache: dict[str, list[tuple[bool, str]]] = {}


class RobotsDisallowed(RuntimeError):
    """Raised loudly when a probe tries to fetch a disallowed path."""


def _load(origin: str) -> list[tuple[bool, str]]:
    if origin in _rules_cache:
        return _rules_cache[origin]
    r = fetch(f"{origin}/robots.txt", verbose=False)
    rules: list[tuple[bool, str]] = []
    if r.ok:
        applies = False
        for line in r.text.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                applies = v == "*"
            elif applies and k in ("disallow", "allow") and v:
                rules.append((k == "allow", v))
    _rules_cache[origin] = rules
    return rules


def allowed(url: str) -> tuple[bool, str | None]:
    """(is_allowed, matching_rule). Longest-match wins; Allow beats Disallow."""
    p = urlparse(url)
    origin = f"{p.scheme}://{p.netloc}"
    path = p.path or "/"
    if p.query:
        path += "?" + p.query

    best: tuple[int, bool, str] | None = None
    for is_allow, pattern in _load(origin):
        pat = pattern if "*" in pattern or pattern.endswith("$") else pattern + "*"
        if fnmatch.fnmatchcase(path, pat):
            score = len(pattern)
            if best is None or score > best[0] or (score == best[0] and is_allow):
                best = (score, is_allow, pattern)
    if best is None:
        return True, None
    return best[1], best[2]


def assert_allowed(url: str) -> None:
    ok, rule = allowed(url)
    if not ok:
        raise RobotsDisallowed(f"robots.txt disallows {url} (rule: Disallow: {rule})")

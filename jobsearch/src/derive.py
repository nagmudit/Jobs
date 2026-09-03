"""Derived scalars, registered as SQLite functions and used inside the jobs view.

Keeping these as functions rather than hardcoded SQL means the derivation
improves for free: fix a regex here and every row re-derives on the next query,
with no re-crawl and no migration.

The raw string is always preserved alongside the parsed value. An unparsed
value is strictly better than a silently wrong one, so anything unrecognised
returns NULL rather than a guess.
"""

from __future__ import annotations

import re
from typing import Any

# "$25k – $50k", "$120k - $200k", "₹15L – ₹25L", "€60k", "50,000 - 70,000"
# Wellfound uses an en-dash; hyphens and em-dashes appear too.
_RANGE_SPLIT = re.compile(r"\s*[–—-]\s*")
_NUM = re.compile(
    r"(?P<cur>[$₹€£])?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>cr|[klm])?",
    re.IGNORECASE,
)

_UNIT_MULT = {
    "k": 1_000,
    "m": 1_000_000,
    "l": 100_000,      # Indian lakh
    "cr": 10_000_000,  # Indian crore
}


def _one(part: str) -> int | None:
    m = _NUM.search(part or "")
    if not m:
        return None
    try:
        n = float(m.group("num").replace(",", ""))
    except ValueError:
        return None
    unit = (m.group("unit") or "").lower()
    mult = _UNIT_MULT.get(unit)
    if mult is None and unit:
        return None          # unrecognised unit -> refuse to guess
    if mult:
        n *= mult
    # A bare number under 1000 in a comp field is not a salary we can trust.
    if n < 1000:
        return None
    return int(n)


def salary_bounds(raw: str | None) -> tuple[int | None, int | None]:
    if not raw or not isinstance(raw, str):
        return None, None
    s = raw.strip()
    if not s or s.lower() in ("no salary", "none", "-"):
        return None, None
    # Equity-only strings like "0.0% – 1.0%" are not salaries.
    if "%" in s and "$" not in s and "₹" not in s:
        return None, None
    parts = _RANGE_SPLIT.split(s)
    if len(parts) >= 2:
        lo, hi = _one(parts[0]), _one(parts[1])
        # "$25k – $50k": the second half carries the currency, first may not.
        if lo is not None and hi is not None and hi < lo:
            lo, hi = hi, lo
        return lo, hi
    v = _one(s)
    return v, v


def salary_min(raw: str | None) -> int | None:
    return salary_bounds(raw)[0]


def salary_max(raw: str | None) -> int | None:
    return salary_bounds(raw)[1]


# "0.0% – 1.0%" -> (0.0, 1.0)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def equity_bounds(raw: str | None) -> tuple[float | None, float | None]:
    if not raw or not isinstance(raw, str):
        return None, None
    vals = [float(x) for x in _PCT.findall(raw)]
    if not vals:
        return None, None
    return min(vals), max(vals)


def equity_max(raw: str | None) -> float | None:
    return equity_bounds(raw)[1]


# Wellfound ships company size as an enum (SIZE_11_50). Map it for display and
# expose a sortable lower bound. The raw enum stays in company_raw regardless.
_SIZE_LABELS = {
    "SIZE_1_10": "1-10",
    "SIZE_11_50": "11-50",
    "SIZE_51_200": "51-200",
    "SIZE_201_500": "201-500",
    "SIZE_501_1000": "501-1000",
    "SIZE_1001_5000": "1001-5000",
    "SIZE_5000_PLUS": "5000+",
    "SIZE_5001_10000": "5001-10000",
    "SIZE_10001_PLUS": "10001+",
}


def size_label(raw: str | None) -> str | None:
    if not raw:
        return None
    if raw in _SIZE_LABELS:
        return _SIZE_LABELS[raw]
    # Unknown enum: surface it rather than hiding it, so a new bucket is visible.
    return raw.replace("SIZE_", "").replace("_", "-").lower()


def size_min(raw: str | None) -> int | None:
    """Lower bound of the size bucket, so the UI can sort and filter numerically."""
    if not raw:
        return None
    m = re.search(r"SIZE_(\d+)", raw)
    return int(m.group(1)) if m else None


# remoteConfig.kind is ONSITE / REMOTE / ONSITE_OR_REMOTE, but it is frequently
# null, in which case only the `remote` boolean is available. Both paths must
# produce the SAME label set or the UI ends up with "Onsite" and "On-site" as
# two separate, half-populated filter options.
_REMOTE_LABELS = {
    "ONSITE": "Onsite",
    "REMOTE": "Remote",
    "ONSITE_OR_REMOTE": "Onsite or Remote",
}


def remote_label(kind: str | None, remote: Any = None) -> str | None:
    if kind:
        return _REMOTE_LABELS.get(str(kind), str(kind).replace("_", " ").title())
    if remote in (1, True):
        return "Remote"
    if remote in (0, False):
        return "Onsite"
    return None


def register(conn) -> None:
    """Attach the derivations to a connection so the jobs view can call them."""
    conn.create_function("salary_min", 1, salary_min, deterministic=True)
    conn.create_function("salary_max", 1, salary_max, deterministic=True)
    conn.create_function("equity_max", 1, equity_max, deterministic=True)
    conn.create_function("size_label", 1, size_label, deterministic=True)
    conn.create_function("size_min", 1, size_min, deterministic=True)
    conn.create_function("remote_label", 2, remote_label, deterministic=True)

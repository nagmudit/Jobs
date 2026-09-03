"""Single normalized Job record that every probe emits.

All probes, regardless of source, must produce this shape so the comparison
matrix in REPORT.md is apples-to-apples. Fields that a given source cannot
supply stay None -- never invent or infer them, the whole point of the probe
is measuring which fields each path actually recovers.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


# The 12 fields we score coverage against.
JOB_FIELDS = [
    "source",
    "source_job_id",
    "company",
    "title",
    "location_raw",
    "remote",
    "description",
    "salary_raw",
    "equity_raw",
    "posted_at",
    "apply_url",
    "fetched_at",
]


@dataclass
class Job:
    source: str                       # which probe/provider produced this
    source_job_id: str | None = None
    company: str | None = None
    title: str | None = None
    location_raw: str | None = None
    remote: bool | None = None
    description: str | None = None
    salary_raw: str | None = None
    equity_raw: str | None = None
    posted_at: str | None = None      # ISO8601 string, source-reported
    apply_url: str | None = None
    fetched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def filled_fields(self) -> list[str]:
        """Names of the 12 fields that carry real data."""
        d = self.to_dict()
        out = []
        for k in JOB_FIELDS:
            v = d.get(k)
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            out.append(k)
        return out


def coverage(jobs: list[Job]) -> dict[str, Any]:
    """Per-field fill rate across a list of jobs. Used directly by REPORT.md."""
    if not jobs:
        return {"n_jobs": 0, "per_field": {}, "mean_fields_filled": 0.0}
    per_field: dict[str, int] = {f: 0 for f in JOB_FIELDS}
    total = 0
    for j in jobs:
        filled = j.filled_fields()
        total += len(filled)
        for f in filled:
            per_field[f] += 1
    n = len(jobs)
    return {
        "n_jobs": n,
        "per_field": {f: {"n": c, "pct": round(100.0 * c / n, 1)} for f, c in per_field.items()},
        "mean_fields_filled": round(total / n, 2),
        "fields_ever_filled": sorted([f for f, c in per_field.items() if c > 0]),
        "fields_never_filled": sorted([f for f, c in per_field.items() if c == 0]),
    }

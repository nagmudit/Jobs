"""Assemble the REPORT.md comparison matrix from results/*.json.

Keeps the report honest: every number in the matrix is read back out of the
raw probe output rather than typed in by hand.

    python -m src.matrix
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import JOB_FIELDS

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"

# Judgement columns -- these are assessments, not measurements, and are labelled
# as such in the report.
JUDGEMENT = {
    "p1_static": {"fragility": 3, "setup": "trivial", "cost": "$0", "maint": "low-medium"},
    "p2_structured": {"fragility": 2, "setup": "trivial", "cost": "$0", "maint": "low"},
    "p3_feeds": {"fragility": "n/a", "setup": "n/a", "cost": "n/a", "maint": "n/a"},
    "p4_browser": {"fragility": 4, "setup": "heavy", "cost": "$0 + RAM/CPU", "maint": "high"},
    "p6_ats": {"fragility": 1, "setup": "moderate", "cost": "$0", "maint": "very low"},
}


def load(name: str) -> dict | None:
    p = RESULTS / name
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> None:
    files = {
        "p1_static": "p1_static.json",
        "p2_structured": "p2_structured.json",
        "p3_feeds": "p3_feeds.json",
        "p4_browser": "p4_browser.json",
        "p6_ats": "p6_ats.json",
    }
    rows = []
    for probe, fn in files.items():
        d = load(fn)
        if d is None:
            rows.append((probe, "NOT RUN", "-", "-", "-", "-", "-", "-"))
            continue
        cov = d.get("coverage") or {}
        ever = cov.get("fields_ever_filled") or []
        n_fields = len([f for f in ever if f in JOB_FIELDS])
        j = JUDGEMENT[probe]
        rows.append(
            (
                probe,
                d.get("verdict", "?"),
                f"{n_fields}/12",
                str(cov.get("n_jobs", 0)),
                str(j["fragility"]),
                j["setup"],
                j["cost"],
                j["maint"],
            )
        )

    hdr = ["probe", "verdict", "fields", "jobs", "fragility", "setup", "cost", "maint"]
    widths = [max(len(str(r[i])) for r in rows + [tuple(hdr)]) for i in range(len(hdr))]
    line = lambda cells: "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)) + " |"
    print(line(hdr))
    print("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for r in rows:
        print(line(r))

    print("\nPER-FIELD FILL RATES")
    for probe, fn in files.items():
        d = load(fn)
        if not d or not (d.get("coverage") or {}).get("n_jobs"):
            continue
        pf = d["coverage"]["per_field"]
        print(f"\n  {probe} (n={d['coverage']['n_jobs']}):")
        for f in JOB_FIELDS:
            print(f"    {f:16} {pf[f]['pct']:6.1f}%  ({pf[f]['n']})")

    p6 = load("p6_ats.json")
    if p6:
        print("\nP6 ATS HIT RATE")
        for k, v in p6["stats"].items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()

"""Validate quality/test-manifest.yaml.

Implements the rules from .agents/skills/test-governance/references/manifest.md.
Rules 2, 5 and 6 are the ones that matter; the rest is hygiene.

    python quality/validate_manifest.py

Exit 0 = valid. Exit 1 = a rule failed, with the reason on stderr.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "quality" / "test-manifest.yaml"


def main() -> int:
    m = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    errs: list[str] = []
    warns: list[str] = []

    caps = {c["id"] for c in m.get("capabilities", [])}
    journeys = m.get("journeys", [])
    tests = m.get("tests", [])
    gaps = m.get("known_gaps", [])
    accs = m.get("risk_acceptances", []) or []

    # 1. IDs unique; references resolve.
    for name, items in (("journey", journeys), ("test", tests), ("gap", gaps)):
        ids = [i["id"] for i in items]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            errs.append(f"duplicate {name} ids: {sorted(dupes)}")
    jset = {j["id"] for j in journeys}
    for j in journeys:
        if j["capability"] not in caps:
            errs.append(f"{j['id']} references unknown capability {j['capability']}")
    for t in tests:
        if t["journey"] not in jset:
            errs.append(f"{t['id']} references unknown journey {t['journey']}")
    for g in gaps:
        if g["journey"] not in jset:
            errs.append(f"{g['id']} references unknown journey {g['journey']}")

    # 2. No silent P0 journeys. THE rule.
    covered = {t["journey"] for t in tests if t.get("status") == "implemented"}
    open_gap = {g["journey"] for g in gaps if g.get("status") == "open"}
    accepted = {g["journey"] for g in gaps if g.get("status") == "accepted"}
    for j in journeys:
        if j["priority"] == "P0" and j["id"] not in covered | open_gap | accepted:
            errs.append(
                f"RULE 2: P0 journey {j['id']} has no implemented test, no open gap, "
                f"and no acceptance. Silence is not allowed."
            )

    # 3. Implemented tests point at files that exist.
    for t in tests:
        if t.get("status") == "implemented" and not (ROOT / t["location"]).exists():
            errs.append(f"{t['id']} location does not exist: {t['location']}")

    # 4. merge_blocking implies a command.
    for t in tests:
        if t.get("merge_blocking") and not t.get("command"):
            errs.append(f"{t['id']} is merge_blocking with no command")

    # 5. Open P0/P1 gaps have owners.
    for g in gaps:
        if g.get("status") == "open" and g["priority"] in ("P0", "P1") and not g.get("owner"):
            errs.append(f"RULE 5: {g['id']} is an open {g['priority']} gap with no owner")

    # 6. No expired risk acceptances.
    today = dt.date.today()
    for a in accs:
        exp = a.get("expires")
        if not exp:
            errs.append(f"RULE 6: acceptance for {a.get('gap')} has no expiry")
        elif isinstance(exp, dt.date) and exp < today:
            errs.append(f"RULE 6: acceptance for {a.get('gap')} expired {exp}")

    # 8. No skipped / .only tests on disk.
    import re

    pat = re.compile(r"@pytest\.mark\.skip|\.only\(|pytest\.skip\(")
    for f in (ROOT / "jobsearch" / "tests").rglob("test_*.py"):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if pat.search(line):
                errs.append(f"RULE 8: skipped/only test at {f.relative_to(ROOT)}:{n}")

    # 7. Test files on disk with no manifest entry -> warn only.
    known = {t["location"] for t in tests}
    for f in (ROOT / "jobsearch" / "tests").rglob("test_*.py"):
        rel = str(f.relative_to(ROOT)).replace("\\", "/")
        if rel not in known:
            warns.append(f"RULE 7: {rel} has no manifest entry")

    for w in warns:
        print(f"warn: {w}")
    for e in errs:
        print(f"FAIL: {e}", file=sys.stderr)

    if errs:
        print(f"\n{len(errs)} error(s), {len(warns)} warning(s)", file=sys.stderr)
        return 1
    print(f"manifest valid  ({len(journeys)} journeys, {len(tests)} tests, "
          f"{sum(1 for g in gaps if g.get('status') == 'open')} open gaps, "
          f"{len(warns)} warning(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

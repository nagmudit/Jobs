"""Drift checks for the context system.

Implements the mechanical checks from
`.agents/skills/context/references/drift.md`. Prose asks; this enforces.

    python quality/validate_context.py

Exit 0 = clean. Exit 1 = drift found, with the reason on stderr.

Paths that the manifest lists as `status: proposed` are excluded from the dead-path
check. A proposed file is *supposed* not to exist yet -- that distinction is the whole
point of the status field, and flagging it would train everyone to ignore this script.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DOC_GLOBS = ["docs/**/*.md", "AGENTS.md", "CLAUDE.md"]
PATH_RE = re.compile(r"\b(?:jobsearch|wellfound-probe|quality|docs|src|tests)/[A-Za-z0-9_/.\-]+")
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)#]+)\)")
SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-"
    r"|BEGIN [A-Z ]*PRIVATE KEY|://[^\s/]+:[^\s@]+@"
)
AGENTS_MAX_LINES = 200


def proposed_paths() -> set[str]:
    """Locations the manifest says do not exist yet."""
    mf = ROOT / "quality" / "test-manifest.yaml"
    if not mf.exists():
        return set()
    m = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    out = set()
    for t in m.get("tests", []):
        if t.get("status") == "proposed" and t.get("location"):
            loc = t["location"]
            out.add(loc)
            out.add(loc.split("/", 1)[-1])  # jobsearch/tests/x.py -> tests/x.py
    return out


def docs() -> list[Path]:
    seen: list[Path] = []
    for g in DOC_GLOBS:
        seen.extend(p for p in ROOT.glob(g) if p.is_file())
    return seen


def main() -> int:
    errs: list[str] = []
    warns: list[str] = []
    future = proposed_paths()
    files = docs()

    for d in files:
        text = d.read_text(encoding="utf-8", errors="replace")
        rel = d.relative_to(ROOT)

        # Dead paths -- highest hit rate of any check.
        for raw in set(PATH_RE.findall(text)):
            p = raw.rstrip(".,;:)").rstrip("/")
            if "*" in p or p in future:
                continue
            if not any((ROOT / c / p).exists() for c in ("", "jobsearch", "wellfound-probe")):
                errs.append(f"dead path  {rel}: {p}")

        # Relative links resolve.
        for href in LINK_RE.findall(text):
            if href.startswith(("http://", "https://", "mailto:")):
                continue
            if not (d.parent / href).resolve().exists():
                errs.append(f"broken link {rel}: {href}")

        # Secrets. P0 always -- docs get pasted into issues far more casually than code.
        for n, line in enumerate(text.splitlines(), 1):
            if SECRET_RE.search(line):
                errs.append(f"possible secret {rel}:{n}")

    # Duplicate ADR ids.
    ids = [f.name.split("-")[1] for f in ROOT.glob("docs/architecture/decisions/ADR-*.md")]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        errs.append(f"duplicate ADR ids: {sorted(dupes)}")

    # Orphan docs.
    idx_p = ROOT / "docs" / "index.md"
    if idx_p.exists():
        idx = idx_p.read_text(encoding="utf-8")
        for f in ROOT.glob("docs/**/*.md"):
            if f.name != "index.md" and f.name not in idx:
                warns.append(f"orphan (absent from docs/index.md): {f.relative_to(ROOT)}")

    # AGENTS.md bloat.
    a = ROOT / "AGENTS.md"
    if a.exists():
        n = len(a.read_text(encoding="utf-8").splitlines())
        if n > AGENTS_MAX_LINES:
            errs.append(f"AGENTS.md is {n} lines (> {AGENTS_MAX_LINES}); split it")

    for w in warns:
        print(f"warn: {w}")
    for e in errs:
        print(f"FAIL: {e}", file=sys.stderr)

    if errs:
        print(f"\n{len(errs)} error(s), {len(warns)} warning(s)", file=sys.stderr)
        return 1
    print(f"context valid  ({len(files)} docs, {len(future)} proposed paths excluded, "
          f"{len(warns)} warning(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

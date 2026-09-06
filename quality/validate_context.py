"""Drift checks for the context system.

Implements the mechanical checks from
`.agents/skills/context/references/drift.md`. Prose asks; this enforces.

    python quality/validate_context.py

Exit 0 = clean. Exit 1 = drift found, with the reason on stderr.

Two kinds of path are excluded from the dead-path check, both because they are
*supposed* not to exist:

  * anything the manifest lists as `status: proposed` -- that distinction is the whole
    point of the status field;
  * anything git ignores. `wellfound-probe/cache/` and `jobsearch/jobs.db` are
    generated, documented, and absent from a fresh checkout. Without this the script
    passes on a developer machine and fails in CI, which is the worst possible split:
    the failure lands on someone who did not cause it and cannot reproduce it.

Flagging either would train everyone to ignore this script.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DOC_GLOBS = ["docs/**/*.md", "AGENTS.md", "CLAUDE.md"]
# (?<!/) so an ABSOLUTE host path in a deployment doc -- /opt/jobsearch/.venv,
# /etc/jobsearch/web.env -- is not read as a repo-relative path that then fails
# the dead-path check. A repo path reference is never preceded by a slash.
PATH_RE = re.compile(
    r"(?<!/)\b(?:jobsearch|wellfound-probe|quality|docs|src|tests)/[A-Za-z0-9_/.\-]+")
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)#]+)\)")
SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-"
    r"|BEGIN [A-Z ]*PRIVATE KEY|://[^\s/]+:[^\s@]+@"
)
AGENTS_MAX_LINES = 200


def git_ignored(paths: set[str]) -> set[str]:
    """Which of these does git deliberately ignore?

    Asks git rather than parsing .gitignore: git is the authority, and it handles
    negations, directory-only patterns and nested ignore files correctly. Works on
    paths that do not exist, which is exactly the CI case.

    Degrades to "nothing is ignored" if git is unavailable, so a missing git makes
    the check stricter rather than silently blind.
    """
    if not paths:
        return set()
    probes = sorted({q for p in paths for q in (p, p + "/")})
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=ROOT, input=chr(10).join(probes),
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    # Exit 0 = some matched, 1 = none matched, 128 = not a repo / git error.
    if proc.returncode not in (0, 1):
        return set()
    # check-ignore echoes the paths back exactly as fed, so these are
    # already the forward-slash forms the docs use.
    hits = {line.strip().rstrip("/")
            for line in proc.stdout.splitlines() if line.strip()}
    return {p for p in paths if p in hits}


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
    missing: list[tuple[Path, str]] = []

    for d in files:
        text = d.read_text(encoding="utf-8", errors="replace")
        rel = d.relative_to(ROOT)

        # Dead paths -- highest hit rate of any check. Collected rather than
        # reported inline: whether a missing path is *generated* takes one batched
        # call to git, made once after every doc has been read.
        for raw in set(PATH_RE.findall(text)):
            p = raw.rstrip(".,;:)").rstrip("/")
            if "*" in p or p in future:
                continue
            if not any((ROOT / c / p).exists() for c in ("", "jobsearch", "wellfound-probe")):
                missing.append((rel, p))

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

    # A missing path that git ignores is generated, not dead -- it is absent from a
    # fresh checkout by design. Resolving this here, in one batched call, is what
    # stops the script passing locally and failing in CI.
    generated = git_ignored({p for _, p in missing})
    for rel, p in missing:
        if p not in generated:
            errs.append(f"dead path  {rel}: {p}")

    for w in warns:
        print(f"warn: {w}")
    for e in errs:
        print(f"FAIL: {e}", file=sys.stderr)

    if errs:
        print(f"\n{len(errs)} error(s), {len(warns)} warning(s)", file=sys.stderr)
        return 1
    print(f"context valid  ({len(files)} docs, {len(future)} proposed paths excluded, "
          f"{len(generated)} generated path(s) excluded, {len(warns)} warning(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

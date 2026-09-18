"""Bootstrap for the hosted (Vercel) deployment.

Lives here rather than in `api/index.py` so it is covered by the normal suite.
The entrypoint Vercel imports is a thin shim over these two functions.

The deployment bundle at /var/task is read-only and SQLite in WAL mode must
create `-wal`/`-shm` sidecars, so the corpus is staged into a writable temp dir
before it is opened.

The published corpus carries no user marks -- the daily workflow strips
`user_state` before upload -- so there is nothing personal to restore here. Marks
live on the user's machine and are carried across a refresh by `src.cli sync`.
"""

from __future__ import annotations

import gzip
import shutil
import tempfile
from pathlib import Path

# Below this, a download truncated or a database was never written. Serving it
# would fail at query time, on the reader's first page load, rather than here.
MIN_CORPUS_BYTES = 1_000_000


def temp_dir() -> Path:
    """/tmp on Vercel; the platform temp dir anywhere else, so this is runnable
    and testable off Vercel."""
    return Path(tempfile.gettempdir())


def stage_corpus(bundled: Path, dest: Path | None = None) -> Path:
    """Put the bundled corpus somewhere writable. Idempotent.

    The bundle holds `jobs.db.gz` (scripts/vercel-build.sh gzips it: ~4x
    smaller, and the plain file no longer fits Vercel's 225 MB function limit)
    or, off Vercel, a plain `jobs.db`. Only a cold start pays for the copy;
    warm invocations find the file already staged and return immediately.

    Written to a temp name and renamed into place, because the staged file is
    also the "already done" marker: a copy cut short would otherwise be served
    as the corpus on every later invocation.
    """
    dest = dest or (temp_dir() / "jobs.db")
    if dest.exists() and dest.stat().st_size >= MIN_CORPUS_BYTES:
        return dest
    gz = bundled.with_name(bundled.name + ".gz")
    if not bundled.exists() and not gz.exists():
        raise RuntimeError(
            f"no corpus at {bundled} or {gz}. scripts/vercel-build.sh downloads "
            "it from the `corpus` release asset at build time; check CORPUS_URL."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        if bundled.exists():
            shutil.copy2(bundled, part)
        else:
            with gzip.open(gz, "rb") as src, open(part, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
        size = part.stat().st_size
        if size < MIN_CORPUS_BYTES:
            raise RuntimeError(f"corpus from {bundled if bundled.exists() else gz} "
                               f"looks truncated ({size} bytes)")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest

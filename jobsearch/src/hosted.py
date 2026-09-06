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
    """Copy the read-only bundled corpus somewhere writable. Idempotent.

    Only a cold start pays the copy; warm invocations find the file already
    staged and return immediately.
    """
    dest = dest or (temp_dir() / "jobs.db")
    if dest.exists() and dest.stat().st_size >= MIN_CORPUS_BYTES:
        return dest
    if not bundled.exists():
        raise RuntimeError(
            f"no corpus at {bundled}. scripts/vercel-build.sh downloads it from "
            "the `corpus` release asset at build time; check CORPUS_URL."
        )
    size = bundled.stat().st_size
    if size < MIN_CORPUS_BYTES:
        raise RuntimeError(f"corpus at {bundled} looks truncated ({size} bytes)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(bundled, dest)
    return dest

"""Vercel entrypoint. Serves the UI read-only; it never crawls.

Vercel probes a fixed list of paths for a module-level `app`, and `api/index.py`
is one of them -- the original deploy failed because the app is built by a
factory (`web.app.create_app`) with nothing exported at module level.

The work is in `src/hosted.py` so it is covered by the suite; this file is the
shim that wires it to Vercel's filesystem layout.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jobsearch"))

# Read-only is the default here and is set before the app is built: this is a
# public URL, and an /api/fetch on it would be an internet-reachable trigger for
# crawling third-party sites. See web/app.py::create_app.
os.environ.setdefault("JOBSEARCH_READONLY", "1")

from src import hosted                # noqa: E402  -- after sys.path setup
from src.config import Config         # noqa: E402
from src.web.app import create_app    # noqa: E402

# Placed by scripts/vercel-build.sh at build time; never committed.
_db = hosted.stage_corpus(ROOT / "jobsearch" / "jobs.db")

os.environ["JOBSEARCH_DB"] = str(_db)
os.environ.setdefault("JOBSEARCH_CACHE", str(hosted.temp_dir() / "jobsearch-cache"))

app = create_app(Config.load())

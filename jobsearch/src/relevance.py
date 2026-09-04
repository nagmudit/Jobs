"""Local role-relevance matching, shared by the sources that need it.

Deliberately has no internal imports so both `src/sources/*` and `src/roles.py`
can use it without an import cycle.

## Why this exists

Only Wellfound filters by role server-side. Measured 2026-09-04:

* **Himalayas** ignores every filter parameter (`search`, `q`, `category`,
  `keyword`, `title`, `seniority`). `search=engineer` returned *Grants
  Coordinator*, *Account Executive*, *Industrial Power Systems Inspector*.
* **RemoteOK** honours `?tag=` in the sense that it changes the result set and
  returns zero for an unknown tag -- but the results are mostly not role
  relevant. Title-relevance by tag: `software` 83%, `machine learning` 60%,
  and then a cliff: `backend` 9%, `react` 5%, `devops` 3%, `mobile` 1%.
  `?tag=react` returns *Aviation Maintenance Technician*; `?tag=mobile` returns
  *Regional Sales Manager*.

So for both of those the role filter has to be applied here, locally, after the
fetch. A job is kept when its title or its categories match the role's
keywords.

**Matching is on title and categories, never the description.** Descriptions
mention "machine learning" in boilerplate on jobs that have nothing to do with
it, which would let almost everything through and quietly make the filter
meaningless.
"""

from __future__ import annotations

import re


def _norm(s: str) -> str:
    """Lowercase, collapse separators, pad with spaces for word-boundary tests."""
    return " " + re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip() + " "


def matches(title: str | None, categories: list | None,
            keywords: list[str] | None) -> bool:
    """True when the role's keywords appear in the title or the categories.

    An empty keyword list means "no role filter configured" -> keep everything,
    so a misconfigured role never silently empties the corpus.
    """
    if not keywords:
        return True
    hay = _norm(title or "")
    for c in categories or []:
        hay += _norm(c)
    for kw in keywords:
        k = _norm(kw)
        if not k.strip():
            continue
        # Substring over space-padded text = word-boundary match, so "ml" does
        # not match "html" and "ai" does not match "retail". That distinction
        # matters: RemoteOK's own `?tag=ml` returns 98 jobs, none of them ML.
        if k in hay:
            return True
    return False

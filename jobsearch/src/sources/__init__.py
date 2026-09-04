"""Source registry: one module per job platform.

A source supplies two things and nothing else:

  * `CORE_VIEW_SQL` -- a SELECT over `job_raw` mapping that platform's raw JSON
    onto the canonical column list below. Nothing else in the codebase needs to
    know the platform's field names.
  * `ingest(...)` -- fetches and stores raw rows. Free to page however the
    platform pages; must respect the shared Fetcher (robots, rate limit,
    mitigation halt) and must store payloads verbatim (ADR-002).

Adding a platform is a new module plus a line in `SOURCES`. Storage does not
change shape, because `job_raw.raw_json` already holds whatever the source gave.

**Every core view MUST emit CORE_COLUMNS, in order.** They are UNION ALL-ed, so
a mismatch is a silent column shift rather than an error -- `assert_core_views`
checks this at connect time.
"""

from __future__ import annotations

# The contract. Order matters: UNION ALL matches by position, not by name.
CORE_COLUMNS = [
    "source",
    "source_job_id",      # globally unique: "<source>:<native_id>"
    "native_id",
    "company_slug",
    "title",
    "company",
    "company_size",       # human label, e.g. "11-50"
    "company_size_min",   # numeric lower bound, sortable
    "high_concept",
    "badges",
    "location_raw",
    "remote_locations",
    "remote",             # 0/1
    "remote_config",
    "remote_label",       # "Onsite" | "Remote" | "Onsite or Remote"
    "job_type",
    "salary_raw",         # the string, when the platform gives one
    # Numeric ONLY when the platform states an annual figure. Himalayas mixes
    # hourly in with annual (3 of 40 sampled); emitting an hourly 45 alongside
    # an annual 45000 would sort silently wrong, so hourly stays raw-only.
    "salary_min_native",
    "salary_max_native",
    "salary_currency",    # USD / INR / EUR ... NULL when unstated
    "salary_period",      # 'annual' | 'hourly' | ...
    "posted_ts",          # unix seconds
    "expires_ts",         # unix seconds; NULL where the platform has no expiry
    "description",
    "ats_source",
    "primary_role_title",
    "auto_posted",
    "apply_url",
    "first_seen",
    "last_seen",
]


def registry() -> dict[str, object]:
    from . import ashby, greenhouse, himalayas, remoteok, wellfound, workable

    return {
        wellfound.NAME: wellfound,
        remoteok.NAME: remoteok,
        himalayas.NAME: himalayas,
        greenhouse.NAME: greenhouse,
        ashby.NAME: ashby,
        workable.NAME: workable,
    }


def core_view_sql() -> str:
    """`jobs_core` = every registered source's view, UNION ALL-ed."""
    parts = [m.CORE_VIEW_SQL.strip() for m in registry().values()]  # type: ignore[attr-defined]
    return "CREATE VIEW jobs_core AS\n" + "\nUNION ALL\n".join(parts) + ";"


def assert_core_views(conn) -> None:
    """Fail loudly if a source view drifts from the contract.

    UNION ALL aligns by position, so a source that adds, drops or reorders a
    column produces a view that still builds and silently returns another
    column's values. Checking is cheap; discovering it in the UI is not.
    """
    for name, mod in registry().items():
        cols = [d[0] for d in conn.execute(
            f"SELECT * FROM ({mod.CORE_VIEW_SQL}) LIMIT 0").description]  # type: ignore[attr-defined]
        if cols != CORE_COLUMNS:
            missing = [c for c in CORE_COLUMNS if c not in cols]
            extra = [c for c in cols if c not in CORE_COLUMNS]
            raise RuntimeError(
                f"source {name!r} core view does not match CORE_COLUMNS.\n"
                f"  missing: {missing}\n  extra: {extra}\n"
                f"  order differs: {cols != CORE_COLUMNS and not missing and not extra}"
            )

"""Probe runner.

    python -m src.run --probe p1
    python -m src.run --probe all
    python -m src.run --probe p4 --login       # one-time manual login for P4
    python -m src.run --probe p6 --refresh     # bypass cache

Probes are independent: each can be run alone and writes results/<probe>.json.
P2 and P6 consume results/p1_static.json (they need job URLs / company names),
so `--probe all` runs them in dependency order.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback

from .cache import write_results

PROBES = ["p1", "p2", "p3", "p4", "p6"]

DEFAULT_ROLE = "artificial-intelligence-engineer"
DEFAULT_LOCATION = "bangalore"


def _dispatch(name: str):
    if name == "p1":
        from .probes.p1_static import run
    elif name == "p2":
        from .probes.p2_structured import run
    elif name == "p3":
        from .probes.p3_feeds import run
    elif name == "p4":
        from .probes.p4_browser import run
    elif name == "p6":
        from .probes.p6_ats import run
    else:
        raise SystemExit(f"unknown probe {name!r}; choose from {PROBES + ['all']}")
    return run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wellfound data-acquisition feasibility probe")
    ap.add_argument("--probe", required=True, help=f"one of {PROBES + ['all']}")
    ap.add_argument("--role", default=DEFAULT_ROLE, help="Wellfound role slug")
    ap.add_argument("--location", default=DEFAULT_LOCATION, help="Wellfound location slug")
    ap.add_argument("--pages", type=int, default=2, help="pages of results (probe cap: 2)")
    ap.add_argument("--refresh", action="store_true", help="bypass cache and re-fetch")
    ap.add_argument("--limit", type=int, default=None, help="P6: cap companies resolved")
    ap.add_argument("--login", action="store_true", help="P4: open headed browser to log in")
    ap.add_argument("--headed", action="store_true", help="P4: run visibly")
    ap.add_argument(
        "--no-strict-role",
        action="store_true",
        help="P1: do not fail when Wellfound silently ignores the role slug",
    )
    args = ap.parse_args(argv)

    if args.login:
        from .probes.p4_browser import login

        login()
        return 0

    names = PROBES if args.probe == "all" else [args.probe]
    summary: dict[str, str] = {}
    failed = False

    for name in names:
        run = _dispatch(name)
        kwargs: dict = {
            "role": args.role,
            "location": args.location,
            "refresh": args.refresh,
        }
        if name in ("p1", "p4"):
            kwargs["pages"] = args.pages
        if name == "p1":
            kwargs["strict_role"] = not args.no_strict_role
        if name == "p4":
            kwargs["headless"] = not args.headed
        if name == "p6":
            kwargs["limit"] = args.limit

        try:
            res = run(**kwargs)
            summary[name] = res.get("verdict", "?")
        except Exception as e:
            # Fail loudly, but let the remaining probes run so one broken path
            # does not cost us the whole matrix.
            failed = True
            summary[name] = f"EXCEPTION {type(e).__name__}: {e}"
            print(f"\n!! probe {name} raised:", file=sys.stderr)
            traceback.print_exc()

    print("\n" + "=" * 72)
    print("SUMMARY")
    for k, v in summary.items():
        print(f"  {k:4} {v}")
    write_results("summary.json", summary)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

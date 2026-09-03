"""Phase 2 corpus builder.

    python -m src.run2 --stage a
    python -m src.run2 --stage b --resume
    python -m src.run2 --stage c --sample
    python -m src.run2 --stage d --sample
    python -m src.run2 --stage e

Stages are gated: each one measures its own marginal yield and stops. Nothing
chains automatically -- that is deliberate, so a stage that turns out not to be
worth its hours can be skipped before it is paid for.

Every stage is resumable (--resume, on by default) and halts on the first
Cloudflare mitigation rather than retrying through it.
"""

from __future__ import annotations

import argparse
import sys
import traceback

from .store import connect, counts, mitigation_events

STAGES = ["a", "b", "c", "d", "e"]

# Seeded from ai-engineer as briefed, plus artificial-intelligence-engineer,
# which Phase 1 independently validated (596 Bangalore jobs vs 215 for
# ai-engineer). Dropping a known-valid in-family slug would cost recall, which
# is the whole objective here.
DEFAULT_SEEDS = ["ai-engineer", "artificial-intelligence-engineer"]
DEFAULT_LOCATIONS = ["bangalore", "remote", "india"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Wellfound corpus builder (Phase 2)")
    ap.add_argument("--stage", required=True, help=f"one of {STAGES}")
    ap.add_argument("--seeds", default=",".join(DEFAULT_SEEDS))
    ap.add_argument("--locations", default=",".join(DEFAULT_LOCATIONS))
    ap.add_argument("--depth", type=int, default=2, help="stage A discovery depth")
    ap.add_argument("--max-candidates", type=int, default=60)
    ap.add_argument("--sample", action="store_true", help="stages C/D: sample mode")
    ap.add_argument("--sample-size", type=int, default=50)
    ap.add_argument("--max-pages", type=int, default=None, help="stage B: cap pages per pair")
    ap.add_argument("--refresh", action="store_true", help="bypass the HTTP cache")
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args(argv)

    kwargs = {
        "seeds": [s for s in args.seeds.split(",") if s],
        "locations": [s for s in args.locations.split(",") if s],
        "depth": args.depth,
        "max_candidates": args.max_candidates,
        "sample": args.sample,
        "sample_size": args.sample_size,
        "max_pages": args.max_pages,
        "refresh": args.refresh,
        "resume": not args.no_resume,
    }

    if args.stage == "a":
        from .stages.a_catalogue import run
    elif args.stage == "b":
        from .stages.b_search_union import run
    elif args.stage == "c":
        from .stages.c_company import run
    elif args.stage == "d":
        from .stages.d_detail import run
    elif args.stage == "e":
        from .stages.e_ats import run
    else:
        raise SystemExit(f"unknown stage {args.stage!r}; choose from {STAGES}")

    try:
        run(**kwargs)
    except Exception as e:
        # Halts are a legitimate result. Persist, report, exit non-zero.
        print(f"\n!! STAGE {args.stage.upper()} HALTED: {type(e).__name__}: {e}", file=sys.stderr)
        traceback.print_exc()
        _summary()
        return 2

    _summary()
    return 0


def _summary() -> None:
    conn = connect()
    c = counts(conn)
    print("\n" + "=" * 72)
    print("CORPUS STATE")
    for k, v in c.items():
        print(f"  {k:20} {v}")
    ev = mitigation_events(conn)
    if ev:
        print(f"\n  !! {len(ev)} MITIGATION EVENTS:")
        for r in ev[:10]:
            print(f"     {r['fetched_at']} {r['status']} {r['cf_mitigated']} {r['url']}")
    else:
        print("  mitigation events    0")


if __name__ == "__main__":
    raise SystemExit(main())

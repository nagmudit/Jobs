#!/usr/bin/env python3
"""Decide whether this hourly trigger of the daily fetch should actually fetch.

The daily fetch starts at a different time each day (2026-09-19). Himalayas
refused two scheduled runs and served two others (see
docs/plans/active/per-host-halt.md); a spread of start times shows whether the
refusal follows the time of day. It is NOT a way past a refusal: a host that
refuses is still skipped for the whole run and never retried (ADR-016).

GitHub cron cannot be random, and scheduled runs are delayed by hours or
dropped under load. So the workflow triggers hourly and asks this script:

- Today's start time is derived from the date, so every hourly trigger that
  day agrees on it, and it cannot be predicted from the previous day's.
- The first trigger at or after it fetches. A delayed or dropped trigger just
  hands over to the next hour.
- **At most one fetch attempt per UTC day**, manual runs included. A failed
  run is not retried the same day -- that would be a retry path.
- A manual dispatch always fetches, unless one already ran today and
  `force` is set to false.

    python scripts/daily_gate.py --event schedule --attempted 0
    -> prints go=true|false, delay=<seconds>, slot=HH:MM (for $GITHUB_OUTPUT)
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import sys

# Starts fall between 00:00 and 20:00 UTC, so even a start delayed by hours
# still finishes the same UTC day.
WINDOW_MINUTES = 20 * 60
SALT = "jobs-daily-fetch"


def slot(day: dt.date, salt: str = SALT) -> int:
    """Today's start, in minutes after 00:00 UTC. Deterministic per date."""
    h = hashlib.sha256(f"{salt}:{day.isoformat()}".encode()).digest()
    return int.from_bytes(h[:8], "big") % WINDOW_MINUTES


def decide(now: dt.datetime, attempted_today: int, event: str,
           salt: str = SALT) -> tuple[bool, int]:
    """(go, delay_seconds).

    `delay` spreads the start across the hour: a trigger fires near the top of
    the hour, so without it every start would land on the same minute.
    """
    if attempted_today > 0:
        return False, 0
    if event == "workflow_dispatch":
        return True, 0
    start = slot(now.date(), salt)
    minute_now = now.hour * 60 + now.minute
    if minute_now // 60 < start // 60:
        return False, 0          # the slot's hour has not come yet
    # In the slot's hour and before its minute: wait for it. Past it (a delayed
    # or dropped trigger): go now.
    return True, max(0, start - minute_now) * 60


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", required=True)
    ap.add_argument("--attempted", type=int, required=True,
                    help="fetch attempts already made today (UTC)")
    ap.add_argument("--now", default=None, help="ISO time, for testing")
    a = ap.parse_args(argv)
    now = (dt.datetime.fromisoformat(a.now) if a.now
           else dt.datetime.now(dt.timezone.utc))
    go, delay = decide(now, a.attempted, a.event)
    s = slot(now.date())
    print(f"go={'true' if go else 'false'}")
    print(f"delay={delay}")
    print(f"slot={s // 60:02d}:{s % 60:02d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

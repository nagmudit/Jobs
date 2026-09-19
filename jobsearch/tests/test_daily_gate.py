"""The daily fetch's start-time gate (scripts/daily_gate.py).

The workflow triggers hourly; this decides which trigger fetches. The two
properties that matter are conduct properties, so they are tested:

- **at most one fetch attempt per UTC day**, so a failed run is never retried
  the same day and a manual run is not followed by a scheduled one;
- **a dropped or delayed trigger does not lose the day** -- the next one goes.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "daily_gate", Path(__file__).resolve().parents[2] / "scripts" / "daily_gate.py")
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)

UTC = dt.timezone.utc
DAY = dt.date(2026, 9, 20)


def at(minutes: int, day: dt.date = DAY) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, tzinfo=UTC) + dt.timedelta(
        minutes=minutes)


def test_slot_is_stable_within_a_day_and_in_the_window():
    s = G.slot(DAY)
    assert s == G.slot(DAY)
    assert 0 <= s < G.WINDOW_MINUTES


def test_slot_moves_between_days():
    slots = {G.slot(DAY + dt.timedelta(days=i)) for i in range(30)}
    assert len(slots) > 20                       # not a fixed time in disguise
    hours = {s // 60 for s in slots}
    assert len(hours) > 8                        # spread across the window


def test_before_the_slots_hour_nothing_runs():
    # A date whose slot is past the first hour, so an hour earlier exists.
    day = next(DAY + dt.timedelta(days=i) for i in range(60)
               if G.slot(DAY + dt.timedelta(days=i)) >= 60)
    s = G.slot(day)
    assert G.decide(at(s - 60, day), 0, "schedule") == (False, 0)


def test_trigger_in_the_slots_hour_waits_for_the_minute():
    s = G.slot(DAY)
    top = s - s % 60 + 5                          # a trigger at HH:05
    go, delay = G.decide(at(min(top, s)), 0, "schedule")
    assert go and delay == (s - min(top, s)) * 60


def test_a_late_trigger_goes_immediately():
    """GitHub delays scheduled runs by hours; the day must not be lost."""
    s = G.slot(DAY)
    assert G.decide(at(s + 180), 0, "schedule") == (True, 0)


def test_at_most_one_attempt_per_day():
    s = G.slot(DAY)
    assert G.decide(at(s + 60), 1, "schedule") == (False, 0)
    assert G.decide(at(s + 60), 1, "workflow_dispatch") == (False, 0)


def test_manual_dispatch_runs_at_any_time_if_nothing_ran_today():
    assert G.decide(at(0), 0, "workflow_dispatch") == (True, 0)

"""IST clock helpers used by risk window and strategy entry filters."""

from __future__ import annotations

import datetime as dt

from bot.risk.window import within_minutes_of_ist_time


def test_within_minutes_of_ist_time_at_open() -> None:
    # Friday 09:30 IST == 04:00 UTC
    now_utc = dt.datetime(2026, 5, 15, 4, 0, 0)
    assert within_minutes_of_ist_time(now_utc, dt.time(9, 30), minutes=30)


def test_within_minutes_of_ist_time_rejects_utc_mistake() -> None:
    # Naive 09:30 UTC is 15:00 IST — outside the 09:30 IST open window
    now_utc = dt.datetime(2026, 5, 15, 9, 30, 0)
    assert not within_minutes_of_ist_time(now_utc, dt.time(9, 30), minutes=30)

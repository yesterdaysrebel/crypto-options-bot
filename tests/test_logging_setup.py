"""Log timestamp formatting."""

from __future__ import annotations

import datetime as dt

from bot.observability.logging_setup import _ist_log_record_patcher
from bot.risk.window import IST


def test_ist_log_record_patcher_sets_ist_wall_clock(monkeypatch) -> None:
    fixed = dt.datetime(2026, 5, 15, 9, 30, 45, tzinfo=IST)

    class _FixedDatetime:
        @staticmethod
        def now(tz: dt.tzinfo | None = None) -> dt.datetime:
            assert tz is IST
            return fixed

    monkeypatch.setattr("bot.observability.logging_setup.dt.datetime", _FixedDatetime)
    record: dict = {"time": dt.datetime(2026, 1, 1)}
    _ist_log_record_patcher(record)
    assert record["time"] == fixed
    assert record["time"].tzinfo is IST

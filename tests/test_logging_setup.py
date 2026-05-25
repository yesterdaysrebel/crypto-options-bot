"""Log timestamp formatting."""

from __future__ import annotations

import datetime as dt

from bot.observability.logging_setup import _ist_log_record_patcher
from bot.risk.window import IST


def test_ist_log_record_patcher_converts_utc_to_ist() -> None:
    record: dict = {"time": dt.datetime(2026, 5, 25, 6, 9, 1, tzinfo=dt.timezone.utc)}
    _ist_log_record_patcher(record)
    assert record["time"].tzinfo is IST
    assert record["time"] == dt.datetime(2026, 5, 25, 11, 39, 1, tzinfo=IST)


def test_ist_log_record_patcher_preserves_naive_local_as_ist() -> None:
    record: dict = {"time": dt.datetime(2026, 5, 25, 17, 8, 45)}
    _ist_log_record_patcher(record)
    assert record["time"].tzinfo is IST
    assert record["time"].replace(tzinfo=None) == dt.datetime(2026, 5, 25, 17, 8, 45)

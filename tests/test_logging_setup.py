"""Log timestamp formatting."""

from __future__ import annotations

import datetime as dt

from bot.observability.logging_setup import _ist_log_record_patcher
from bot.risk.window import IST


def test_ist_log_record_patcher_sets_ist_wall_clock() -> None:
    before = dt.datetime.now(IST)
    record: dict = {"time": dt.datetime(2026, 1, 1, tzinfo=IST)}
    _ist_log_record_patcher(record)
    after = dt.datetime.now(IST)
    assert record["time"].tzinfo is IST
    assert before <= record["time"] <= after

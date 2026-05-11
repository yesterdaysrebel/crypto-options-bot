"""Trading-window check (in IST)."""

from __future__ import annotations

import datetime as dt

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def utc_to_ist(now: dt.datetime) -> dt.datetime:
    """Convert a naive UTC datetime (the convention used in storage/models) to IST."""
    return now.replace(tzinfo=dt.UTC).astimezone(IST)


class TradingWindow:
    """Allow trading between `start` and `end` IST (inclusive of `start`, exclusive of `end`)."""

    def __init__(self, start: dt.time, end: dt.time, force_close: dt.time) -> None:
        self._start = start
        self._end = end
        self._force_close = force_close

    @property
    def start(self) -> dt.time:
        return self._start

    @property
    def end(self) -> dt.time:
        return self._end

    @property
    def force_close(self) -> dt.time:
        return self._force_close

    def is_open(self, now_utc: dt.datetime) -> bool:
        local = utc_to_ist(now_utc).time()
        return self._start <= local < self._end

    def is_force_close_time(self, now_utc: dt.datetime) -> bool:
        local = utc_to_ist(now_utc).time()
        return local >= self._force_close

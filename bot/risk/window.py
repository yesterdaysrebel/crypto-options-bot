"""Trading-window check (in IST)."""

from __future__ import annotations

import datetime as dt

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def utc_to_ist(now: dt.datetime) -> dt.datetime:
    """Convert a naive UTC datetime (the convention used in storage/models) to IST."""
    return now.replace(tzinfo=dt.UTC).astimezone(IST)


def india_options_session_close_utc(now_utc: dt.datetime) -> dt.datetime:
    """Next India cash-session options close (17:30 IST) as naive UTC."""
    local = utc_to_ist(now_utc)
    close_local = local.replace(hour=17, minute=30, second=0, microsecond=0)
    if local.time() >= dt.time(17, 30):
        close_local += dt.timedelta(days=1)
    return close_local.astimezone(dt.UTC).replace(tzinfo=None)


def within_minutes_of_ist_time(now_utc: dt.datetime, target_ist: dt.time, *, minutes: int) -> bool:
    """True when `now_utc` falls within +/- `minutes` of `target_ist` on the IST clock."""
    local_time = utc_to_ist(now_utc).time().replace(microsecond=0)
    return _within_minutes_of_clock_time(local_time, target_ist, minutes=minutes)


def _within_minutes_of_clock_time(a: dt.time, b: dt.time, *, minutes: int) -> bool:
    anchor = dt.date(2000, 1, 1)
    delta = abs((dt.datetime.combine(anchor, a) - dt.datetime.combine(anchor, b)).total_seconds())
    return delta <= minutes * 60


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

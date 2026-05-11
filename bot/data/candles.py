"""Tick → OHLCV candle aggregator with technical indicators (EMA, ATR, BBwidth).

Strategies expect:
  - 15m candles (directional + strangle short tf)
  - 1h candles  (strangle long tf)
The aggregator is timeframe-agnostic; one instance per (symbol, timeframe).

We bucket ticks by floor(ts / period_seconds). When a tick arrives in a new bucket, the
prior bucket is finalised and emitted via the `on_close` callback (or pulled via `pop_closed()`).
"""

from __future__ import annotations

import datetime as dt
import math
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import numpy as np

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


@dataclass
class Candle:
    ts: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    n_ticks: int = 0

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def hl2(self) -> float:
        return (self.high + self.low) / 2.0


@dataclass
class CandleAggregator:
    """Bucket ticks into fixed-period OHLCV candles.

    Args:
        timeframe: one of TIMEFRAME_SECONDS keys.
        on_close: optional callback fired with the just-closed Candle.
        history: max # of finalised candles to retain in `closed`.
    """

    timeframe: str
    on_close: Callable[[Candle], None] | None = None
    history: int = 256

    closed: deque[Candle] = field(default_factory=deque, init=False)
    current: Candle | None = field(default=None, init=False)
    _period_s: int = field(init=False)
    _current_bucket: int = field(default=-1, init=False)

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAME_SECONDS:
            raise ValueError(f"unknown timeframe {self.timeframe!r}")
        self._period_s = TIMEFRAME_SECONDS[self.timeframe]
        self.closed = deque(maxlen=self.history)

    def add_tick(self, ts: dt.datetime, price: float, volume: float = 0.0) -> None:
        epoch = int(ts.timestamp())
        bucket = epoch // self._period_s
        if self.current is None:
            self._open_new(bucket, ts, price, volume)
            return
        if bucket == self._current_bucket:
            self.current.high = max(self.current.high, price)
            self.current.low = min(self.current.low, price)
            self.current.close = price
            self.current.volume += volume
            self.current.n_ticks += 1
            return
        self._close_current()
        for missing_bucket in range(self._current_bucket + 1, bucket):
            filler_ts = dt.datetime.fromtimestamp(missing_bucket * self._period_s, tz=dt.UTC).replace(
                tzinfo=None
            )
            self._open_new(missing_bucket, filler_ts, self.closed[-1].close, 0.0)
            self._close_current()
        self._open_new(bucket, ts, price, volume)

    def _open_new(self, bucket: int, ts: dt.datetime, price: float, volume: float) -> None:
        floor_ts = dt.datetime.fromtimestamp(bucket * self._period_s, tz=dt.UTC).replace(tzinfo=None)
        self.current = Candle(
            ts=floor_ts,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
            n_ticks=1,
        )
        self._current_bucket = bucket

    def _close_current(self) -> None:
        if self.current is None:
            return
        self.closed.append(self.current)
        if self.on_close is not None:
            self.on_close(self.current)
        self.current = None

    def force_close(self) -> Candle | None:
        if self.current is None:
            return None
        closed = self.current
        self._close_current()
        return closed

    def pop_closed(self) -> list[Candle]:
        out = list(self.closed)
        self.closed.clear()
        return out

    def n_closed(self) -> int:
        return len(self.closed)

    def closes(self) -> np.ndarray:
        return np.array([c.close for c in self.closed], dtype=float)

    def highs(self) -> np.ndarray:
        return np.array([c.high for c in self.closed], dtype=float)

    def lows(self) -> np.ndarray:
        return np.array([c.low for c in self.closed], dtype=float)


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average. EMA[0] = values[0]; alpha = 2 / (period + 1)."""
    if period <= 0:
        raise ValueError(f"ema period must be > 0, got {period}")
    out = np.empty_like(values, dtype=float)
    if len(values) == 0:
        return out
    alpha = 2.0 / (period + 1.0)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Average True Range using Wilder's smoothing (RMA). Returns NaN until `period` bars."""
    if period <= 0:
        raise ValueError(f"atr period must be > 0, got {period}")
    n = len(closes)
    if n == 0:
        return np.array([])
    tr = np.empty(n, dtype=float)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    out = np.full(n, np.nan, dtype=float)
    if n < period:
        return out
    out[period - 1] = tr[:period].mean()
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def bollinger_width(values: np.ndarray, period: int = 20, std: float = 2.0) -> np.ndarray:
    """Width = (upper - lower) / mid, where bands are mid ± std*sigma."""
    if period <= 0:
        raise ValueError(f"bollinger period must be > 0, got {period}")
    n = len(values)
    if n < period:
        return np.full(n, np.nan, dtype=float)
    out = np.full(n, np.nan, dtype=float)
    for i in range(period - 1, n):
        window = values[i - period + 1 : i + 1]
        mid = window.mean()
        sigma = window.std(ddof=0)
        if math.isclose(mid, 0.0) or math.isnan(sigma):
            out[i] = math.nan
        else:
            out[i] = 2.0 * std * sigma / mid
    return out


def percentile_rank(value: float, history: Iterable[float]) -> float:
    """Return the mid-rank percentile of `value` in `history` (0.0 = lowest).

    midrank = (count_strictly_less + 0.5 * count_equal) / total
    This gives 0.5 when value equals the median; the minimum gets ~0 (or 0.5/N for unique).
    """
    hist = np.array(list(history), dtype=float)
    if len(hist) == 0:
        return math.nan
    less = int(np.sum(hist < value))
    equal = int(np.sum(hist == value))
    return (less + 0.5 * equal) / float(len(hist))

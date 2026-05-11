"""Tests for the candle aggregator + indicators.

AC for PR #7: given a synthetic tick stream covering 60min spanning a 15m boundary,
aggregator emits exactly 4 candles with OHLC matching the reference values.
"""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest
from bot.data.candles import (
    Candle,
    CandleAggregator,
    atr,
    bollinger_width,
    ema,
    percentile_rank,
)


def _ts(epoch: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch, tz=dt.UTC).replace(tzinfo=None)


def test_60min_stream_emits_four_15m_candles() -> None:
    base = 1_700_000_000  # bucket-aligned to 15m (since 1700000000 % 900 == 800; let's align)
    base = base - (base % 900)
    agg = CandleAggregator(timeframe="15m")
    prices: list[tuple[int, float]] = []
    for i in range(60):
        ts_epoch = base + i * 60
        prices.append((ts_epoch, 100.0 + (i % 15) * 0.1))
    prices.append((base + 60 * 60, 200.0))
    for epoch, p in prices:
        agg.add_tick(_ts(epoch), p)
    assert agg.n_closed() == 4, f"expected 4 closed candles, got {agg.n_closed()}"
    closed = list(agg.closed)
    assert closed[0].open == 100.0
    assert closed[0].close == pytest.approx(101.4, abs=1e-9)
    assert closed[0].high == pytest.approx(101.4, abs=1e-9)
    assert closed[0].low == 100.0
    bucket_seconds = [c.ts.timestamp() for c in closed]
    assert all((bucket_seconds[i + 1] - bucket_seconds[i]) == 900 for i in range(3))


def test_gap_fills_missing_buckets_with_flat_candle() -> None:
    base = 1_700_000_000
    base = base - (base % 900)
    agg = CandleAggregator(timeframe="15m")
    agg.add_tick(_ts(base), 100.0)
    agg.add_tick(_ts(base + 30), 101.0)
    agg.add_tick(_ts(base + 3 * 900 + 60), 105.0)
    assert agg.n_closed() == 3
    filler_a = list(agg.closed)[1]
    filler_b = list(agg.closed)[2]
    assert filler_a.open == filler_a.close == 101.0
    assert filler_a.n_ticks == 1
    assert filler_b.open == filler_b.close == 101.0


def test_force_close_finalises_current() -> None:
    base = 1_700_000_000
    base = base - (base % 900)
    agg = CandleAggregator(timeframe="15m")
    agg.add_tick(_ts(base), 50.0)
    agg.add_tick(_ts(base + 60), 51.0)
    closed = agg.force_close()
    assert closed is not None
    assert closed.high == 51.0
    assert closed.low == 50.0
    assert agg.current is None


def test_on_close_callback_receives_candle() -> None:
    base = 1_700_000_000
    base = base - (base % 900)
    received: list[Candle] = []
    agg = CandleAggregator(timeframe="15m", on_close=received.append)
    agg.add_tick(_ts(base), 100.0)
    agg.add_tick(_ts(base + 900), 110.0)
    assert len(received) == 1
    assert received[0].close == 100.0


def test_ema_matches_reference() -> None:
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    out = ema(values, period=2)
    assert out[0] == 1.0
    alpha = 2.0 / 3.0
    expected_1 = alpha * 2.0 + (1 - alpha) * 1.0
    assert out[1] == pytest.approx(expected_1)


def test_atr_uses_wilder_smoothing() -> None:
    highs = np.array([10, 11, 12, 13, 14, 15, 16], dtype=float)
    lows = np.array([8, 9, 10, 11, 12, 13, 14], dtype=float)
    closes = np.array([9, 10, 11, 12, 13, 14, 15], dtype=float)
    out = atr(highs, lows, closes, period=3)
    assert math.isnan(out[1])
    assert not math.isnan(out[2])
    assert math.isclose(out[2], 2.0, abs_tol=1e-9), f"first ATR={out[2]}"


def test_bollinger_width_basic() -> None:
    values = np.array([100.0] * 30)
    out = bollinger_width(values, period=20, std=2.0)
    assert math.isclose(out[-1], 0.0, abs_tol=1e-9)
    values2 = np.array([100.0 if i % 2 == 0 else 102.0 for i in range(30)])
    out2 = bollinger_width(values2, period=20, std=2.0)
    assert out2[-1] > 0.0


def test_percentile_rank_basic() -> None:
    history = list(range(100))
    assert percentile_rank(50.0, history) == pytest.approx(0.51, abs=0.01)
    assert percentile_rank(0.0, history) == pytest.approx(0.01, abs=0.01)
    assert percentile_rank(99.0, history) == pytest.approx(1.0, abs=0.01)


def test_invalid_timeframe_raises() -> None:
    with pytest.raises(ValueError, match="unknown timeframe"):
        CandleAggregator(timeframe="7m")


def test_invalid_periods_raise() -> None:
    with pytest.raises(ValueError):
        ema(np.array([1.0]), period=0)
    with pytest.raises(ValueError):
        atr(np.array([1.0]), np.array([1.0]), np.array([1.0]), period=-1)
    with pytest.raises(ValueError):
        bollinger_width(np.array([1.0]), period=0)

"""Tests for directional optimization helpers."""

from __future__ import annotations

import datetime as dt

from bot.analytics.directional_optimize import (
    FilledTrade,
    _premium_dd_from_candles,
    _trade_id_from_coid,
    analyze_trade_path,
)


def test_trade_id_from_coid() -> None:
    assert _trade_id_from_coid("dr-1023-0-entrym-abc") == 1023
    assert _trade_id_from_coid("directional-1-0-entry") is None


def test_premium_dd_detects_50pct_drop() -> None:
    t0 = int(dt.datetime(2026, 5, 27, 8, 0).timestamp())
    candles = []
    for i, close in enumerate([100.0, 95.0, 40.0, 45.0]):
        candles.append({"time": t0 + i * 900, "open": close, "high": close, "low": close, "close": close})
    dd, bar = _premium_dd_from_candles(candles, 0, dd_threshold=0.50)
    assert dd is not None and dd >= 50.0
    assert bar == 2


def test_analyze_trade_path_loss_on_adverse_spot() -> None:
    entry = dt.datetime(2026, 5, 14, 10, 0)
    t0 = int(entry.timestamp())
    u_candles = []
    for i in range(8):
        close = 100.0 - i * 8.0
        u_candles.append(
            {
                "time": t0 + i * 900,
                "open": close,
                "high": close + 1,
                "low": close - 2,
                "close": close,
            }
        )
    o_candles = [
        {"time": t0 + i * 900, "open": 10 - i, "high": 10, "low": 10 - i, "close": 10 - i * 0.6}
        for i in range(8)
    ]
    trade = FilledTrade(
        trade_id=1,
        entry_ts=entry,
        exit_ts=entry + dt.timedelta(hours=2),
        underlying="BTC",
        mode="dry",
        status="closed",
        symbol="C-BTC-100-280526",
        option_type="call",
        strike=100.0,
        lots=1,
        entry_premium=320.0,
        exit_premium=160.0,
        realised_pnl_inr=-160.0,
        r_multiple=-0.5,
        exit_reason="premium_stop",
        spot_at_entry=100.0,
        atr_at_entry=5.0,
        ema_sep=2.0,
        threshold=1.0,
        source="db",
    )
    row = analyze_trade_path(trade, u_candles, o_candles)
    assert row is not None
    assert row.spot_adverse_15m is True
    assert row.win is False

"""Tests for directional post-mortem helpers."""

from __future__ import annotations

import datetime as dt

from bot.analytics.directional_postmortem import (
    TradeAttempt,
    analyze_movement,
    dedupe_attempts,
)


def test_dedupe_collapses_retry_storm() -> None:
    base = dt.datetime(2026, 5, 27, 8, 30, 0)
    attempts = [
        TradeAttempt(
            trade_id=i,
            entry_ts=base.replace(second=i % 60),
            underlying="BTC",
            status="errored",
            mode="live",
            error="partial_fill_rolled_back",
            intended_symbol="C-BTC-76000-280526",
            intended_strike=76000.0,
            intended_premium_inr=45.0,
            option_type="call",
            spot_at_signal=76000.0,
            ema_sep=100.0,
            atr=200.0,
        )
        for i in range(1000, 1010)
    ]
    deduped = dedupe_attempts(attempts)
    assert len(deduped) == 1
    assert deduped[0].trade_id == 1000


def test_analyze_movement_call_adverse_on_drop() -> None:
    entry = dt.datetime(2026, 5, 27, 8, 30, 0)
    t0 = int(entry.timestamp())
    candles = []
    price = 100.0
    for i in range(10):
        close = price - i * 2.0
        candles.append(
            {
                "time": t0 + i * 900,
                "open": close,
                "high": close + 1,
                "low": close - 1,
                "close": close,
                "volume": 1,
            }
        )
    attempt = TradeAttempt(
        trade_id=1,
        entry_ts=entry,
        underlying="BTC",
        status="errored",
        mode="live",
        error=None,
        intended_symbol="C-BTC-100-280526",
        intended_strike=100.0,
        intended_premium_inr=10.0,
        option_type="call",
        spot_at_signal=100.0,
        ema_sep=1.0,
        atr=2.0,
    )
    row = analyze_movement(attempt, candles)
    assert row is not None
    assert row.verdict_60m == "adverse"
    assert row.ret_pct_at_bar[4] is not None
    assert row.ret_pct_at_bar[4] < 0

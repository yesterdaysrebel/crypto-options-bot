"""Delta PnL attribution on trade close."""

from __future__ import annotations

from bot.desk.pnl_attribution import estimate_delta_pnl_inr
from bot.storage.models import Leg, Trade


def test_estimate_delta_pnl_inr_long_call() -> None:
    trade = Trade(
        id=1,
        strategy_id="directional",
        underlying="BTC",
        lots=2,
        notes={
            "entry_greeks": {
                "C-BTC-100000-180526": {"delta": 0.5},
            },
        },
    )
    legs = [
        Leg(
            trade_id=1,
            leg_idx=0,
            symbol="C-BTC-100000-180526",
            side="buy",
            option_type="call",
            lots=2,
        ),
    ]
    pnl = estimate_delta_pnl_inr(
        trade,
        legs,
        entry_underlying_price=100_000.0,
        exit_underlying_price=101_000.0,
        usd_inr_rate=85.0,
        chain=None,
    )
    # 0.5 * 1000 * 1 * 2 * 1 * 85 = 85_000
    assert pnl is not None
    assert abs(pnl - 85_000.0) < 1e-6


def test_estimate_delta_pnl_returns_none_without_greeks() -> None:
    trade = Trade(id=1, strategy_id="directional", underlying="BTC", lots=1, notes={})
    legs = [
        Leg(
            trade_id=1,
            leg_idx=0,
            symbol="C-BTC-100000-180526",
            side="buy",
            option_type="call",
        ),
    ]
    assert (
        estimate_delta_pnl_inr(
            trade,
            legs,
            entry_underlying_price=100_000.0,
            exit_underlying_price=101_000.0,
            usd_inr_rate=85.0,
        )
        is None
    )

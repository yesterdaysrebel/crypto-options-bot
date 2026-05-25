"""Tests for CreditVerticalStrategy — trend-aligned 2-leg credit spread."""

from __future__ import annotations

import datetime as dt

from bot.config.models import StrategyId, Underlying
from bot.strategies.credit_vertical import CreditVerticalStrategy

from tests.strategy_fixtures import (
    credit_vertical_cfg,
    make_breakout_candles,
    make_chain,
    make_market_state,
    set_quote_open_interest,
)


def _ranges_of_strikes() -> list[int]:
    return list(range(85000, 115001, 500))


def test_uptrend_emits_bull_put_spread() -> None:
    now = dt.datetime(2026, 5, 12, 10, 0, 0)
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=1),
        strikes=_ranges_of_strikes(),
        spot=spot,
        atm_mid=800.0,
        decay_per_pct=120.0,
    )
    candles = make_breakout_candles(
        consolidation_n=35,
        breakout_n=5,
        consolidation_price=spot - 1000,
        breakout_step=300,
        consolidation_range=100,
        breakout_range=200,
    )
    state = make_market_state(
        now,
        chain=chain,
        candles_by_tf={Underlying.BTC: {"15m": candles}},
        spots={Underlying.BTC: spot},
    )
    strat = CreditVerticalStrategy(
        credit_vertical_cfg(credit={"min_credit_pct_of_width": 0.02}),
    )
    intents, decisions = strat.evaluate(state)
    assert len(intents) == 1, f"reasons={[d['reason'] for d in decisions]}"
    intent = intents[0]
    assert intent.strategy_id == StrategyId.CREDIT_VERTICAL
    assert len(intent.legs) == 2
    assert intent.legs[0].option_type == "put"
    assert intent.legs[0].side == "buy"
    assert intent.legs[1].side == "sell"
    assert intent.legs[0].strike < intent.legs[1].strike
    assert intent.target_credit_inr is not None
    assert intent.target_max_loss_inr is not None


def test_flat_market_does_not_fire() -> None:
    from tests.strategy_fixtures import make_flat_candles

    now = dt.datetime(2026, 5, 12, 10, 0, 0)
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=1),
        strikes=_ranges_of_strikes(),
        spot=spot,
    )
    candles = make_flat_candles(n=50, price=spot, base_range=20)
    state = make_market_state(
        now,
        chain=chain,
        candles_by_tf={Underlying.BTC: {"15m": candles}},
        spots={Underlying.BTC: spot},
    )
    strat = CreditVerticalStrategy(credit_vertical_cfg())
    intents, _ = strat.evaluate(state)
    assert intents == []


def test_low_oi_blocks_entry() -> None:
    now = dt.datetime(2026, 5, 12, 10, 0, 0)
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=1),
        strikes=_ranges_of_strikes(),
        spot=spot,
    )
    candles = make_breakout_candles(
        consolidation_n=35,
        breakout_n=5,
        consolidation_price=spot - 1000,
        breakout_step=300,
        consolidation_range=100,
        breakout_range=200,
    )
    for row in chain.all_instruments():
        set_quote_open_interest(chain, row.symbol, 0.0)
    state = make_market_state(
        now,
        chain=chain,
        candles_by_tf={Underlying.BTC: {"15m": candles}},
        spots={Underlying.BTC: spot},
    )
    strat = CreditVerticalStrategy(credit_vertical_cfg(desk={"min_open_interest": 50}))
    intents, decisions = strat.evaluate(state)
    assert intents == []
    assert any(d.get("reason") == "low_open_interest" for d in decisions)

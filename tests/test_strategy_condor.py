"""Tests for IronCondorStrategy. AC: Friday 09:30 with healthy chain → 1 four-leg intent;
non-Friday → 0 intents; credit too thin → reason='credit_too_thin'."""

from __future__ import annotations

import datetime as dt

from bot.config.models import StrategyId, Underlying
from bot.strategies.iron_condor import IronCondorStrategy

from tests.strategy_fixtures import condor_cfg, make_chain, make_market_state


def _friday_open() -> dt.datetime:
    return dt.datetime(2026, 5, 15, 9, 30, 0)


def _strikes() -> list[int]:
    return list(range(85000, 115001, 500))


def test_friday_open_with_healthy_chain_emits_4leg_intent() -> None:
    now = _friday_open()
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=7),
        strikes=_strikes(),
        spot=spot,
        atm_mid=1000.0,
        decay_per_pct=150.0,
    )
    state = make_market_state(
        now,
        chain=chain,
        candles_by_tf={},
        spots={Underlying.BTC: spot},
    )
    strat = IronCondorStrategy(condor_cfg())
    intents, decisions = strat.evaluate(state)
    assert len(intents) == 1
    intent = intents[0]
    assert intent.strategy_id == StrategyId.IRON_CONDOR
    assert len(intent.legs) == 4
    sides = [leg.side for leg in intent.legs]
    types = [leg.option_type for leg in intent.legs]
    assert sides == ["buy", "sell", "sell", "buy"]
    assert types == ["put", "put", "call", "call"]
    strikes = [leg.strike for leg in intent.legs]
    assert strikes[0] < strikes[1] < strikes[2] < strikes[3]
    assert intent.target_credit_inr is not None
    assert intent.target_max_loss_inr is not None
    decision = next(d for d in decisions if d["passed"])
    assert "credit" in decision["feature_vector"]


def test_non_friday_does_not_fire() -> None:
    now = dt.datetime(2026, 5, 14, 9, 30, 0)  # Thursday
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=8),
        strikes=_strikes(),
        spot=spot,
    )
    state = make_market_state(now, chain=chain, candles_by_tf={}, spots={Underlying.BTC: spot})
    strat = IronCondorStrategy(condor_cfg())
    intents, decisions = strat.evaluate(state)
    assert intents == []
    assert all(d["reason"] == "filter_failed" for d in decisions)


def test_outside_open_window_does_not_fire() -> None:
    now = _friday_open().replace(hour=15)  # 3 PM Friday
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=7),
        strikes=_strikes(),
        spot=spot,
    )
    state = make_market_state(now, chain=chain, candles_by_tf={}, spots={Underlying.BTC: spot})
    strat = IronCondorStrategy(condor_cfg())
    intents, _ = strat.evaluate(state)
    assert intents == []


def test_credit_too_thin_rejected() -> None:
    now = _friday_open()
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=7),
        strikes=_strikes(),
        spot=spot,
        atm_mid=20.0,  # very thin credit
        decay_per_pct=2.0,
    )
    state = make_market_state(now, chain=chain, candles_by_tf={}, spots={Underlying.BTC: spot})
    strat = IronCondorStrategy(condor_cfg())
    intents, decisions = strat.evaluate(state)
    assert intents == []
    assert any(d["reason"] == "credit_too_thin" for d in decisions)

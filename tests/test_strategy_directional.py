"""Tests for DirectionalStrategy. AC: 4-bar high + 0.25*ATR + EMA9>EMA21 produces 1 long-call intent."""

from __future__ import annotations

import datetime as dt

from bot.config.models import ExpiryBucket, StrategyId, Underlying
from bot.desk.iv_history import IvPercentileResult
from bot.strategies.directional import DirectionalStrategy

from tests.strategy_fixtures import (
    directional_cfg,
    make_breakout_candles,
    make_chain,
    make_flat_candles,
    make_market_state,
)


def _spot_today() -> tuple[dt.datetime, float]:
    # 09:30 IST == 04:00 UTC; same-day close 17:30 IST == 12:00 UTC
    return dt.datetime(2026, 5, 12, 4, 0, 0), 100000.0


def _ranges_of_strikes() -> list[int]:
    return list(range(88000, 112001, 500))


def test_breakout_long_emits_intent() -> None:
    now, spot = _spot_today()
    expiry_today = dt.datetime(2026, 5, 12, 12, 0, 0)
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=expiry_today,
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
    state = make_market_state(
        now,
        chain=chain,
        candles_by_tf={Underlying.BTC: {"15m": candles}},
        spots={Underlying.BTC: spot},
    )
    strat = DirectionalStrategy(directional_cfg())
    intents, decisions = strat.evaluate(state)
    assert len(intents) == 1, f"expected 1 intent, got {len(intents)}: {[d['reason'] for d in decisions]}"
    intent = intents[0]
    assert intent.strategy_id == StrategyId.DIRECTIONAL
    assert intent.underlying == Underlying.BTC
    assert len(intent.legs) == 1
    leg = intent.legs[0]
    assert leg.side == "buy"
    assert leg.option_type == "call"


def test_flat_market_does_not_fire() -> None:
    now, spot = _spot_today()
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=1),
        strikes=_ranges_of_strikes(),
        spot=spot,
    )
    candles = make_flat_candles(n=40, price=spot, base_range=100)
    state = make_market_state(
        now,
        chain=chain,
        candles_by_tf={Underlying.BTC: {"15m": candles}},
        spots={Underlying.BTC: spot},
    )
    strat = DirectionalStrategy(directional_cfg())
    intents, decisions = strat.evaluate(state)
    assert intents == []
    assert all(not d["passed"] for d in decisions)
    assert all(d["reason"] == "filter_failed" for d in decisions)


def test_short_setup_emits_put_intent() -> None:
    now, spot = _spot_today()
    expiry_today = dt.datetime(2026, 5, 12, 12, 0, 0)
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=expiry_today,
        strikes=_ranges_of_strikes(),
        spot=spot,
    )
    candles = make_breakout_candles(
        consolidation_n=35,
        breakout_n=5,
        consolidation_price=spot + 1000,
        breakout_step=-300,
        consolidation_range=100,
        breakout_range=200,
    )
    state = make_market_state(
        now,
        chain=chain,
        candles_by_tf={Underlying.BTC: {"15m": candles}},
        spots={Underlying.BTC: spot},
    )
    strat = DirectionalStrategy(directional_cfg())
    intents, _ = strat.evaluate(state)
    assert len(intents) == 1
    assert intents[0].legs[0].option_type == "put"


def test_insufficient_history_does_not_fire() -> None:
    now, spot = _spot_today()
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=1),
        strikes=_ranges_of_strikes(),
        spot=spot,
    )
    candles = make_flat_candles(n=5, price=spot, base_range=100)
    state = make_market_state(
        now,
        chain=chain,
        candles_by_tf={Underlying.BTC: {"15m": candles}},
        spots={Underlying.BTC: spot},
    )
    strat = DirectionalStrategy(directional_cfg())
    intents, decisions = strat.evaluate(state)
    assert intents == []
    assert all(d["reason"] == "insufficient_history" for d in decisions)


def test_iv_out_of_range_blocks_entry() -> None:
    now, spot = _spot_today()
    expiry_today = dt.datetime(2026, 5, 12, 12, 0, 0)
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=expiry_today,
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
    iv_pct = {
        (Underlying.BTC, ExpiryBucket.D1): IvPercentileResult(0.90, "ok", 100),
    }
    state = make_market_state(
        now,
        chain=chain,
        candles_by_tf={Underlying.BTC: {"15m": candles}},
        spots={Underlying.BTC: spot},
        iv_percentiles=iv_pct,
    )
    cfg = directional_cfg(
        desk={"max_iv_percentile_long": 0.70, "min_iv_percentile_long": None},
    )
    strat = DirectionalStrategy(cfg)
    intents, decisions = strat.evaluate(state)
    assert intents == []
    assert decisions[0]["reason"] == "iv_out_of_range"
    assert decisions[0]["feature_vector"]["iv_percentile"] == 0.90


def test_low_iv_percentile_passes_with_breakout() -> None:
    now, spot = _spot_today()
    expiry_today = dt.datetime(2026, 5, 12, 12, 0, 0)
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=expiry_today,
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
    iv_pct = {
        (Underlying.BTC, ExpiryBucket.D1): IvPercentileResult(0.35, "ok", 100),
    }
    state = make_market_state(
        now,
        chain=chain,
        candles_by_tf={Underlying.BTC: {"15m": candles}},
        spots={Underlying.BTC: spot},
        iv_percentiles=iv_pct,
    )
    cfg = directional_cfg(desk={"max_iv_percentile_long": 0.70})
    strat = DirectionalStrategy(cfg)
    intents, _ = strat.evaluate(state)
    assert len(intents) == 1
    assert intents[0].feature_vector.get("leg_delta") is not None


def test_prefer_delta_strike_selects_otm() -> None:
    now, spot = _spot_today()
    expiry_today = dt.datetime(2026, 5, 12, 12, 0, 0)
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=expiry_today,
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
    state = make_market_state(
        now,
        chain=chain,
        candles_by_tf={Underlying.BTC: {"15m": candles}},
        spots={Underlying.BTC: spot},
    )
    cfg = directional_cfg(desk={"prefer_delta_strike": 0.35})
    strat = DirectionalStrategy(cfg)
    intents, _ = strat.evaluate(state)
    assert len(intents) == 1
    leg = intents[0].legs[0]
    assert leg.strike != spot

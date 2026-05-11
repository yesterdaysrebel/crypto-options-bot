"""Tests for VolStrangleStrategy. AC: low-ATR + tight 1h BBwidth + compressed 15m range →
1 two-leg intent; trending market → 0 intents."""

from __future__ import annotations

import datetime as dt

from bot.config.models import StrategyId, Underlying
from bot.strategies.vol_strangle import VolStrangleStrategy

from tests.strategy_fixtures import (
    make_chain,
    make_flat_candles,
    make_market_state,
    make_noisy_then_quiet_candles,
    make_trend_candles,
    strangle_cfg,
)


def _strikes() -> list[int]:
    return list(range(85000, 115001, 500))


def test_quiet_market_fires_2leg_long_strangle() -> None:
    now = dt.datetime(2026, 5, 12, 10, 0, 0)
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=1, hours=8),  # D2 bucket
        strikes=_strikes(),
        spot=spot,
    )
    candles_15m = make_noisy_then_quiet_candles(
        noisy_n=1450,
        quiet_n=10,
        price=spot,
        noisy_amplitude=400,
        quiet_amplitude=20,
    )
    candles_1h = make_noisy_then_quiet_candles(
        noisy_n=300,
        quiet_n=24,
        price=spot,
        noisy_amplitude=2000,
        quiet_amplitude=80,
        bucket_seconds=3600,
    )
    candles = {Underlying.BTC: {"15m": candles_15m, "1h": candles_1h}}
    state = make_market_state(now, chain=chain, candles_by_tf=candles, spots={Underlying.BTC: spot})
    strat = VolStrangleStrategy(strangle_cfg())
    intents, decisions = strat.evaluate(state)
    assert len(intents) == 1, f"expected 1 intent, got reasons={[d['reason'] for d in decisions]}"
    intent = intents[0]
    assert intent.strategy_id == StrategyId.VOL_STRANGLE
    assert len(intent.legs) == 2
    sides = [leg.side for leg in intent.legs]
    types = sorted(leg.option_type for leg in intent.legs)
    assert sides == ["buy", "buy"]
    assert types == ["call", "put"]


def test_trending_market_does_not_fire() -> None:
    now = dt.datetime(2026, 5, 12, 10, 0, 0)
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=2),
        strikes=_strikes(),
        spot=spot,
    )
    trending_15m = make_trend_candles(n=200, start_price=spot - 10_000, step=50, base_range=500)
    trending_1h = make_trend_candles(
        n=200, start_price=spot - 10_000, step=200, base_range=2000, bucket_seconds=3600
    )
    candles = {Underlying.BTC: {"15m": trending_15m, "1h": trending_1h}}
    state = make_market_state(now, chain=chain, candles_by_tf=candles, spots={Underlying.BTC: spot})
    strat = VolStrangleStrategy(strangle_cfg())
    intents, _ = strat.evaluate(state)
    assert intents == []


def test_insufficient_history_does_not_fire() -> None:
    now = dt.datetime(2026, 5, 12, 10, 0, 0)
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=2),
        strikes=_strikes(),
        spot=spot,
    )
    short_15m = make_flat_candles(n=10, price=spot, base_range=50)
    short_1h = make_flat_candles(n=10, price=spot, base_range=200, bucket_seconds=3600)
    candles = {Underlying.BTC: {"15m": short_15m, "1h": short_1h}}
    state = make_market_state(now, chain=chain, candles_by_tf=candles, spots={Underlying.BTC: spot})
    strat = VolStrangleStrategy(strangle_cfg())
    intents, decisions = strat.evaluate(state)
    assert intents == []
    assert all(d["reason"] == "insufficient_history" for d in decisions)


def test_cooldown_blocks_entry() -> None:
    now = dt.datetime(2026, 5, 12, 10, 0, 0)
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=1, hours=8),
        strikes=_strikes(),
        spot=spot,
    )
    candles_15m = make_noisy_then_quiet_candles(
        noisy_n=1450,
        quiet_n=10,
        price=spot,
        noisy_amplitude=400,
        quiet_amplitude=20,
    )
    candles_1h = make_noisy_then_quiet_candles(
        noisy_n=300,
        quiet_n=24,
        price=spot,
        noisy_amplitude=2000,
        quiet_amplitude=80,
        bucket_seconds=3600,
    )
    candles = {Underlying.BTC: {"15m": candles_15m, "1h": candles_1h}}
    state = make_market_state(now, chain=chain, candles_by_tf=candles, spots={Underlying.BTC: spot})
    strat = VolStrangleStrategy(strangle_cfg())
    strat.context.cooldown_until = now + dt.timedelta(hours=12)
    intents, decisions = strat.evaluate(state)
    assert intents == []
    assert any(d["reason"] == "anti_revenge_block" for d in decisions)

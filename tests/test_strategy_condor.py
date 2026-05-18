"""Tests for IronCondorStrategy. AC: Friday 09:30 with healthy chain → 1 four-leg intent;
non-Friday → 0 intents; credit too thin → reason='credit_too_thin'."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

from bot.config.models import StrategyId, Underlying
from bot.strategies.iron_condor import IronCondorStrategy

from tests.strategy_fixtures import condor_cfg, make_chain, make_market_state, set_quote_open_interest


def _friday_open_ist_as_utc() -> dt.datetime:
    """Friday 09:30 IST == 04:00 UTC (naive UTC convention used by the engine)."""
    return dt.datetime(2026, 5, 15, 4, 0, 0)


def _strikes() -> list[int]:
    return list(range(85000, 115001, 500))


def test_friday_open_with_healthy_chain_emits_4leg_intent() -> None:
    now = _friday_open_ist_as_utc()
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
    # Thursday 09:30 IST == Thursday 04:00 UTC
    now = dt.datetime(2026, 5, 14, 4, 0, 0)
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


def test_utc_clock_time_matching_open_ist_does_not_fire() -> None:
    """Regression: comparing UTC clock to IST open_time used to open at 15:00 IST."""
    now = dt.datetime(2026, 5, 15, 9, 30, 0)  # 15:00 IST Friday
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


def test_outside_open_window_does_not_fire() -> None:
    # Friday 15:00 IST == 09:30 UTC (outside 09:30 +/- 30m IST window)
    now = dt.datetime(2026, 5, 15, 9, 30, 0)
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
    now = _friday_open_ist_as_utc()
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


def test_low_open_interest_rejects_condor() -> None:
    now = _friday_open_ist_as_utc()
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=7),
        strikes=_strikes(),
        spot=spot,
        atm_mid=1000.0,
        decay_per_pct=150.0,
        open_interest=200.0,
    )
    for symbol in chain._quotes:
        set_quote_open_interest(chain, symbol, 5.0)
    state = make_market_state(now, chain=chain, candles_by_tf={}, spots={Underlying.BTC: spot})
    strat = IronCondorStrategy(condor_cfg(desk={"min_open_interest": 50}))
    intents, decisions = strat.evaluate(state)
    assert intents == []
    assert any(d["reason"] == "low_open_interest" for d in decisions)


def test_condor_passes_logs_wing_greeks() -> None:
    now = _friday_open_ist_as_utc()
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=7),
        strikes=_strikes(),
        spot=spot,
        atm_mid=1000.0,
        decay_per_pct=150.0,
        open_interest=200.0,
    )
    state = make_market_state(now, chain=chain, candles_by_tf={}, spots={Underlying.BTC: spot})
    strat = IronCondorStrategy(condor_cfg(desk={"min_open_interest": 50}))
    intents, decisions = strat.evaluate(state)
    assert len(intents) == 1
    fv = next(d["feature_vector"] for d in decisions if d["passed"])
    assert "short_call_iv" in fv
    assert "long_put_delta" in fv


def test_missing_greeks_blocks_condor_when_delta_band_empty() -> None:
    """Delta-selected wings need greeks on quotes; clearing them prevents strike pick."""
    now = _friday_open_ist_as_utc()
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now + dt.timedelta(days=7),
        strikes=_strikes(),
        spot=spot,
        atm_mid=1000.0,
        decay_per_pct=150.0,
        open_interest=200.0,
    )
    for symbol in chain._quotes:
        quote = chain.get_quote(symbol)
        assert quote is not None
        chain.upsert_quote(replace(quote, delta=None))
    state = make_market_state(now, chain=chain, candles_by_tf={}, spots={Underlying.BTC: spot})
    strat = IronCondorStrategy(condor_cfg(desk={"greeks_required": True}))
    intents, decisions = strat.evaluate(state)
    assert intents == []
    assert any(d["reason"] == "condor_delta_band_unfillable" for d in decisions)

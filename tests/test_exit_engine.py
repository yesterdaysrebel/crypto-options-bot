"""Tests for the per-strategy exit engine.

AC: 1) directional position at +50% premium PnL with +1R peak emits trail-to-BE on first
   tick, then chandelier on subsequent ticks; 2) condor position with unwind cost <= 50%
   credit emits CLOSE(TARGET); 3) condor position with tested-side breach emits TESTED_SIDE_CUT;
   4) strangle position at +50% premium emits CLOSE(TARGET); 5) all strategies emit
   FORCE_CLOSE_EXPIRY inside their respective windows.
"""

from __future__ import annotations

import datetime as dt

import pytest

from bot.config.models import StrategyId, Underlying
from bot.data.chain_cache import QuoteSnapshot
from bot.exits import ExitDirective, ExitEngine, ExitKind, PositionRuntime
from bot.strategies import (
    CreditVerticalStrategy,
    DirectionalStrategy,
    LongStraddleStrategy,
    StrategyRegistry,
)
from bot.strategies.base import ExitTrigger, MarketState, PositionState

from tests.strategy_fixtures import (
    condor_cfg,
    directional_cfg,
    make_chain,
    make_flat_candles,
    make_market_state,
    strangle_cfg,
)


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 12, 10, 0, 0)


def _spot() -> float:
    return 100_000.0


def _make_market_directional(*, with_quote_mid: float | None = None) -> MarketState:
    now = _now()
    spot = _spot()
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now.replace(hour=17, minute=30),
        strikes=list(range(95000, 105001, 500)),
        spot=spot,
    )
    candles = make_flat_candles(n=40, price=spot, base_range=100)
    state = make_market_state(
        now, chain=chain, candles_by_tf={Underlying.BTC: {"15m": candles}}, spots={Underlying.BTC: spot}
    )
    if with_quote_mid is not None:
        state.quote_for["C-BTC-100000-130524"] = QuoteSnapshot(
            symbol="C-BTC-100000-130524",
            bid=with_quote_mid - 1,
            ask=with_quote_mid + 1,
            mark_price=with_quote_mid,
        )
    return state


def _make_position_directional(entry: float = 100.0) -> PositionState:
    return PositionState(
        trade_id=1,
        strategy_id=StrategyId.DIRECTIONAL,
        underlying=Underlying.BTC,
        expiry=_now() + dt.timedelta(days=1),
        lots=3,
        entry_ts=_now() - dt.timedelta(minutes=30),
        entry_premium_inr=entry,
        entry_underlying_price=_spot(),
        entry_atr=200.0,
        leg_states=[{"symbol": "C-BTC-100000-130524", "side": "buy", "option_type": "call"}],
    )


def test_directional_delta_breach_emits_close() -> None:
    cfg = directional_cfg(desk={"max_abs_delta_move": 0.10, "max_abs_gamma_shock": None})
    strat = DirectionalStrategy(cfg)
    engine = ExitEngine(StrategyRegistry([strat]))
    state = _make_market_directional(with_quote_mid=150.0)
    pos = _make_position_directional(entry=100.0)
    pos.notes = {
        "entry_greeks": {
            "C-BTC-100000-130524": {"delta": 0.50, "gamma": 0.0001},
        },
    }
    state.quote_for["C-BTC-100000-130524"] = QuoteSnapshot(
        symbol="C-BTC-100000-130524",
        bid=149.0,
        ask=151.0,
        mark_price=150.0,
        delta=0.70,
        gamma=0.0001,
    )
    runtime = PositionRuntime(position=pos)
    directives = engine.step(runtime, state)
    assert any(d.kind == ExitKind.CLOSE and d.trigger == ExitTrigger.DELTA_BREACH for d in directives)


def test_directional_target_emits_close() -> None:
    strat = DirectionalStrategy(directional_cfg())
    engine = ExitEngine(StrategyRegistry([strat]))
    state = _make_market_directional(with_quote_mid=350.0)
    runtime = PositionRuntime(position=_make_position_directional(entry=100.0))
    directives = engine.step(runtime, state)
    assert any(d.kind == ExitKind.CLOSE and d.trigger == ExitTrigger.TARGET for d in directives)


def test_directional_premium_drawdown_emits_close() -> None:
    strat = DirectionalStrategy(directional_cfg())
    engine = ExitEngine(StrategyRegistry([strat]))
    state = _make_market_directional(with_quote_mid=40.0)
    runtime = PositionRuntime(position=_make_position_directional(entry=100.0))
    directives = engine.step(runtime, state)
    assert any(d.kind == ExitKind.CLOSE and d.trigger == ExitTrigger.PREMIUM_STOP for d in directives)


def test_directional_trail_breakeven_after_1r_peak() -> None:
    strat = DirectionalStrategy(directional_cfg())
    engine = ExitEngine(StrategyRegistry([strat]))
    # lots=3: 1R needs peak >= entry*lots; mid=201 -> peak = 303 INR
    state = _make_market_directional(with_quote_mid=201.0)
    runtime = PositionRuntime(position=_make_position_directional(entry=100.0))
    directives = engine.step(runtime, state)
    update_stops = [d for d in directives if d.kind == ExitKind.UPDATE_STOP]
    assert update_stops, "expected at least one UPDATE_STOP for trail to BE"
    assert update_stops[0].new_stop_price is not None


def test_directional_force_close_within_t_minus_2h() -> None:
    strat = DirectionalStrategy(directional_cfg())
    engine = ExitEngine(StrategyRegistry([strat]))
    now = _now()
    state = _make_market_directional(with_quote_mid=80.0)
    pos = _make_position_directional()
    pos.expiry = now + dt.timedelta(hours=1)
    runtime = PositionRuntime(position=pos)
    directives = engine.step(runtime, state)
    assert any(d.kind == ExitKind.CLOSE and d.trigger == ExitTrigger.FORCE_CLOSE_EXPIRY for d in directives)


def _make_position_condor(entry_credit: float = 300.0) -> PositionState:
    return PositionState(
        trade_id=2,
        strategy_id=StrategyId.CREDIT_VERTICAL,
        underlying=Underlying.BTC,
        expiry=_now() + dt.timedelta(days=5),
        lots=1,
        entry_ts=_now() - dt.timedelta(days=2),
        entry_credit_inr=entry_credit,
        leg_states=[
            {"symbol": "P-BTC-97000-190524", "side": "buy", "option_type": "put"},
            {"symbol": "P-BTC-98000-190524", "side": "sell", "option_type": "put"},
            {"symbol": "C-BTC-102000-190524", "side": "sell", "option_type": "call"},
            {"symbol": "C-BTC-103000-190524", "side": "buy", "option_type": "call"},
        ],
        notes={"short_call_strike": 102_000.0, "short_put_strike": 98_000.0},
    )


def _condor_market_state(quote_mids: dict[str, float], spot: float = 100_000.0) -> MarketState:
    now = _now()
    state = MarketState(now=now, chain=None, candles_by_tf={}, underlying_marks={Underlying.BTC: spot})  # type: ignore[arg-type]
    for sym, mid in quote_mids.items():
        state.quote_for[sym] = QuoteSnapshot(symbol=sym, bid=mid - 1, ask=mid + 1, mark_price=mid)
    return state


def test_condor_profit_take_emits_close() -> None:
    strat = CreditVerticalStrategy(condor_cfg())
    engine = ExitEngine(StrategyRegistry([strat]))
    pos = _make_position_condor(entry_credit=300.0)
    quotes = {
        "P-BTC-97000-190524": 20.0,
        "P-BTC-98000-190524": 50.0,
        "C-BTC-102000-190524": 50.0,
        "C-BTC-103000-190524": 20.0,
    }
    state = _condor_market_state(quotes)
    runtime = PositionRuntime(position=pos)
    directives = engine.step(runtime, state)
    assert any(d.kind == ExitKind.CLOSE and d.trigger == ExitTrigger.TARGET for d in directives)
    assert pos.notes["current_unwind_cost"] == 50 + 50 - 20 - 20


def test_condor_tested_side_cut_emits_close() -> None:
    strat = CreditVerticalStrategy(condor_cfg())
    engine = ExitEngine(StrategyRegistry([strat]))
    pos = _make_position_condor(entry_credit=300.0)
    quotes = {
        "P-BTC-97000-190524": 30.0,
        "P-BTC-98000-190524": 100.0,
        "C-BTC-102000-190524": 200.0,
        "C-BTC-103000-190524": 100.0,
    }
    state = _condor_market_state(quotes, spot=102_500.0)
    runtime = PositionRuntime(position=pos)
    directives = engine.step(runtime, state)
    assert any(d.kind == ExitKind.CLOSE and d.trigger == ExitTrigger.TESTED_SIDE_CUT for d in directives)


def test_condor_force_close_t_minus_2d() -> None:
    strat = CreditVerticalStrategy(condor_cfg())
    engine = ExitEngine(StrategyRegistry([strat]))
    pos = _make_position_condor(entry_credit=300.0)
    pos.expiry = _now() + dt.timedelta(days=1)
    state = _condor_market_state({})
    runtime = PositionRuntime(position=pos)
    directives = engine.step(runtime, state)
    assert any(d.kind == ExitKind.CLOSE and d.trigger == ExitTrigger.FORCE_CLOSE_EXPIRY for d in directives)


def test_strangle_profit_take_emits_close() -> None:
    strat = LongStraddleStrategy(strangle_cfg())
    engine = ExitEngine(StrategyRegistry([strat]))
    pos = PositionState(
        trade_id=3,
        strategy_id=StrategyId.LONG_STRADDLE,
        underlying=Underlying.BTC,
        expiry=_now() + dt.timedelta(days=2),
        lots=2,
        entry_ts=_now() - dt.timedelta(hours=1),
        entry_premium_inr=200.0,
        leg_states=[
            {"symbol": "C-BTC-101000-130524", "side": "buy", "option_type": "call"},
            {"symbol": "P-BTC-99000-130524", "side": "buy", "option_type": "put"},
        ],
    )
    quotes = {"C-BTC-101000-130524": 200.0, "P-BTC-99000-130524": 150.0}
    state = _condor_market_state(quotes)
    runtime = PositionRuntime(position=pos)
    directives = engine.step(runtime, state)
    assert any(d.kind == ExitKind.CLOSE and d.trigger == ExitTrigger.TARGET for d in directives)


def test_strangle_force_close_t_minus_4h() -> None:
    strat = LongStraddleStrategy(strangle_cfg())
    engine = ExitEngine(StrategyRegistry([strat]))
    pos = PositionState(
        trade_id=4,
        strategy_id=StrategyId.LONG_STRADDLE,
        underlying=Underlying.BTC,
        expiry=_now() + dt.timedelta(hours=3),
        lots=2,
        entry_ts=_now() - dt.timedelta(hours=6),
        entry_premium_inr=200.0,
        leg_states=[
            {"symbol": "C-BTC-101000-130524", "side": "buy", "option_type": "call"},
            {"symbol": "P-BTC-99000-130524", "side": "buy", "option_type": "put"},
        ],
    )
    state = _condor_market_state({})
    runtime = PositionRuntime(position=pos)
    directives = engine.step(runtime, state)
    assert any(d.kind == ExitKind.CLOSE and d.trigger == ExitTrigger.FORCE_CLOSE_EXPIRY for d in directives)


def test_put_trail_emits_when_stop_tightens() -> None:
    strat = DirectionalStrategy(directional_cfg())
    engine = ExitEngine(StrategyRegistry([strat]), trail_update_throttle_seconds=0.0)
    sym = "P-BTC-99000-130524"
    state = _make_market_directional(with_quote_mid=210.0)
    state.quote_for[sym] = QuoteSnapshot(symbol=sym, bid=209.0, ask=211.0, mark_price=210.0)
    pos = PositionState(
        trade_id=5,
        strategy_id=StrategyId.DIRECTIONAL,
        underlying=Underlying.BTC,
        expiry=_now() + dt.timedelta(days=1),
        lots=1,
        entry_ts=_now(),
        entry_premium_inr=100.0,
        entry_underlying_price=_spot(),
        entry_atr=200.0,
        leg_states=[{"symbol": sym, "side": "buy", "option_type": "put", "current_mid": 210.0}],
    )
    runtime = PositionRuntime(position=pos)
    directives = engine.step(runtime, state)
    assert any(d.kind == ExitKind.UPDATE_STOP for d in directives)
    assert runtime.last_trail_stop == pytest.approx(100.0)


def test_trail_throttle_suppresses_duplicate_updates() -> None:
    strat = DirectionalStrategy(directional_cfg())
    engine = ExitEngine(StrategyRegistry([strat]), trail_update_throttle_seconds=10.0)
    state = _make_market_directional(with_quote_mid=199.0)
    runtime = PositionRuntime(position=_make_position_directional(entry=100.0))
    _ = engine.step(runtime, state)
    second = engine.step(runtime, state)
    assert all(d.kind != ExitKind.UPDATE_STOP for d in second), (
        "second tick within throttle should not re-emit trail"
    )


def _unused(_d: ExitDirective) -> None: ...  # keep import referenced

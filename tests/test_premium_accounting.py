"""Premium per-lot storage and PnL accounting (regression for multi-lot trades)."""

from __future__ import annotations

import datetime as dt

from bot.config.models import StrategyId, Underlying
from bot.execution.premium import per_lot_premium_from_net, realised_pnl_inr
from bot.execution.router import LegFill, LegSide
from bot.strategies.base import ActionType, ExitTrigger, PositionState
from bot.strategies.directional import DirectionalStrategy

from tests.strategy_fixtures import directional_cfg, make_chain, make_flat_candles, make_market_state


def test_per_lot_premium_from_net_long() -> None:
    prem, cred = per_lot_premium_from_net(278.156, 5)
    assert prem == 278.156 / 5
    assert cred is None


def test_per_lot_premium_from_net_credit() -> None:
    prem, cred = per_lot_premium_from_net(-600.0, 3)
    assert prem is None
    assert cred == 200.0


def test_realised_pnl_multi_lot_matches_leg_prices() -> None:
    """Trade 3 scenario: ~flat when per-lot premium is stored correctly."""
    entry_per_lot = 55.6312
    lots = 5
    exit_per_lot = 55.4019
    fills = [
        LegFill(
            symbol="P-BTC-79400-140526",
            side=LegSide.SELL,
            qty_requested=lots,
            qty_filled=lots,
            avg_fill_price=exit_per_lot,
            leg_idx=0,
            client_order_id="t",
        )
    ]
    pnl = realised_pnl_inr(
        premium_paid_per_lot=entry_per_lot,
        credit_received_per_lot=None,
        lots=lots,
        exit_fills=fills,
    )
    assert abs(pnl - (exit_per_lot - entry_per_lot) * lots) < 1e-6


def test_realised_pnl_old_buggy_total_premium_inflates_loss() -> None:
    """Storing total premium in premium_paid_inr then x lots overstates loss."""
    entry_total_stored_wrong = 278.156
    lots = 5
    exit_per_lot = 55.4019
    fills = [
        LegFill(
            symbol="P",
            side=LegSide.SELL,
            qty_requested=lots,
            qty_filled=lots,
            avg_fill_price=exit_per_lot,
            leg_idx=0,
            client_order_id="t",
        )
    ]
    pnl = realised_pnl_inr(
        premium_paid_per_lot=entry_total_stored_wrong,
        credit_received_per_lot=None,
        lots=lots,
        exit_fills=fills,
    )
    assert pnl < -1000.0


def test_directional_premium_stop_uses_per_lot_entry() -> None:
    now = dt.datetime(2026, 5, 14, 11, 27, 0)
    spot = 79_329.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now.replace(hour=17, minute=30),
        strikes=[79_400],
        spot=spot,
    )
    candles = make_flat_candles(n=40, price=spot, base_range=100)
    market = make_market_state(
        now, chain=chain, candles_by_tf={Underlying.BTC: {"15m": candles}}, spots={Underlying.BTC: spot}
    )
    symbol = "P-BTC-79400-140526"
    mid = 55.63
    market.quote_for[symbol] = chain.get_quote(symbol)
    assert market.quote_for[symbol] is not None
    market.quote_for[symbol].bid = mid - 0.5  # type: ignore[union-attr]
    market.quote_for[symbol].ask = mid + 0.5  # type: ignore[union-attr]

    strat = DirectionalStrategy(directional_cfg())
    position = PositionState(
        trade_id=3,
        strategy_id=StrategyId.DIRECTIONAL,
        underlying=Underlying.BTC,
        expiry=now.replace(hour=17, minute=30),
        lots=5,
        entry_ts=now,
        entry_premium_inr=55.6312,
        entry_underlying_price=spot,
        entry_atr=200.0,
        leg_states=[{"symbol": symbol, "side": "buy", "option_type": "put", "current_mid": mid}],
    )
    actions = strat.manage(position, market)
    close = [a for a in actions if a.kind == ActionType.CLOSE and a.close is not None]
    assert not any(c.close.reason == ExitTrigger.PREMIUM_STOP for c in close if c.close)


def test_directional_premium_stop_fires_at_50pct_drawdown_per_lot() -> None:
    now = dt.datetime(2026, 5, 14, 11, 27, 0)
    spot = 79_329.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now.replace(hour=17, minute=30),
        strikes=[79_400],
        spot=spot,
    )
    candles = make_flat_candles(n=40, price=spot, base_range=100)
    market = make_market_state(
        now, chain=chain, candles_by_tf={Underlying.BTC: {"15m": candles}}, spots={Underlying.BTC: spot}
    )
    symbol = "P-BTC-79400-140526"
    entry = 100.0
    mid = 40.0
    market.quote_for[symbol] = chain.get_quote(symbol)
    assert market.quote_for[symbol] is not None
    market.quote_for[symbol].bid = mid - 0.5  # type: ignore[union-attr]
    market.quote_for[symbol].ask = mid + 0.5  # type: ignore[union-attr]

    strat = DirectionalStrategy(directional_cfg())
    position = PositionState(
        trade_id=1,
        strategy_id=StrategyId.DIRECTIONAL,
        underlying=Underlying.BTC,
        expiry=now.replace(hour=17, minute=30),
        lots=5,
        entry_ts=now,
        entry_premium_inr=entry,
        entry_underlying_price=spot,
        entry_atr=200.0,
        leg_states=[{"symbol": symbol, "side": "buy", "option_type": "put", "current_mid": mid}],
    )
    actions = strat.manage(position, market)
    assert any(
        a.kind == ActionType.CLOSE and a.close is not None and a.close.reason == ExitTrigger.PREMIUM_STOP
        for a in actions
    )


def test_directional_skips_underlying_stop_when_premium_in_profit() -> None:
    """Trade #9 case: spot moved against call but option mid still above entry."""
    now = dt.datetime(2026, 5, 17, 14, 0, 0)
    spot_entry = 2186.92
    spot_now = 2182.31
    chain = make_chain(
        underlying=Underlying.ETH,
        expiry=now.replace(hour=17, minute=30),
        strikes=[2180],
        spot=spot_now,
    )
    candles = make_flat_candles(n=40, price=spot_now, base_range=2)
    market = make_market_state(
        now, chain=chain, candles_by_tf={Underlying.ETH: {"15m": candles}}, spots={Underlying.ETH: spot_now}
    )
    symbol = "C-ETH-2180-170526"
    entry = 8.81
    mid = 12.14
    market.quote_for[symbol] = chain.get_quote(symbol)
    assert market.quote_for[symbol] is not None
    market.quote_for[symbol].bid = mid - 0.1  # type: ignore[union-attr]
    market.quote_for[symbol].ask = mid + 0.1  # type: ignore[union-attr]

    strat = DirectionalStrategy(directional_cfg())
    position = PositionState(
        trade_id=9,
        strategy_id=StrategyId.DIRECTIONAL,
        underlying=Underlying.ETH,
        expiry=now.replace(hour=17, minute=30),
        lots=10,
        entry_ts=now,
        entry_premium_inr=entry,
        entry_underlying_price=spot_entry,
        entry_atr=3.21,
        leg_states=[{"symbol": symbol, "side": "buy", "option_type": "call", "current_mid": mid}],
    )
    actions = strat.manage(position, market)
    close = [a for a in actions if a.kind == ActionType.CLOSE and a.close is not None]
    assert not any(c.close.reason == ExitTrigger.UNDERLYING_STOP for c in close if c.close)


def test_directional_trail_breakeven_requires_full_r_on_multi_lot() -> None:
    """peak_pnl is total INR; breakeven at 1R needs entry * lots profit, not entry alone."""
    now = dt.datetime(2026, 5, 17, 10, 0, 0)
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now.replace(hour=17, minute=30),
        strikes=[100_000],
        spot=spot,
    )
    candles = make_flat_candles(n=40, price=spot, base_range=100)
    market = make_market_state(
        now, chain=chain, candles_by_tf={Underlying.BTC: {"15m": candles}}, spots={Underlying.BTC: spot}
    )
    symbol = "C-BTC-100000-170526"
    entry = 100.0
    mid = 150.0  # +50/lot but only 0.5R on 10 lots (need +100/lot for 1R)
    market.quote_for[symbol] = chain.get_quote(symbol)
    assert market.quote_for[symbol] is not None
    market.quote_for[symbol].bid = mid - 0.5  # type: ignore[union-attr]
    market.quote_for[symbol].ask = mid + 0.5  # type: ignore[union-attr]

    strat = DirectionalStrategy(directional_cfg())
    position = PositionState(
        trade_id=1,
        strategy_id=StrategyId.DIRECTIONAL,
        underlying=Underlying.BTC,
        expiry=now.replace(hour=17, minute=30),
        lots=10,
        entry_ts=now,
        entry_premium_inr=entry,
        entry_underlying_price=spot,
        entry_atr=200.0,
        peak_pnl_inr=500.0,
        leg_states=[{"symbol": symbol, "side": "buy", "option_type": "call", "current_mid": mid}],
    )
    actions = strat.manage(position, market)
    assert not any(a.kind == ActionType.TRAIL_STOP for a in actions), (
        "0.5R total peak should not arm breakeven trail at 1R config"
    )


def test_directional_trail_breach_closes() -> None:
    now = dt.datetime(2026, 5, 17, 10, 0, 0)
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now.replace(hour=17, minute=30),
        strikes=[100_000],
        spot=spot,
    )
    candles = make_flat_candles(n=40, price=spot, base_range=100)
    market = make_market_state(
        now, chain=chain, candles_by_tf={Underlying.BTC: {"15m": candles}}, spots={Underlying.BTC: spot}
    )
    symbol = "C-BTC-100000-170526"
    entry = 100.0
    mid = 99.0
    market.quote_for[symbol] = chain.get_quote(symbol)
    assert market.quote_for[symbol] is not None
    market.quote_for[symbol].bid = mid - 0.5  # type: ignore[union-attr]
    market.quote_for[symbol].ask = mid + 0.5  # type: ignore[union-attr]

    strat = DirectionalStrategy(directional_cfg())
    position = PositionState(
        trade_id=1,
        strategy_id=StrategyId.DIRECTIONAL,
        underlying=Underlying.BTC,
        expiry=now.replace(hour=17, minute=30),
        lots=1,
        entry_ts=now,
        entry_premium_inr=entry,
        entry_underlying_price=spot,
        entry_atr=200.0,
        current_trail_stop_price=entry,
        leg_states=[{"symbol": symbol, "side": "buy", "option_type": "call", "current_mid": mid}],
    )
    actions = strat.manage(position, market)
    assert any(
        a.kind == ActionType.CLOSE and a.close is not None and a.close.reason == ExitTrigger.TRAIL_BREAKEVEN
        for a in actions
    )


def test_directional_exit_cooldown_applies_after_any_close() -> None:
    from bot.runtime.engine import _apply_directional_exit_cooldown
    from bot.strategies import StrategyRegistry

    now = dt.datetime(2026, 5, 17, 10, 0, 0)
    strat = DirectionalStrategy(directional_cfg())
    registry = StrategyRegistry([strat])
    trade = type("T", (), {"strategy_id": "directional", "underlying": "BTC"})()
    _apply_directional_exit_cooldown(registry, trade, now=now)
    assert strat.context.is_underlying_in_cooldown("BTC", now + dt.timedelta(minutes=1))
    assert not strat.context.is_underlying_in_cooldown("BTC", now + dt.timedelta(minutes=60))


def test_directional_underlying_cooldown_blocks_entry() -> None:
    now = dt.datetime(2026, 5, 17, 10, 0, 0)
    spot = 100_000.0
    chain = make_chain(
        underlying=Underlying.BTC,
        expiry=now.replace(hour=17, minute=30),
        strikes=list(range(95000, 105001, 500)),
        spot=spot,
    )
    candles = make_flat_candles(n=40, price=spot, base_range=100)
    market = make_market_state(
        now, chain=chain, candles_by_tf={Underlying.BTC: {"15m": candles}}, spots={Underlying.BTC: spot}
    )
    strat = DirectionalStrategy(directional_cfg())
    strat.context.set_underlying_cooldown("BTC", now + dt.timedelta(minutes=30))
    _, decisions = strat.evaluate(market)
    btc = next(d for d in decisions if d.get("underlying") == "BTC")
    assert btc["reason"] == "cooldown_active"

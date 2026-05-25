"""Portfolio greek aggregation across open legs."""

from __future__ import annotations

import datetime as dt

import pytest
from bot.config.models import Underlying
from bot.data.chain_cache import InstrumentRecord, QuoteSnapshot
from bot.desk.portfolio_greeks import PortfolioGreeks
from bot.storage.models import Leg, Trade


def _trade(*, lots: int = 1, underlying: str = "BTC") -> Trade:
    return Trade(
        strategy_id="directional",
        underlying=underlying,
        lots=lots,
        entry_ts=dt.datetime(2026, 5, 18, 10, 0, 0),
    )


def _leg(
    trade_id: int,
    *,
    symbol: str,
    side: str,
    lots: int = 1,
) -> Leg:
    return Leg(
        trade_id=trade_id,
        strategy_id="directional",
        leg_idx=0,
        symbol=symbol,
        side=side,
        lots=lots,
        option_type="call",
    )


def test_long_call_delta_matches_hand_calc() -> None:
    sym = "C-BTC-100000-180526"
    trade = _trade(lots=3)
    legs = [_leg(1, symbol=sym, side="buy", lots=3)]
    quotes = {
        sym: QuoteSnapshot(symbol=sym, delta=0.25, gamma=0.001, theta=-5.0, vega=12.0),
    }
    book = PortfolioGreeks.from_open_trades([(trade, legs)], quotes)
    assert book.delta == pytest.approx(3 * 0.25)
    assert book.gamma == pytest.approx(3 * 0.001)
    assert book.theta == pytest.approx(3 * -5.0)
    assert book.vega == pytest.approx(3 * 12.0)
    assert book.legs_with_greeks == 1
    assert book.legs_missing_greeks == 0


def test_short_leg_flips_sign() -> None:
    sym = "C-BTC-100000-180526"
    trade = _trade(lots=2)
    legs = [_leg(1, symbol=sym, side="sell", lots=2)]
    quotes = {sym: QuoteSnapshot(symbol=sym, delta=0.30)}
    book = PortfolioGreeks.from_open_trades([(trade, legs)], quotes)
    assert book.delta == pytest.approx(-2 * 0.30)


def test_strangle_net_delta() -> None:
    call_sym = "C-BTC-100000-180526"
    put_sym = "P-BTC-100000-180526"
    trade = _trade(lots=1)
    legs = [
        Leg(
            trade_id=1,
            strategy_id="long_straddle",
            leg_idx=0,
            symbol=call_sym,
            side="buy",
            lots=1,
            option_type="call",
        ),
        Leg(
            trade_id=1,
            strategy_id="long_straddle",
            leg_idx=1,
            symbol=put_sym,
            side="buy",
            lots=1,
            option_type="put",
        ),
    ]
    quotes = {
        call_sym: QuoteSnapshot(symbol=call_sym, delta=0.25),
        put_sym: QuoteSnapshot(symbol=put_sym, delta=-0.20),
    }
    book = PortfolioGreeks.from_open_trades([(trade, legs)], quotes)
    assert book.delta == pytest.approx(0.05)
    assert book.legs_with_greeks == 2


def test_contract_multiplier_from_chain() -> None:
    from unittest.mock import AsyncMock

    from bot.data.chain_cache import ChainCache

    sym = "C-BTC-100000-180526"
    chain = ChainCache(AsyncMock())
    chain._instruments_by_symbol[sym] = InstrumentRecord(
        product_id=1,
        symbol=sym,
        underlying=Underlying.BTC,
        option_type="call",
        strike=100_000.0,
        expiry=dt.datetime(2026, 5, 18, 17, 30),
        lot_size=0.001,
        tick_size=0.5,
    )
    trade = _trade(lots=2)
    legs = [_leg(1, symbol=sym, side="buy", lots=2)]
    quotes = {sym: QuoteSnapshot(symbol=sym, delta=0.50)}
    book = PortfolioGreeks.from_open_trades([(trade, legs)], quotes, chain=chain)
    assert book.delta == pytest.approx(2 * 0.001 * 0.50)


def test_missing_quote_counts_as_missing() -> None:
    trade = _trade()
    legs = [_leg(1, symbol="C-BTC-100000-180526", side="buy")]
    book = PortfolioGreeks.from_open_trades([(trade, legs)], {})
    assert book.delta == 0.0
    assert book.legs_missing_greeks == 1

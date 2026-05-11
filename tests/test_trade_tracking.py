"""Trade tracking helpers (indicators + unrealised PnL estimates)."""

from __future__ import annotations

import datetime as dt

from bot.config.models import Underlying
from bot.data.candles import Candle, CandleAggregator
from bot.data.chain_cache import ChainCache, QuoteSnapshot
from bot.exchange.rest import DeltaRestClient
from bot.storage.models import Leg, Trade
from bot.strategies.base import MarketState
from bot.runtime.trade_tracking import (
    estimate_unrealized_pnl_inr,
    indicator_snapshot_for_underlying,
)


def test_indicator_snapshot_contains_spot_and_tf_keys() -> None:
    agg = CandleAggregator("15m", history=32)
    ts = dt.datetime(2026, 1, 1, 12, 0, 0)
    agg.add_tick(ts, 100.0, 1.0)
    agg.add_tick(ts.replace(minute=30), 101.0, 1.0)
    closed = list(agg.closed)
    chain = ChainCache(DeltaRestClient.__new__(DeltaRestClient))  # type: ignore[misc]
    market = MarketState(
        now=ts,
        chain=chain,
        candles_by_tf={Underlying.BTC: {"15m": closed, "1h": []}},
        underlying_marks={Underlying.BTC: 102.0},
    )
    snap = indicator_snapshot_for_underlying(Underlying.BTC, market)
    assert snap["underlying"] == "BTC"
    assert snap["spot"] == 102.0
    assert snap["15m_n"] == len(closed)


def test_estimate_unrealized_long_leg() -> None:
    rest = DeltaRestClient.__new__(DeltaRestClient)  # type: ignore[misc]
    chain = ChainCache(rest)
    chain.upsert_quote(QuoteSnapshot(symbol="C-BTC-100000-150526", bid=400.0, ask=404.0))
    trade = Trade(
        strategy_id="directional",
        underlying="BTC",
        lots=1,
        premium_paid_inr=320.0,
    )
    trade.id = 1
    legs = [
        Leg(
            trade_id=1,
            strategy_id="directional",
            leg_idx=0,
            symbol="C-BTC-100000-150526",
            option_type="call",
            strike=100_000.0,
            side="buy",
            lots=1,
            entry_price=320.0,
            status="open",
        )
    ]
    u = estimate_unrealized_pnl_inr(trade, legs, chain)
    assert u is not None
    assert abs(u - (402.0 - 320.0)) < 0.01

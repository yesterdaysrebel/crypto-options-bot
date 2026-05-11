"""Tests for the execution router (DryExecutor) and atomic helper semantics."""

from __future__ import annotations

import datetime as dt

import pytest
from bot.config.models import StrategyId, Underlying
from bot.data.chain_cache import ChainCache, InstrumentRecord, QuoteSnapshot
from bot.execution.client_id import generate_client_order_id
from bot.execution.dry import DryExecutor
from bot.execution.router import EntryRequest, ExitRequest, LegSide
from bot.strategies.base import ExitTrigger, LegIntent


class _StubRest:
    pass


def _seed_chain(
    legs: list[tuple[str, float, float]],
    *,
    expiry: dt.datetime | None = None,
) -> ChainCache:
    cache = ChainCache(_StubRest())  # type: ignore[arg-type]
    if expiry is None:
        expiry = dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(days=3)
    for idx, (symbol, bid, ask) in enumerate(legs):
        cache._instruments_by_symbol[symbol] = InstrumentRecord(
            product_id=1000 + idx,
            symbol=symbol,
            underlying=Underlying.BTC,
            option_type="call" if symbol.startswith("C") else "put",
            strike=100000.0 + idx * 1000,
            expiry=expiry,
            lot_size=0.001,
            tick_size=0.5,
        )
        cache.upsert_quote(QuoteSnapshot(symbol=symbol, bid=bid, ask=ask))
    return cache


def _leg(symbol: str, side: str, strike: float, option_type: str = "call") -> LegIntent:
    return LegIntent(
        symbol=symbol,
        side=side,
        option_type=option_type,
        strike=strike,
        expiry=dt.datetime.now(dt.UTC).replace(tzinfo=None) + dt.timedelta(days=3),
    )


def test_client_order_id_is_stable_for_same_salt() -> None:
    a = generate_client_order_id(
        strategy_id="directional", trade_id=7, leg_idx=0, purpose="entry", salt="abc12345"
    )
    b = generate_client_order_id(
        strategy_id="directional", trade_id=7, leg_idx=0, purpose="entry", salt="abc12345"
    )
    assert a == b
    assert a.startswith("directional-7-0-entry-")


def test_client_order_id_changes_with_purpose_and_leg() -> None:
    a = generate_client_order_id(strategy_id="iron_condor", trade_id=1, leg_idx=0, purpose="entry", salt="s")
    b = generate_client_order_id(strategy_id="iron_condor", trade_id=1, leg_idx=1, purpose="entry", salt="s")
    c = generate_client_order_id(
        strategy_id="iron_condor", trade_id=1, leg_idx=0, purpose="exit_target", salt="s"
    )
    assert a != b != c != a


@pytest.mark.asyncio
async def test_dry_submit_entry_directional_fills_one_leg() -> None:
    cache = _seed_chain([("C-BTC-100000-150526", 100.0, 110.0)])
    exec_ = DryExecutor(cache, seed=1)
    res = await exec_.submit_entry(
        EntryRequest(
            strategy_id=StrategyId.DIRECTIONAL,
            trade_id=1,
            underlying=Underlying.BTC,
            legs=[_leg("C-BTC-100000-150526", "buy", 100000.0)],
            lots=2,
            intent_rationale="unit-test",
        )
    )
    assert res.success
    assert len(res.fills) == 1
    fill = res.fills[0]
    assert fill.qty_filled == 2
    assert fill.avg_fill_price is not None
    assert 95.0 < fill.avg_fill_price < 115.0
    assert fill.client_order_id.startswith("directional-1-0-entry-")


@pytest.mark.asyncio
async def test_dry_submit_entry_iron_condor_four_legs_atomic() -> None:
    legs = [
        ("P-BTC-97000-150526", 18.0, 22.0),
        ("P-BTC-98000-150526", 48.0, 52.0),
        ("C-BTC-102000-150526", 48.0, 52.0),
        ("C-BTC-103000-150526", 18.0, 22.0),
    ]
    cache = _seed_chain(legs)
    exec_ = DryExecutor(cache, seed=7)
    leg_intents = [
        _leg("P-BTC-97000-150526", "buy", 97000.0, "put"),
        _leg("P-BTC-98000-150526", "sell", 98000.0, "put"),
        _leg("C-BTC-102000-150526", "sell", 102000.0, "call"),
        _leg("C-BTC-103000-150526", "buy", 103000.0, "call"),
    ]
    res = await exec_.submit_entry(
        EntryRequest(
            strategy_id=StrategyId.IRON_CONDOR,
            trade_id=42,
            underlying=Underlying.BTC,
            legs=leg_intents,
            lots=1,
            intent_rationale="condor",
        )
    )
    assert res.success
    assert len(res.fills) == 4
    assert all(f.qty_filled == 1 for f in res.fills)
    # Entry credit (sell-buy) should be positive: shorts ~50 - longs ~20 = ~60 (per lot side).
    assert (
        res.total_premium_inr < 0
    )  # negative because total = long(+) - short(-): longs 20+20=40, shorts -(50+50)=-100, net -60


@pytest.mark.asyncio
async def test_dry_submit_entry_rejects_when_quote_missing() -> None:
    cache = _seed_chain([("C-BTC-100000-150526", 0.0, 0.0)])
    cache.upsert_quote(QuoteSnapshot(symbol="C-BTC-100000-150526", bid=None, ask=None))
    exec_ = DryExecutor(cache, seed=2)
    res = await exec_.submit_entry(
        EntryRequest(
            strategy_id=StrategyId.DIRECTIONAL,
            trade_id=2,
            underlying=Underlying.BTC,
            legs=[_leg("C-BTC-100000-150526", "buy", 100000.0)],
            lots=1,
            intent_rationale="missing-quote",
        )
    )
    assert not res.success
    assert res.error == "dry_executor_partial_fill"
    assert res.fills[0].state == "rejected"


@pytest.mark.asyncio
async def test_dry_submit_exit_flips_sides_and_settles() -> None:
    cache = _seed_chain([("C-BTC-100000-150526", 200.0, 210.0)])
    exec_ = DryExecutor(cache, seed=3)
    res = await exec_.submit_exit(
        ExitRequest(
            strategy_id=StrategyId.DIRECTIONAL,
            trade_id=99,
            underlying=Underlying.BTC,
            legs=[_leg("C-BTC-100000-150526", "buy", 100000.0)],
            lots=1,
            trigger=ExitTrigger.TARGET,
        )
    )
    assert res.success
    assert len(res.fills) == 1
    fill = res.fills[0]
    assert fill.side == LegSide.SELL  # flipped
    assert "exit_target" in fill.client_order_id


@pytest.mark.asyncio
async def test_dry_update_stop_returns_ok_envelope() -> None:
    cache = _seed_chain([("C-BTC-100000-150526", 100.0, 110.0)])
    exec_ = DryExecutor(cache)
    out = await exec_.update_stop(
        trade_id=5,
        symbol="C-BTC-100000-150526",
        side=LegSide.SELL,
        qty=1,
        new_stop_price=72.5,
        client_order_id="directional-5-0-trail-aaaaaaaaaa",
    )
    assert out["ok"] is True
    assert out["new_stop_price"] == 72.5


@pytest.mark.asyncio
async def test_dry_cancel_all_for_trade_clears_open_orders() -> None:
    cache = _seed_chain([("C-BTC-100000-150526", 100.0, 110.0)])
    exec_ = DryExecutor(cache, seed=4)
    await exec_.submit_entry(
        EntryRequest(
            strategy_id=StrategyId.DIRECTIONAL,
            trade_id=11,
            underlying=Underlying.BTC,
            legs=[_leg("C-BTC-100000-150526", "buy", 100000.0)],
            lots=1,
            intent_rationale="t",
        )
    )
    n = await exec_.cancel_all_for_trade(11)
    assert n == 1
    n2 = await exec_.cancel_all_for_trade(11)
    assert n2 == 0

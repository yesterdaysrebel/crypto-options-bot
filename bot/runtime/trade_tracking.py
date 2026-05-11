"""Open-trade analytics: indicator snapshots, unrealised PnL, peak for trailing."""

from __future__ import annotations

from typing import Any

from bot.config.models import Underlying
from bot.data.chain_cache import ChainCache
from bot.storage.db import Database
from bot.storage.models import Leg, Trade
from bot.strategies.base import MarketState


def indicator_snapshot_for_underlying(underlying: Underlying, market: MarketState) -> dict[str, Any]:
    """Lightweight context for journals / trade.notes (spot + last candle stats)."""
    out: dict[str, Any] = {
        "underlying": underlying.value,
        "spot": market.spot(underlying),
    }
    for tf in ("15m", "1h"):
        candles = market.candles(underlying, tf)
        out[f"{tf}_n"] = len(candles)
        if candles:
            last = candles[-1]
            out[f"{tf}_last_close"] = last.close
            out[f"{tf}_last_high"] = last.high
            out[f"{tf}_last_low"] = last.low
            out[f"{tf}_last_ts"] = last.ts.isoformat()
    return out


def indicator_snapshot_for_trade(trade: Trade, market: MarketState) -> dict[str, Any]:
    return indicator_snapshot_for_underlying(Underlying(trade.underlying), market)


def estimate_unrealized_pnl_inr(trade: Trade, legs: list[Leg], chain: ChainCache) -> float | None:
    """Single long-premium style leg: (mid - entry) * lots. Multi-leg returns None (extend later)."""
    if len(legs) != 1:
        return None
    leg = legs[0]
    if leg.side != "buy" or leg.entry_price is None:
        return None
    q = chain.get_quote(leg.symbol)
    mid = q.mid if q is not None else None
    if mid is None:
        return None
    return (float(mid) - float(leg.entry_price)) * float(trade.lots)


async def refresh_all_open_trades(
    db: Database,
    open_rows: list[tuple[Trade, list[Leg]]],
    chain: ChainCache,
    market: MarketState,
    *,
    wallet_snapshot: dict[str, Any] | None,
) -> None:
    for trade, legs in open_rows:
        await refresh_open_trade_notes(db, trade, legs, chain, market, wallet_snapshot=wallet_snapshot)


async def refresh_open_trade_notes(
    db: Database,
    trade: Trade,
    legs: list[Leg],
    chain: ChainCache,
    market: MarketState,
    *,
    wallet_snapshot: dict[str, Any] | None,
) -> None:
    """Update `trade.notes` with latest indicators, wallet (throttled by caller), unrealised + peak PnL."""
    ind = indicator_snapshot_for_trade(trade, market)
    unreal = estimate_unrealized_pnl_inr(trade, legs, chain)
    async with db.session() as session:
        t = await session.get(Trade, trade.id)
        if t is None:
            return
        notes = dict(t.notes or {})
        notes["last_indicator_snapshot"] = ind
        if wallet_snapshot is not None:
            notes["wallet_last_tick"] = wallet_snapshot
        if unreal is not None:
            notes["unrealized_pnl_inr"] = unreal
            prev_peak = notes.get("peak_pnl_inr")
            try:
                prev_peak_f = float(prev_peak) if prev_peak is not None else unreal
            except (TypeError, ValueError):
                prev_peak_f = unreal
            notes["peak_pnl_inr"] = max(prev_peak_f, unreal)
        t.notes = notes


__all__ = [
    "estimate_unrealized_pnl_inr",
    "indicator_snapshot_for_trade",
    "indicator_snapshot_for_underlying",
    "refresh_all_open_trades",
    "refresh_open_trade_notes",
]

"""Simplified exchange-greek PnL attribution on trade close."""

from __future__ import annotations

from collections.abc import Mapping

from bot.config.models import Underlying
from bot.data.chain_cache import ChainCache
from bot.storage.models import Leg, Trade


def estimate_delta_pnl_inr(
    trade: Trade,
    legs: list[Leg],
    *,
    entry_underlying_price: float | None,
    exit_underlying_price: float | None,
    usd_inr_rate: float,
    chain: ChainCache | None = None,
) -> float | None:
    """Approximate delta PnL: sum(entry_delta * spot_move * side * lots * contract_size) in INR."""
    if entry_underlying_price is None or exit_underlying_price is None:
        return None
    spot_move = float(exit_underlying_price) - float(entry_underlying_price)
    if abs(spot_move) < 1e-12:
        return 0.0

    entry_greeks = (trade.notes or {}).get("entry_greeks")
    if not isinstance(entry_greeks, dict):
        return None

    total = 0.0
    any_leg = False
    for leg in legs:
        row = entry_greeks.get(leg.symbol)
        if not isinstance(row, dict):
            continue
        delta_entry = row.get("delta")
        if delta_entry is None:
            continue
        sign = _side_sign(leg.side)
        if sign == 0.0:
            continue
        lots = float(leg.lots if leg.lots else trade.lots)
        mult = _contract_multiplier(chain, leg.symbol)
        total += float(delta_entry) * spot_move * sign * lots * mult * usd_inr_rate
        any_leg = True

    return total if any_leg else None


def entry_underlying_from_trade(trade: Trade) -> float | None:
    notes = trade.notes or {}
    raw = notes.get("entry_underlying_price")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def exit_underlying_from_marks(
    trade: Trade,
    underlying_marks: Mapping[Underlying, float],
) -> float | None:
    try:
        underlying = Underlying(trade.underlying)
    except ValueError:
        return None
    spot = underlying_marks.get(underlying)
    return float(spot) if spot is not None else None


def _contract_multiplier(chain: ChainCache | None, symbol: str) -> float:
    if chain is None:
        return 1.0
    inst = chain.get_instrument(symbol)
    if inst is None:
        return 1.0
    return float(inst.lot_size)


def _side_sign(side: str) -> float:
    s = side.strip().lower()
    if s == "buy":
        return 1.0
    if s == "sell":
        return -1.0
    return 0.0

"""Cap position lots by per-trade vega/gamma notional (desk risk budgets)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from bot.data.chain_cache import ChainCache, QuoteSnapshot
from bot.desk.greek_snapshot import resolve_quote
from bot.strategies.base import Intent


def cap_lots_by_greeks(
    intent: Intent,
    sized_lots: int,
    quote_for: Mapping[str, QuoteSnapshot],
    chain: ChainCache | None,
    *,
    max_vega_inr: float | None,
    max_gamma_inr: float | None,
    usd_inr_rate: float,
) -> tuple[int, dict[str, Any]]:
    """Return lots capped so estimated vega/gamma INR stay within desk limits."""
    if sized_lots < 1:
        return sized_lots, {}

    notes: dict[str, Any] = {}
    capped = sized_lots

    if max_vega_inr is not None and max_vega_inr > 0:
        vega_per_lot = _per_lot_greek_inr(intent, quote_for, chain, "vega", usd_inr_rate=usd_inr_rate)
        notes["vega_inr_per_lot"] = vega_per_lot
        if vega_per_lot > 0:
            cap = min(capped, max(1, math.floor(max_vega_inr / vega_per_lot)))
            capped = min(capped, cap)

    if max_gamma_inr is not None and max_gamma_inr > 0:
        gamma_per_lot = _per_lot_greek_inr(intent, quote_for, chain, "gamma", usd_inr_rate=usd_inr_rate)
        notes["gamma_inr_per_lot"] = gamma_per_lot
        if gamma_per_lot > 0:
            cap = min(capped, max(1, math.floor(max_gamma_inr / gamma_per_lot)))
            capped = min(capped, cap)

    if capped < sized_lots:
        notes["greek_cap_applied"] = True
        notes["lots_before_greek_cap"] = float(sized_lots)
    return capped, notes


def _per_lot_greek_inr(
    intent: Intent,
    quote_for: Mapping[str, QuoteSnapshot],
    chain: ChainCache | None,
    greek: str,
    *,
    usd_inr_rate: float,
) -> float:
    total = 0.0
    for leg in intent.legs:
        quote = resolve_quote(leg.symbol, quote_for, chain)
        if quote is None:
            continue
        value = getattr(quote, greek, None)
        if value is None:
            continue
        mult = _contract_multiplier(chain, leg.symbol)
        total += abs(float(value)) * mult * usd_inr_rate
    return total


def _contract_multiplier(chain: ChainCache | None, symbol: str) -> float:
    if chain is None:
        return 1.0
    inst = chain.get_instrument(symbol)
    if inst is None:
        return 1.0
    return float(inst.lot_size)

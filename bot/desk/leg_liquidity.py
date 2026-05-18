"""Per-leg OI and greek checks for multi-leg option strategies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bot.data.chain_cache import QuoteSnapshot, StrikeSelection


@dataclass(frozen=True)
class LegLiquidityResult:
    ok: bool
    reason: str | None = None
    symbol: str | None = None
    features: dict[str, Any] | None = None


def leg_quote_features(role: str, quote: QuoteSnapshot) -> dict[str, Any]:
    return {
        f"{role}_iv": quote.iv,
        f"{role}_delta": quote.delta,
        f"{role}_gamma": quote.gamma,
        f"{role}_theta": quote.theta,
        f"{role}_vega": quote.vega,
        f"{role}_oi": quote.open_interest,
    }


def check_multi_leg_liquidity(
    legs: list[tuple[str, StrikeSelection]],
    *,
    min_open_interest: float,
    greeks_required: bool,
) -> LegLiquidityResult:
    features: dict[str, Any] = {}
    for role, sel in legs:
        quote = sel.quote
        features.update(leg_quote_features(role, quote))
        if greeks_required and quote.delta is None:
            return LegLiquidityResult(
                ok=False,
                reason="missing_greeks",
                symbol=sel.instrument.symbol,
                features=features,
            )
        if min_open_interest > 0:
            oi = quote.open_interest
            if oi is None or oi < min_open_interest:
                return LegLiquidityResult(
                    ok=False,
                    reason="low_open_interest",
                    symbol=sel.instrument.symbol,
                    features=features,
                )
    return LegLiquidityResult(ok=True, features=features)

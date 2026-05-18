"""Aggregate exchange greeks across open option legs (desk portfolio view)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from bot.config.models import Underlying
from bot.data.chain_cache import ChainCache, QuoteSnapshot
from bot.storage.models import Leg, Trade


@dataclass(frozen=True)
class UnderlyingGreeks:
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0


@dataclass(frozen=True)
class PortfolioGreeks:
    """Book-level greek totals from open legs and live quotes."""

    delta: float
    gamma: float
    theta: float
    vega: float
    by_underlying: dict[str, UnderlyingGreeks] = field(default_factory=dict)
    legs_with_greeks: int = 0
    legs_missing_greeks: int = 0

    @classmethod
    def from_open_trades(
        cls,
        open_rows: Iterable[tuple[Trade, list[Leg]]],
        quote_for: Mapping[str, QuoteSnapshot],
        *,
        chain: ChainCache | None = None,
    ) -> PortfolioGreeks:
        if not isinstance(quote_for, Mapping):
            raise TypeError(f"quote_for must be a mapping, got {type(quote_for).__name__}")

        totals = UnderlyingGreeks()
        by_u: dict[str, UnderlyingGreeks] = {}
        with_greeks = 0
        missing = 0

        for trade, legs in open_rows:
            if not isinstance(trade, Trade):
                raise TypeError(f"trade must be Trade, got {type(trade).__name__}")
            if not isinstance(legs, list):
                raise TypeError(f"legs must be a list, got {type(legs).__name__}")

            underlying_key = str(trade.underlying)
            bucket = by_u.setdefault(underlying_key, UnderlyingGreeks())

            for leg in legs:
                if not isinstance(leg, Leg):
                    raise TypeError(f"leg must be Leg, got {type(leg).__name__}")

                quote = quote_for.get(leg.symbol)
                if quote is None:
                    missing += 1
                    continue

                contrib = _leg_greeks(leg, trade, quote, chain)
                if contrib is None:
                    missing += 1
                    continue

                with_greeks += 1
                d, g, t, v = contrib
                totals = _add_greeks(totals, d, g, t, v)
                by_u[underlying_key] = _add_greeks(bucket, d, g, t, v)

        return cls(
            delta=totals.delta,
            gamma=totals.gamma,
            theta=totals.theta,
            vega=totals.vega,
            by_underlying=by_u,
            legs_with_greeks=with_greeks,
            legs_missing_greeks=missing,
        )

    def for_underlying(self, underlying: Underlying) -> UnderlyingGreeks:
        return self.by_underlying.get(underlying.value, UnderlyingGreeks())


def _leg_greeks(
    leg: Leg,
    trade: Trade,
    quote: QuoteSnapshot,
    chain: ChainCache | None,
) -> tuple[float, float, float, float] | None:
    if quote.delta is None and quote.gamma is None and quote.theta is None and quote.vega is None:
        return None

    sign = _side_sign(leg.side)
    if sign == 0.0:
        return None

    lots = float(leg.lots if leg.lots else trade.lots)
    mult = _contract_multiplier(chain, leg.symbol)

    scale = sign * lots * mult
    return (
        scale * float(quote.delta or 0.0),
        scale * float(quote.gamma or 0.0),
        scale * float(quote.theta or 0.0),
        scale * float(quote.vega or 0.0),
    )


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


def _add_greeks(
    base: UnderlyingGreeks,
    delta: float,
    gamma: float,
    theta: float,
    vega: float,
) -> UnderlyingGreeks:
    return UnderlyingGreeks(
        delta=base.delta + delta,
        gamma=base.gamma + gamma,
        theta=base.theta + theta,
        vega=base.vega + vega,
    )

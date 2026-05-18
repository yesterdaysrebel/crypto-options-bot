"""Capture exchange greeks and IV at trade entry/exit for audit and attribution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from bot.data.chain_cache import ChainCache, QuoteSnapshot


def resolve_quote(
    symbol: str,
    quote_for: Mapping[str, QuoteSnapshot],
    chain: ChainCache | None = None,
) -> QuoteSnapshot | None:
    quote = quote_for.get(symbol)
    if quote is None and chain is not None:
        quote = chain.get_quote(symbol)
    return quote


def leg_greeks_from_quote(quote: QuoteSnapshot) -> dict[str, float | None]:
    return {
        "iv": quote.iv,
        "delta": quote.delta,
        "gamma": quote.gamma,
        "theta": quote.theta,
        "vega": quote.vega,
        "open_interest": quote.open_interest,
    }


def trade_iv_from_symbols(
    symbols: Sequence[str],
    quote_for: Mapping[str, QuoteSnapshot],
    *,
    chain: ChainCache | None = None,
) -> float | None:
    """Representative trade IV: mean of leg IVs that are present on the quote."""
    values: list[float] = []
    for symbol in symbols:
        quote = resolve_quote(symbol, quote_for, chain)
        if quote is None or quote.iv is None:
            continue
        values.append(float(quote.iv))
    if not values:
        return None
    return sum(values) / len(values)


def greeks_by_symbol(
    symbols: Sequence[str],
    quote_for: Mapping[str, QuoteSnapshot],
    *,
    chain: ChainCache | None = None,
) -> dict[str, dict[str, float | None]]:
    out: dict[str, dict[str, float | None]] = {}
    for symbol in symbols:
        quote = resolve_quote(symbol, quote_for, chain)
        if quote is None:
            continue
        out[symbol] = leg_greeks_from_quote(quote)
    return out

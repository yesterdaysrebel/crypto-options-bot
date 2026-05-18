"""Desk policy gates: liquidity, greeks, and portfolio limits before sizing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from bot.config.models import DeskConfig, Underlying
from bot.data.chain_cache import ChainCache, QuoteSnapshot
from bot.desk.portfolio_greeks import PortfolioGreeks

if TYPE_CHECKING:
    from bot.strategies.base import Intent


class DeskPolicy:
    def __init__(self, config: DeskConfig) -> None:
        if not isinstance(config, DeskConfig):
            raise TypeError(f"config must be DeskConfig, got {type(config).__name__}")
        self._cfg = config

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    def check_portfolio(
        self,
        portfolio: PortfolioGreeks,
        underlying_marks: Mapping[Underlying, float],
        *,
        usd_inr_rate: float,
    ) -> tuple[str | None, dict[str, Any]]:
        if not isinstance(portfolio, PortfolioGreeks):
            raise TypeError(f"portfolio must be PortfolioGreeks, got {type(portfolio).__name__}")
        if not isinstance(underlying_marks, Mapping):
            raise TypeError(f"underlying_marks must be a mapping, got {type(underlying_marks).__name__}")
        if usd_inr_rate <= 0:
            raise ValueError("usd_inr_rate must be positive")

        notes: dict[str, Any] = {}
        delta_inr = 0.0
        vega_inr = 0.0
        for key, ug in portfolio.by_underlying.items():
            try:
                underlying = Underlying(key)
            except ValueError:
                continue
            spot = underlying_marks.get(underlying)
            if spot is None or spot <= 0:
                continue
            delta_inr += abs(ug.delta) * float(spot) * usd_inr_rate
            vega_inr += abs(ug.vega) * usd_inr_rate

        notes["portfolio_delta_inr"] = delta_inr
        notes["portfolio_vega_inr"] = vega_inr

        max_delta = self._cfg.max_abs_net_delta_inr
        if max_delta is not None and delta_inr > max_delta:
            notes["max_abs_net_delta_inr"] = max_delta
            return "portfolio_delta_limit", notes

        max_vega = self._cfg.max_abs_net_vega_inr
        if max_vega is not None and vega_inr > max_vega:
            notes["max_abs_net_vega_inr"] = max_vega
            return "portfolio_vega_limit", notes

        return None, notes

    def check_intent(
        self,
        intent: Intent,
        quote_for: Mapping[str, QuoteSnapshot],
        chain: ChainCache | None,
    ) -> tuple[str | None, dict[str, Any]]:
        from bot.strategies.base import Intent as IntentCls

        if not isinstance(intent, IntentCls):
            raise TypeError(f"intent must be Intent, got {type(intent).__name__}")
        if not isinstance(quote_for, Mapping):
            raise TypeError(f"quote_for must be a mapping, got {type(quote_for).__name__}")

        min_oi = self._cfg.min_open_interest
        notes: dict[str, Any] = {"min_open_interest": min_oi}

        for leg in intent.legs:
            quote = quote_for.get(leg.symbol)
            if quote is None and chain is not None:
                quote = chain.get_quote(leg.symbol)
            if quote is None:
                if self._cfg.strict:
                    notes["symbol"] = leg.symbol
                    return "missing_greeks", notes
                continue

            if self._cfg.greeks_required and quote.delta is None:
                notes["symbol"] = leg.symbol
                return "missing_greeks", notes

            if min_oi > 0:
                oi = quote.open_interest
                if oi is None:
                    if self._cfg.strict:
                        notes["symbol"] = leg.symbol
                        return "low_open_interest", notes
                    continue
                notes["open_interest"] = oi
                if oi < min_oi:
                    notes["symbol"] = leg.symbol
                    return "low_open_interest", notes

        return None, notes

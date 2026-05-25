"""Strategy B — Trend-aligned credit vertical (bull put / bear call spread).

Replaces iron condor for smaller accounts: 2 legs, defined max loss, trades on trend
days (not Friday-only). Bullish → sell put spread; bearish → sell call spread.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from bot.config.models import (
    CreditVerticalConfig,
    ExpiryBucket,
    StrategyId,
    Underlying,
)
from bot.desk.leg_liquidity import check_multi_leg_liquidity
from bot.risk.window import utc_to_ist
from bot.strategies.base import (
    Action,
    ActionType,
    CloseAction,
    ExitTrigger,
    Intent,
    LegIntent,
    MarketState,
    PositionState,
    Strategy,
)
from bot.strategies.trend_signal import evaluate_trend_breakout


class CreditVerticalStrategy(Strategy):
    id = StrategyId.CREDIT_VERTICAL

    def __init__(self, config: CreditVerticalConfig) -> None:
        super().__init__(config)
        self.config: CreditVerticalConfig = config

    def evaluate(self, market: MarketState) -> tuple[list[Intent], list[dict[str, Any]]]:
        intents: list[Intent] = []
        decisions: list[dict[str, Any]] = []
        for underlying in self.config.underlyings:
            payload = self._evaluate_one(market, underlying)
            decisions.append(payload)
            intent = payload.pop("_intent", None)
            if payload["passed"] and intent is not None:
                intents.append(intent)
        return intents, decisions

    def _evaluate_one(self, market: MarketState, underlying: Underlying) -> dict[str, Any]:
        cfg = self.config
        if self.context.is_in_cooldown(market.now):
            return _decision(self.id, underlying, None, False, "cooldown_active", {})

        spot = market.spot(underlying)
        if spot is None:
            return _decision(self.id, underlying, None, False, "missing_spot", {})

        candles = market.candles(underlying, cfg.entry.timeframe.value)
        closes = np.array([c.close for c in candles], dtype=float)
        highs = np.array([c.high for c in candles], dtype=float)
        lows = np.array([c.low for c in candles], dtype=float)
        long_setup, short_setup, trend_features = evaluate_trend_breakout(closes, highs, lows, cfg.entry)
        if trend_features.get("error"):
            return _decision(
                self.id,
                underlying,
                None,
                False,
                str(trend_features["error"]),
                trend_features,
            )
        feature_vector: dict[str, Any] = {"spot": spot, **trend_features}
        if not (long_setup or short_setup):
            return _decision(self.id, underlying, None, False, "filter_failed", feature_vector)

        bucket = self._pick_expiry_bucket(market, underlying)
        wings = cfg.spread

        if long_setup:
            option_type = "put"
            short_sel = self.select_strike(
                market.chain,
                underlying,
                option_type,
                bucket,
                target_delta=wings.short_delta_target,
                delta_min=wings.short_delta_min,
                delta_max=wings.short_delta_max,
                now=market.now,
            )
            long_sel = self.select_strike(
                market.chain,
                underlying,
                option_type,
                bucket,
                target_delta=wings.long_delta_target,
                delta_min=wings.long_delta_min,
                delta_max=wings.long_delta_max,
                now=market.now,
            )
        else:
            option_type = "call"
            short_sel = self.select_strike(
                market.chain,
                underlying,
                option_type,
                bucket,
                target_delta=wings.short_delta_target,
                delta_min=wings.short_delta_min,
                delta_max=wings.short_delta_max,
                now=market.now,
            )
            long_sel = self.select_strike(
                market.chain,
                underlying,
                option_type,
                bucket,
                target_delta=wings.long_delta_target,
                delta_min=wings.long_delta_min,
                delta_max=wings.long_delta_max,
                now=market.now,
            )

        if not short_sel or not long_sel or short_sel.quote.mid is None or long_sel.quote.mid is None:
            return _decision(self.id, underlying, None, False, "spread_delta_unfillable", feature_vector)

        if option_type == "put":
            if long_sel.instrument.strike >= short_sel.instrument.strike:
                return _decision(self.id, underlying, None, False, "spread_delta_unfillable", feature_vector)
            width = short_sel.instrument.strike - long_sel.instrument.strike
            credit = float(short_sel.quote.mid - long_sel.quote.mid)
            legs = [
                LegIntent(
                    symbol=long_sel.instrument.symbol,
                    side="buy",
                    option_type="put",
                    strike=long_sel.instrument.strike,
                    expiry=long_sel.instrument.expiry,
                ),
                LegIntent(
                    symbol=short_sel.instrument.symbol,
                    side="sell",
                    option_type="put",
                    strike=short_sel.instrument.strike,
                    expiry=short_sel.instrument.expiry,
                ),
            ]
            spread_kind = "bull_put"
        else:
            if short_sel.instrument.strike >= long_sel.instrument.strike:
                return _decision(self.id, underlying, None, False, "spread_delta_unfillable", feature_vector)
            width = long_sel.instrument.strike - short_sel.instrument.strike
            credit = float(short_sel.quote.mid - long_sel.quote.mid)
            legs = [
                LegIntent(
                    symbol=short_sel.instrument.symbol,
                    side="sell",
                    option_type="call",
                    strike=short_sel.instrument.strike,
                    expiry=short_sel.instrument.expiry,
                ),
                LegIntent(
                    symbol=long_sel.instrument.symbol,
                    side="buy",
                    option_type="call",
                    strike=long_sel.instrument.strike,
                    expiry=long_sel.instrument.expiry,
                ),
            ]
            spread_kind = "bear_call"

        liquidity = check_multi_leg_liquidity(
            [("long", long_sel), ("short", short_sel)],
            min_open_interest=cfg.desk.min_open_interest,
            greeks_required=cfg.desk.greeks_required,
        )
        if liquidity.features:
            feature_vector.update(liquidity.features)
        if not liquidity.ok:
            return _decision(
                self.id,
                underlying,
                liquidity.symbol,
                False,
                liquidity.reason or "filter_failed",
                feature_vector,
            )

        max_loss = width - credit
        if credit <= 0 or max_loss <= 0 or credit < cfg.credit.min_credit_pct_of_width * width:
            return _decision(
                self.id,
                underlying,
                None,
                False,
                "credit_too_thin",
                {
                    **feature_vector,
                    "credit": credit,
                    "width": width,
                    "spread_kind": spread_kind,
                },
            )

        lot_size = short_sel.instrument.lot_size
        feature_vector.update(
            {
                "spread_kind": spread_kind,
                "credit": credit,
                "width": width,
                "max_loss": max_loss,
                "bucket": bucket.value,
            }
        )
        intent = Intent(
            strategy_id=self.id,
            underlying=underlying,
            bucket=bucket,
            legs=legs,
            requested_lots=cfg.max_lots_cap,
            rationale=f"credit_vertical_{spread_kind}",
            feature_vector=feature_vector,
            target_credit_inr=market.premium_inr(credit, lot_size=lot_size),
            target_max_loss_inr=market.premium_inr(max_loss, lot_size=lot_size),
        )
        payload = _decision(self.id, underlying, None, True, "passed", feature_vector)
        payload["_intent"] = intent
        return payload

    def _pick_expiry_bucket(self, market: MarketState, underlying: Underlying) -> ExpiryBucket:
        return self.config.expiry.prefer

    def manage(self, position: PositionState, market: MarketState) -> list[Action]:
        cfg = self.config.exits
        expiry_ist = utc_to_ist(position.expiry).date()
        now_ist = utc_to_ist(market.now).date()
        days_to_expiry = (expiry_ist - now_ist).days

        if days_to_expiry <= cfg.force_close_days_before_expiry:
            return [Action(kind=ActionType.CLOSE, close=CloseAction(reason=ExitTrigger.FORCE_CLOSE_EXPIRY))]

        if position.entry_credit_inr is None:
            return [Action(kind=ActionType.NO_OP)]
        entry_credit = float(position.entry_credit_inr)
        current_unwind_cost = float(position.notes.get("current_unwind_cost") or 0.0)
        pnl = entry_credit - current_unwind_cost
        if pnl >= cfg.profit_take_pct_of_credit * entry_credit:
            return [Action(kind=ActionType.CLOSE, close=CloseAction(reason=ExitTrigger.TARGET))]
        if current_unwind_cost >= cfg.stop_loss_x_credit * entry_credit:
            return [Action(kind=ActionType.CLOSE, close=CloseAction(reason=ExitTrigger.PREMIUM_STOP))]
        return [Action(kind=ActionType.NO_OP)]


def _decision(
    strategy_id: StrategyId,
    underlying: Underlying | None,
    symbol: str | None,
    passed: bool,
    reason: str,
    feature_vector: dict[str, Any],
) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id.value,
        "kind": "evaluate",
        "underlying": underlying.value if underlying is not None else None,
        "symbol": symbol,
        "passed": passed,
        "reason": reason,
        "feature_vector": feature_vector,
    }


__all__ = ["CreditVerticalStrategy"]

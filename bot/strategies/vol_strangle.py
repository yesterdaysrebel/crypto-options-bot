"""Strategy C — Vol-breakout long strangle.

Setup filter (per plan):
  - 15m + 1h candles
  - ATR(14, 15m) percentile <= cfg.setup.atr_percentile_max over the lookback window
  - 1h BB-width contraction (BBwidth at the 30th-percentile or lower of last 180 days)
  - 15m range compression: the last N bars' range is <= ratio * mean(last L bars' range)
  - Anti-revenge cooldown: at least cfg.setup.anti_revenge_hours since last vol_strangle exit

Strike selection: long call + long put with |delta| in [0.20, 0.30] target 0.25.
Expiry: prefer D2 (else W1).
Manage:
  - +50% premium gain     -> CloseAction(TARGET)
  - -50% premium loss      -> CloseAction(PREMIUM_STOP)
  - T-4h before expiry     -> CloseAction(FORCE_CLOSE_EXPIRY)
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from bot.config.models import (
    StrategyId,
    Underlying,
    VolStrangleConfig,
)
from bot.desk.leg_liquidity import check_multi_leg_liquidity
from bot.data.candles import atr, bollinger_width, percentile_rank
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


class VolStrangleStrategy(Strategy):
    id = StrategyId.VOL_STRANGLE

    def __init__(self, config: VolStrangleConfig) -> None:
        super().__init__(config)
        self.config: VolStrangleConfig = config

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
        spot = market.spot(underlying)
        if spot is None:
            return _decision(self.id, underlying, None, False, "missing_spot", {})

        candles_15m = market.candles(underlying, cfg.setup.timeframe_short.value)
        candles_1h = market.candles(underlying, cfg.setup.timeframe_long.value)

        min_15m = max(cfg.setup.atr_percentile_lookback, cfg.setup.range_compression_bars + 1)
        min_1h = cfg.setup.bbwidth_period + 24

        if len(candles_15m) < min_15m or len(candles_1h) < min_1h:
            return _decision(
                self.id,
                underlying,
                None,
                False,
                "insufficient_history",
                {"n_15m": len(candles_15m), "n_1h": len(candles_1h)},
            )

        highs_15 = np.array([c.high for c in candles_15m])
        lows_15 = np.array([c.low for c in candles_15m])
        closes_15 = np.array([c.close for c in candles_15m])
        atr_series = atr(highs_15, lows_15, closes_15, cfg.setup.atr_period)
        latest_atr = atr_series[-1]
        if math.isnan(latest_atr):
            return _decision(self.id, underlying, None, False, "atr_not_ready", {})
        atr_window = atr_series[-cfg.setup.atr_percentile_lookback :]
        atr_window = atr_window[np.isfinite(atr_window)]
        atr_pct = percentile_rank(float(latest_atr), atr_window) if atr_window.size > 0 else math.nan

        closes_1h = np.array([c.close for c in candles_1h])
        bbw = bollinger_width(closes_1h, cfg.setup.bbwidth_period, cfg.setup.bbwidth_std)
        bbw_finite = bbw[np.isfinite(bbw)]
        if bbw_finite.size == 0:
            return _decision(self.id, underlying, None, False, "atr_not_ready", {})
        bbw_pct = percentile_rank(float(bbw[-1]), bbw_finite)

        ranges = highs_15 - lows_15
        recent_range_mean = float(ranges[-cfg.setup.range_compression_bars :].mean())
        lookback_range_mean = float(ranges[-cfg.setup.range_compression_lookback_days * 96 :].mean())
        range_compressed = (
            lookback_range_mean > 0
            and recent_range_mean <= cfg.setup.range_compression_ratio * lookback_range_mean
        )

        if self.context.is_in_cooldown(market.now):
            return _decision(
                self.id,
                underlying,
                None,
                False,
                "anti_revenge_block",
                {"cooldown_until": str(self.context.cooldown_until)},
            )

        feature_vector: dict[str, Any] = {
            "spot": spot,
            "atr": float(latest_atr),
            "atr_pct": float(atr_pct) if np.isfinite(atr_pct) else None,
            "bbw_pct": bbw_pct,
            "range_recent": recent_range_mean,
            "range_long_mean": lookback_range_mean,
            "range_compressed": range_compressed,
        }

        if not (
            np.isfinite(atr_pct)
            and atr_pct <= cfg.setup.atr_percentile_max
            and bbw_pct <= 0.30
            and range_compressed
        ):
            return _decision(self.id, underlying, None, False, "filter_failed", feature_vector)

        bucket = cfg.expiry.prefer
        call_sel = self.select_strike(
            market.chain,
            underlying,
            "call",
            bucket,
            target_delta=cfg.strike.call_delta_target,
            delta_min=cfg.strike.call_delta_min,
            delta_max=cfg.strike.call_delta_max,
            now=market.now,
        )
        put_sel = self.select_strike(
            market.chain,
            underlying,
            "put",
            bucket,
            target_delta=cfg.strike.put_delta_target,
            delta_min=cfg.strike.put_delta_min,
            delta_max=cfg.strike.put_delta_max,
            now=market.now,
        )
        if call_sel is None or put_sel is None or call_sel.quote.mid is None or put_sel.quote.mid is None:
            bucket = cfg.expiry.fallback
            call_sel = self.select_strike(
                market.chain,
                underlying,
                "call",
                bucket,
                target_delta=cfg.strike.call_delta_target,
                delta_min=cfg.strike.call_delta_min,
                delta_max=cfg.strike.call_delta_max,
                now=market.now,
            )
            put_sel = self.select_strike(
                market.chain,
                underlying,
                "put",
                bucket,
                target_delta=cfg.strike.put_delta_target,
                delta_min=cfg.strike.put_delta_min,
                delta_max=cfg.strike.put_delta_max,
                now=market.now,
            )
        if call_sel is None or put_sel is None or call_sel.quote.mid is None or put_sel.quote.mid is None:
            return _decision(self.id, underlying, None, False, "no_acceptable_strike", feature_vector)

        liquidity = check_multi_leg_liquidity(
            [("call", call_sel), ("put", put_sel)],
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

        total_premium = float(call_sel.quote.mid + put_sel.quote.mid)
        legs = [
            LegIntent(
                symbol=call_sel.instrument.symbol,
                side="buy",
                option_type="call",
                strike=call_sel.instrument.strike,
                expiry=call_sel.instrument.expiry,
            ),
            LegIntent(
                symbol=put_sel.instrument.symbol,
                side="buy",
                option_type="put",
                strike=put_sel.instrument.strike,
                expiry=put_sel.instrument.expiry,
            ),
        ]

        intent = Intent(
            strategy_id=self.id,
            underlying=underlying,
            bucket=bucket,
            legs=legs,
            requested_lots=cfg.max_lots_cap,
            rationale="vol_breakout_long_strangle",
            feature_vector={**feature_vector, "total_premium": total_premium},
            target_premium_inr=market.premium_inr(total_premium),
        )
        payload = _decision(
            self.id, underlying, None, True, "passed", {**feature_vector, "total_premium": total_premium}
        )
        payload["_intent"] = intent
        return payload

    def manage(self, position: PositionState, market: MarketState) -> list[Action]:
        cfg = self.config.exits
        hours_to_expiry = (position.expiry - market.now).total_seconds() / 3600.0
        if hours_to_expiry <= cfg.force_close_hours_before_expiry:
            return [Action(kind=ActionType.CLOSE, close=CloseAction(reason=ExitTrigger.FORCE_CLOSE_EXPIRY))]

        if position.entry_premium_inr is None:
            return [Action(kind=ActionType.NO_OP)]

        current = float(position.notes.get("current_total_premium") or 0.0)
        if current <= 0:
            return [Action(kind=ActionType.NO_OP)]
        entry = float(position.entry_premium_inr)

        if current >= (1 + cfg.profit_take_pct_of_premium) * entry:
            return [Action(kind=ActionType.CLOSE, close=CloseAction(reason=ExitTrigger.TARGET))]
        if current <= (1 - cfg.stop_loss_pct_of_premium) * entry:
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


__all__ = ["VolStrangleStrategy"]

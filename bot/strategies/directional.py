"""Strategy A — Directional long-premium.

Entry filter (per plan):
  - 15m candles
  - EMA9 vs EMA21 separated by >= 0.25 * ATR(14) and pointing in the same direction
  - latest 15m bar closes beyond the 4-bar high/low + 0.25 * ATR
  - chosen contract's spread <= 8% of mid (configurable, default 0.08)
  - DTE >= cfg.expiry.min_dte_hours

Strike selection: primary ATM, fallback ATM+1 (call) / ATM-1 (put) on spread/quote failure.
Expiry: prefer D1 when hours-to-close >= cfg.expiry.d1_dte_threshold_hours, else D2.
Sizing: capped lots = cfg.max_lots_cap; final sizing in PR #12 risk module.

Manage signals (PR #13 builds the full per-strategy exit engine on top of these):
  - target hit  -> CloseAction(TARGET)
  - premium drawdown breach -> CloseAction(PREMIUM_STOP)
  - underlying-ATR stop breach -> CloseAction(UNDERLYING_STOP)
  - within force-close window -> CloseAction(FORCE_CLOSE_EXPIRY)
  - once +1R peak reached: move stop to break-even -> TrailAction(stop=entry)
"""

from __future__ import annotations

from typing import Any

import numpy as np

from bot.config.models import (
    DirectionalConfig,
    ExpiryBucket,
    StrategyId,
    Underlying,
)
from bot.data.candles import atr, ema
from bot.risk.window import india_options_session_close_utc
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
    TrailAction,
)

DEFAULT_SPREAD_PCT_MAX = 0.08


class DirectionalStrategy(Strategy):
    id = StrategyId.DIRECTIONAL

    def __init__(self, config: DirectionalConfig, *, spread_pct_max: float = DEFAULT_SPREAD_PCT_MAX) -> None:
        super().__init__(config)
        self.config: DirectionalConfig = config
        self._spread_pct_max = spread_pct_max

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
        if self.context.is_underlying_in_cooldown(underlying.value, market.now):
            return _decision(
                self.id,
                underlying,
                None,
                False,
                "cooldown_active",
                {"cooldown_underlying": underlying.value},
            )
        spot = market.spot(underlying)
        if spot is None:
            return _decision(self.id, underlying, None, False, "missing_spot", {})

        candles = market.candles(underlying, cfg.entry.timeframe.value)
        min_bars = max(cfg.entry.ema_slow, cfg.entry.atr_period + 1, cfg.entry.prior_bars + 2)
        if len(candles) < min_bars:
            return _decision(
                self.id,
                underlying,
                None,
                False,
                "insufficient_history",
                {"n_candles": len(candles), "min_required": min_bars},
            )

        closes = np.array([c.close for c in candles], dtype=float)
        highs = np.array([c.high for c in candles], dtype=float)
        lows = np.array([c.low for c in candles], dtype=float)
        ema_fast = ema(closes, cfg.entry.ema_fast)
        ema_slow = ema(closes, cfg.entry.ema_slow)
        atr_series = atr(highs, lows, closes, cfg.entry.atr_period)
        latest_atr = float(atr_series[-1])
        if not np.isfinite(latest_atr):
            return _decision(self.id, underlying, None, False, "atr_not_ready", {})

        latest_close = float(closes[-1])
        ema_sep = float(ema_fast[-1] - ema_slow[-1])
        threshold = cfg.entry.breakout_atr_mult * latest_atr
        prior_high = float(highs[-(cfg.entry.prior_bars + 1) : -1].max())
        prior_low = float(lows[-(cfg.entry.prior_bars + 1) : -1].min())

        long_setup = ema_sep > threshold and latest_close > prior_high + threshold
        short_setup = ema_sep < -threshold and latest_close < prior_low - threshold

        feature_vector: dict[str, Any] = {
            "spot": spot,
            "latest_close": latest_close,
            "ema_fast": float(ema_fast[-1]),
            "ema_slow": float(ema_slow[-1]),
            "ema_sep": ema_sep,
            "atr": latest_atr,
            "threshold": threshold,
            "prior_high": prior_high,
            "prior_low": prior_low,
        }

        if not (long_setup or short_setup):
            return _decision(self.id, underlying, None, False, "filter_failed", feature_vector)

        option_type = "call" if long_setup else "put"
        bucket = self._pick_expiry_bucket(market)

        selection = self.select_strike(
            market.chain,
            underlying,
            option_type,
            bucket,
            spot_price=spot,
            atm_offset=0,
            now=market.now,
        )
        if selection is None or selection.quote.mid is None:
            selection = self.select_strike(
                market.chain,
                underlying,
                option_type,
                bucket,
                spot_price=spot,
                atm_offset=1,
                now=market.now,
            )
        if selection is None or selection.quote.mid is None:
            return _decision(self.id, underlying, None, False, "no_acceptable_strike", feature_vector)

        spread = selection.quote.spread_pct
        if spread is not None and spread > self._spread_pct_max:
            return _decision(
                self.id,
                underlying,
                selection.instrument.symbol,
                False,
                "spread_too_wide",
                {**feature_vector, "spread_pct": spread},
            )

        intent = Intent(
            strategy_id=self.id,
            underlying=underlying,
            bucket=bucket,
            legs=[
                LegIntent(
                    symbol=selection.instrument.symbol,
                    side="buy",
                    option_type=option_type,
                    strike=selection.instrument.strike,
                    expiry=selection.instrument.expiry,
                )
            ],
            requested_lots=cfg.max_lots_cap,
            rationale=f"directional_{option_type}_atr_breakout",
            feature_vector={**feature_vector, "spread_pct": spread, "mid": selection.quote.mid},
            target_premium_inr=market.premium_inr(selection.quote.mid),
            spread_pct_max=self._spread_pct_max,
        )

        payload = _decision(
            self.id,
            underlying,
            selection.instrument.symbol,
            True,
            "passed",
            {**feature_vector, "spread_pct": spread, "mid": selection.quote.mid},
        )
        payload["_intent"] = intent
        return payload

    def _pick_expiry_bucket(self, market: MarketState) -> ExpiryBucket:
        cfg = self.config.expiry
        now = market.now
        same_day_close = india_options_session_close_utc(now)
        hours_to_d1_close = (same_day_close - now).total_seconds() / 3600.0
        if hours_to_d1_close >= cfg.d1_dte_threshold_hours:
            return cfg.prefer
        return cfg.fallback

    def manage(self, position: PositionState, market: MarketState) -> list[Action]:
        cfg = self.config.exits
        now = market.now
        out: list[Action] = []

        hours_to_expiry = (position.expiry - now).total_seconds() / 3600.0
        if hours_to_expiry <= cfg.force_close_hours_before_expiry:
            return [Action(kind=ActionType.CLOSE, close=CloseAction(reason=ExitTrigger.FORCE_CLOSE_EXPIRY))]

        if not position.leg_states or position.entry_premium_inr is None:
            return [Action(kind=ActionType.NO_OP)]

        leg = position.leg_states[0]
        current_mid = leg.get("current_mid")
        if current_mid is None:
            return [Action(kind=ActionType.NO_OP)]

        entry = float(position.entry_premium_inr)
        mid = float(current_mid)

        if mid >= (1 + cfg.target_r) * entry:
            return [Action(kind=ActionType.CLOSE, close=CloseAction(reason=ExitTrigger.TARGET))]

        if mid <= (1 - cfg.premium_drawdown_pct) * entry:
            return [Action(kind=ActionType.CLOSE, close=CloseAction(reason=ExitTrigger.PREMIUM_STOP))]

        trail_stop = position.current_trail_stop_price
        if trail_stop is not None and mid <= float(trail_stop):
            return [
                Action(
                    kind=ActionType.CLOSE,
                    close=CloseAction(reason=ExitTrigger.TRAIL_BREAKEVEN),
                )
            ]

        if position.entry_underlying_price is not None and position.entry_atr is not None:
            spot_now = market.spot(position.underlying)
            if spot_now is not None and mid <= entry:
                long_side = leg.get("option_type") == "call"
                adverse_move = (
                    (position.entry_underlying_price - spot_now)
                    if long_side
                    else (spot_now - position.entry_underlying_price)
                )
                if adverse_move >= cfg.underlying_atr_mult_stop * position.entry_atr:
                    return [
                        Action(kind=ActionType.CLOSE, close=CloseAction(reason=ExitTrigger.UNDERLYING_STOP))
                    ]

        risk_per_lot = entry * max(position.lots, 1)
        if (
            position.peak_pnl_inr is not None
            and position.peak_pnl_inr >= cfg.trail_breakeven_at_r * risk_per_lot
            and position.current_trail_stop_price is None
        ):
            out.append(Action(kind=ActionType.TRAIL_STOP, trail=TrailAction(new_stop_price=entry)))

        if not out:
            out.append(Action(kind=ActionType.NO_OP))
        return out


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


__all__ = ["DirectionalStrategy"]

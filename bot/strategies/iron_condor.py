"""Strategy B — Weekly iron condor.

Entry filter:
  - Friday opens (cfg.entry.weekday == 'friday'), within +/- 30 min of cfg.entry.open_time_ist
  - All four legs must be priceable from quotes (delta + bid/ask)
  - Credit received >= cfg.credit.min_credit_pct_of_width * width
  - Per-strategy concurrency cap is checked in PR #12 risk module (not here)

Strike selection (delta-based, per plan):
  - short call @ |delta| close to cfg.wings.short_delta_target (in [min,max] band)
  - long call  @ |delta| close to cfg.wings.long_delta_target
  - short put  @ |delta| close to cfg.wings.short_delta_target
  - long put   @ |delta| close to cfg.wings.long_delta_target

Manage:
  - profit_take_pct_of_credit -> CloseAction(TARGET)
  - stop_loss_x_credit       -> CloseAction(PREMIUM_STOP)
  - tested-side cut (one side hit short strike): CloseAction(TESTED_SIDE_CUT)
  - T-2 force close          -> CloseAction(FORCE_CLOSE_EXPIRY)
"""

from __future__ import annotations

from typing import Any

from bot.config.models import (
    ExpiryBucket,
    IronCondorConfig,
    StrategyId,
    Underlying,
)
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


class IronCondorStrategy(Strategy):
    id = StrategyId.IRON_CONDOR

    def __init__(self, config: IronCondorConfig) -> None:
        super().__init__(config)
        self.config: IronCondorConfig = config

    def evaluate(self, market: MarketState) -> tuple[list[Intent], list[dict[str, Any]]]:
        decisions: list[dict[str, Any]] = []
        intents: list[Intent] = []

        if market.now.weekday() != 4:
            decisions.append(
                _decision(self.id, None, None, False, "filter_failed", {"weekday": market.now.weekday()})
            )
            return intents, decisions

        open_at = self.config.entry.open_time
        now_time = market.now.time().replace(microsecond=0)
        if not _within_minutes(now_time, open_at, minutes=30):
            decisions.append(
                _decision(
                    self.id, None, None, False, "filter_failed", {"now": str(now_time), "want": str(open_at)}
                )
            )
            return intents, decisions

        if self.context.is_in_cooldown(market.now):
            decisions.append(_decision(self.id, None, None, False, "cooldown_active", {}))
            return intents, decisions

        for underlying in self.config.underlyings:
            decisions.append(self._evaluate_one(market, underlying, intents))
        return intents, decisions

    def _evaluate_one(
        self,
        market: MarketState,
        underlying: Underlying,
        out_intents: list[Intent],
    ) -> dict[str, Any]:
        cfg = self.config
        spot = market.spot(underlying)
        if spot is None:
            return _decision(self.id, underlying, None, False, "missing_spot", {})

        bucket = ExpiryBucket.W1

        short_call = self.select_strike(
            market.chain,
            underlying,
            "call",
            bucket,
            target_delta=cfg.wings.short_delta_target,
            delta_min=cfg.wings.short_delta_min,
            delta_max=cfg.wings.short_delta_max,
            now=market.now,
        )
        long_call = self.select_strike(
            market.chain,
            underlying,
            "call",
            bucket,
            target_delta=cfg.wings.long_delta_target,
            delta_min=cfg.wings.long_delta_min,
            delta_max=cfg.wings.long_delta_max,
            now=market.now,
        )
        short_put = self.select_strike(
            market.chain,
            underlying,
            "put",
            bucket,
            target_delta=cfg.wings.short_delta_target,
            delta_min=cfg.wings.short_delta_min,
            delta_max=cfg.wings.short_delta_max,
            now=market.now,
        )
        long_put = self.select_strike(
            market.chain,
            underlying,
            "put",
            bucket,
            target_delta=cfg.wings.long_delta_target,
            delta_min=cfg.wings.long_delta_min,
            delta_max=cfg.wings.long_delta_max,
            now=market.now,
        )

        if not all([short_call, long_call, short_put, long_put]):
            return _decision(
                self.id,
                underlying,
                None,
                False,
                "condor_delta_band_unfillable",
                {"spot": spot},
            )

        assert short_call is not None and long_call is not None
        assert short_put is not None and long_put is not None

        if not (
            long_put.instrument.strike
            < short_put.instrument.strike
            < short_call.instrument.strike
            < long_call.instrument.strike
        ):
            return _decision(
                self.id,
                underlying,
                None,
                False,
                "condor_delta_band_unfillable",
                {
                    "long_put": long_put.instrument.strike,
                    "short_put": short_put.instrument.strike,
                    "short_call": short_call.instrument.strike,
                    "long_call": long_call.instrument.strike,
                },
            )

        for s in (short_call, long_call, short_put, long_put):
            if s.quote.mid is None:
                return _decision(
                    self.id, underlying, s.instrument.symbol, False, "condor_delta_band_unfillable", {}
                )

        credit = (
            (short_call.quote.mid or 0)
            + (short_put.quote.mid or 0)
            - ((long_call.quote.mid or 0) + (long_put.quote.mid or 0))
        )

        call_width = long_call.instrument.strike - short_call.instrument.strike
        put_width = short_put.instrument.strike - long_put.instrument.strike
        max_width = max(call_width, put_width)
        max_loss = max_width - credit

        if credit <= 0 or credit < cfg.credit.min_credit_pct_of_width * max_width:
            return _decision(
                self.id,
                underlying,
                None,
                False,
                "credit_too_thin",
                {
                    "credit": credit,
                    "max_width": max_width,
                    "min_required": cfg.credit.min_credit_pct_of_width * max_width,
                },
            )

        legs = [
            LegIntent(
                symbol=long_put.instrument.symbol,
                side="buy",
                option_type="put",
                strike=long_put.instrument.strike,
                expiry=long_put.instrument.expiry,
            ),
            LegIntent(
                symbol=short_put.instrument.symbol,
                side="sell",
                option_type="put",
                strike=short_put.instrument.strike,
                expiry=short_put.instrument.expiry,
            ),
            LegIntent(
                symbol=short_call.instrument.symbol,
                side="sell",
                option_type="call",
                strike=short_call.instrument.strike,
                expiry=short_call.instrument.expiry,
            ),
            LegIntent(
                symbol=long_call.instrument.symbol,
                side="buy",
                option_type="call",
                strike=long_call.instrument.strike,
                expiry=long_call.instrument.expiry,
            ),
        ]

        feature_vector = {
            "spot": spot,
            "long_put_strike": long_put.instrument.strike,
            "short_put_strike": short_put.instrument.strike,
            "short_call_strike": short_call.instrument.strike,
            "long_call_strike": long_call.instrument.strike,
            "credit": credit,
            "max_width": max_width,
            "max_loss": max_loss,
            "short_call_delta": short_call.quote.delta,
            "short_put_delta": short_put.quote.delta,
        }

        intent = Intent(
            strategy_id=self.id,
            underlying=underlying,
            bucket=bucket,
            legs=legs,
            requested_lots=cfg.max_lots_cap,
            rationale="iron_condor_weekly",
            feature_vector=feature_vector,
            target_credit_inr=credit,
            target_max_loss_inr=max_loss,
        )
        out_intents.append(intent)
        return _decision(self.id, underlying, None, True, "passed", feature_vector)

    def manage(self, position: PositionState, market: MarketState) -> list[Action]:
        cfg = self.config.exits
        days_to_expiry = (position.expiry.date() - market.now.date()).days

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

        if cfg.tested_side_cut:
            spot_now = market.spot(position.underlying)
            short_call_strike = position.notes.get("short_call_strike")
            short_put_strike = position.notes.get("short_put_strike")
            if spot_now is not None:
                if short_call_strike is not None and spot_now >= float(short_call_strike):
                    return [
                        Action(
                            kind=ActionType.CLOSE,
                            close=CloseAction(reason=ExitTrigger.TESTED_SIDE_CUT, notes={"side": "call"}),
                        )
                    ]
                if short_put_strike is not None and spot_now <= float(short_put_strike):
                    return [
                        Action(
                            kind=ActionType.CLOSE,
                            close=CloseAction(reason=ExitTrigger.TESTED_SIDE_CUT, notes={"side": "put"}),
                        )
                    ]

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


def _within_minutes(a, b, *, minutes: int) -> bool:
    import datetime as dt

    today = dt.date(2000, 1, 1)
    delta = abs((dt.datetime.combine(today, a) - dt.datetime.combine(today, b)).total_seconds())
    return delta <= minutes * 60


__all__ = ["IronCondorStrategy"]

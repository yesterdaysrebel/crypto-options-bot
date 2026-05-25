"""Stateful exit engine that wraps a strategy's `manage()` per position.

Responsibilities:
    * Track peak/trough PnL per position (used by the directional strategy's trail logic).
    * Throttle TRAIL_STOP emissions so we don't re-submit a near-identical stop every tick.
    * Apply per-strategy specifics that don't belong inside the strategy class:
        - Directional: chandelier trail = peak_underlying_price - cfg.trail_chandelier_atr_mult * ATR
        - Condor: refresh current_unwind_cost from leg quotes before calling manage().
        - Strangle: refresh current_total_premium from leg quotes before calling manage().
    * Convert strategy Actions into wire-ready ExitDirectives keyed by trade_id.

The engine is pure — orders are dispatched by the caller (execution router in PR #14).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bot.config.models import (
    CreditVerticalConfig,
    DirectionalConfig,
    LongStraddleConfig,
)
from bot.strategies.base import (
    Action,
    ActionType,
    CloseAction,
    ExitTrigger,
    MarketState,
    PositionState,
    Strategy,
)
from bot.strategies.registry import StrategyRegistry


class ExitKind(StrEnum):
    CLOSE = "close"
    UPDATE_STOP = "update_stop"
    NO_OP = "no_op"


@dataclass
class PositionRuntime:
    """Engine-owned per-position state. Persistence is the caller's concern."""

    position: PositionState
    peak_pnl_inr: float = 0.0
    trough_pnl_inr: float = 0.0
    peak_underlying: float | None = None
    trough_underlying: float | None = None
    last_trail_stop: float | None = None
    last_trail_update_ts: dt.datetime | None = None
    history_ticks: int = 0


@dataclass(frozen=True)
class ExitDirective:
    trade_id: int
    kind: ExitKind
    trigger: ExitTrigger | None = None
    new_stop_price: float | None = None
    leg_states: list[dict[str, Any]] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)


class ExitEngine:
    """Drives strategies' `manage()` per position with engine-owned bookkeeping."""

    def __init__(
        self,
        registry: StrategyRegistry,
        *,
        trail_update_throttle_seconds: float = 5.0,
    ) -> None:
        self._registry = registry
        self._throttle = trail_update_throttle_seconds

    def step(self, runtime: PositionRuntime, market: MarketState) -> list[ExitDirective]:
        position = runtime.position
        runtime.history_ticks += 1
        strategy = self._registry.get(position.strategy_id)

        self._refresh_position_state(position, market, strategy, runtime)

        actions = strategy.manage(position, market)
        directives = self._translate_actions(actions, position, market, runtime)
        return directives

    def _refresh_position_state(
        self,
        position: PositionState,
        market: MarketState,
        strategy: Strategy,
        runtime: PositionRuntime,
    ) -> None:
        """Strategy-specific bookkeeping before delegating to manage()."""
        cfg = strategy.config

        if isinstance(cfg, DirectionalConfig):
            leg = position.leg_states[0] if position.leg_states else None
            if leg is not None:
                quote = market.quote_for.get(leg["symbol"])
                if quote is not None and quote.mid is not None:
                    leg["current_mid"] = quote.mid
                    if position.entry_premium_inr is not None:
                        pnl = (quote.mid - position.entry_premium_inr) * position.lots
                        runtime.peak_pnl_inr = max(runtime.peak_pnl_inr, pnl)
                        runtime.trough_pnl_inr = min(runtime.trough_pnl_inr, pnl)
                        position.peak_pnl_inr = runtime.peak_pnl_inr
            spot = market.spot(position.underlying)
            if spot is not None:
                runtime.peak_underlying = (
                    spot if runtime.peak_underlying is None else max(runtime.peak_underlying, spot)
                )
                runtime.trough_underlying = (
                    spot if runtime.trough_underlying is None else min(runtime.trough_underlying, spot)
                )
            position.current_trail_stop_price = runtime.last_trail_stop

        elif isinstance(cfg, CreditVerticalConfig):
            # Cost to close: pay to buy back shorts; receive from selling longs.
            #   unwind_cost = sum(short_mids_now) - sum(long_mids_now)
            unwind_cost = 0.0
            for leg in position.leg_states:
                quote = market.quote_for.get(leg["symbol"])
                if quote is None or quote.mid is None:
                    return
                side = leg.get("side", "buy")
                sign = 1.0 if side == "sell" else -1.0
                unwind_cost += sign * quote.mid
            position.notes["current_unwind_cost"] = unwind_cost

        elif isinstance(cfg, LongStraddleConfig):
            total = 0.0
            for leg in position.leg_states:
                quote = market.quote_for.get(leg["symbol"])
                if quote is None or quote.mid is None:
                    return
                total += quote.mid
            position.notes["current_total_premium"] = total

    def _translate_actions(
        self,
        actions: list[Action],
        position: PositionState,
        market: MarketState,
        runtime: PositionRuntime,
    ) -> list[ExitDirective]:
        directives: list[ExitDirective] = []
        for action in actions:
            if action.kind == ActionType.CLOSE:
                close = action.close or CloseAction(reason=ExitTrigger.PREMIUM_STOP)
                directives.append(
                    ExitDirective(
                        trade_id=position.trade_id,
                        kind=ExitKind.CLOSE,
                        trigger=close.reason,
                        leg_states=list(position.leg_states),
                        notes=dict(close.notes),
                    )
                )
            elif action.kind == ActionType.TRAIL_STOP and action.trail is not None:
                new_stop = self._maybe_apply_chandelier(action.trail.new_stop_price, position, runtime)
                if self._should_emit_trail(position, runtime, new_stop, market.now):
                    runtime.last_trail_stop = new_stop
                    runtime.last_trail_update_ts = market.now
                    directives.append(
                        ExitDirective(
                            trade_id=position.trade_id,
                            kind=ExitKind.UPDATE_STOP,
                            new_stop_price=new_stop,
                            notes=dict(action.trail.notes),
                        )
                    )
            else:
                directives.append(ExitDirective(trade_id=position.trade_id, kind=ExitKind.NO_OP))
        return directives

    def _maybe_apply_chandelier(
        self,
        candidate: float,
        position: PositionState,
        runtime: PositionRuntime,
    ) -> float:
        """For directional positions, use the higher of (strategy proposal, chandelier from peak)."""
        strategy_cfg = self._registry.get(position.strategy_id).config
        if not isinstance(strategy_cfg, DirectionalConfig):
            return candidate
        if position.entry_atr is None or runtime.peak_underlying is None:
            return candidate
        leg = position.leg_states[0] if position.leg_states else None
        if leg is None:
            return candidate
        is_long_call = leg.get("option_type") == "call"
        atr_mult = strategy_cfg.exits.trail_chandelier_atr_mult
        if is_long_call:
            chandelier = runtime.peak_underlying - atr_mult * position.entry_atr
            return max(candidate, chandelier)
        chandelier = (runtime.trough_underlying or runtime.peak_underlying) + atr_mult * position.entry_atr
        return min(candidate, chandelier)

    def _should_emit_trail(
        self,
        position: PositionState,
        runtime: PositionRuntime,
        new_stop: float,
        now: dt.datetime,
    ) -> bool:
        if runtime.last_trail_stop is None:
            return True
        if runtime.last_trail_update_ts is not None:
            elapsed = (now - runtime.last_trail_update_ts).total_seconds()
            if elapsed < self._throttle:
                return False
        leg = position.leg_states[0] if position.leg_states else {}
        is_call = str(leg.get("option_type", "")).lower() == "call"
        if is_call:
            return new_stop > runtime.last_trail_stop + 1e-6
        return new_stop < runtime.last_trail_stop - 1e-6

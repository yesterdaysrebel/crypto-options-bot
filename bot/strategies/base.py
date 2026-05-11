"""Strategy interface, intent/action dataclasses, and shared `MarketState` container.

`Intent`s describe what a strategy *wants* to do (entry). The risk module then sizes the
intent, the execution router places orders. `Action`s describe what to do with an existing
position (adjust stop, trail, close).
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bot.config.models import (
    DirectionalConfig,
    ExpiryBucket,
    IronCondorConfig,
    StrategyConfig,
    StrategyId,
    Underlying,
    VolStrangleConfig,
)
from bot.data.candles import Candle
from bot.data.chain_cache import ChainCache, QuoteSnapshot, StrikeSelection


class ActionType(StrEnum):
    CLOSE = "close"
    TRAIL_STOP = "trail_stop"
    MOVE_STOP = "move_stop"
    NO_OP = "no_op"


class ExitTrigger(StrEnum):
    TARGET = "target"
    PREMIUM_STOP = "premium_stop"
    UNDERLYING_STOP = "underlying_stop"
    TRAIL_CHANDELIER = "trail_chandelier"
    TRAIL_BREAKEVEN = "trail_breakeven"
    TIME_STOP = "time_stop"
    FORCE_CLOSE_EXPIRY = "force_close_expiry"
    TESTED_SIDE_CUT = "tested_side_cut"


@dataclass(frozen=True)
class LegIntent:
    """One leg of a multi-leg intent. For 1-leg strategies, intent.legs has length 1."""

    symbol: str
    side: str  # "buy" / "sell"
    option_type: str  # "call" / "put"
    strike: float
    expiry: dt.datetime


@dataclass
class Intent:
    """Strategy's expression of intent. Risk module will size + validate; router places orders."""

    strategy_id: StrategyId
    underlying: Underlying
    bucket: ExpiryBucket
    legs: list[LegIntent]
    requested_lots: int
    rationale: str
    feature_vector: dict[str, Any] = field(default_factory=dict)
    target_premium_inr: float | None = None
    target_credit_inr: float | None = None
    target_max_loss_inr: float | None = None
    spread_pct_max: float = 0.08


@dataclass
class CloseAction:
    reason: ExitTrigger
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class TrailAction:
    new_stop_price: float
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class Action:
    """Result of `Strategy.manage`. `kind` discriminates which fields are populated."""

    kind: ActionType
    close: CloseAction | None = None
    trail: TrailAction | None = None


@dataclass
class PositionState:
    """Strategy-agnostic view of an open position the strategy may need to manage."""

    trade_id: int
    strategy_id: StrategyId
    underlying: Underlying
    expiry: dt.datetime
    lots: int
    entry_ts: dt.datetime
    entry_premium_inr: float | None = None
    entry_credit_inr: float | None = None
    entry_underlying_price: float | None = None
    entry_atr: float | None = None
    current_stop_price: float | None = None
    current_trail_stop_price: float | None = None
    peak_pnl_inr: float | None = None
    leg_states: list[dict[str, Any]] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketState:
    """Snapshot of what the strategy needs at evaluation time.

    Strategies read indicators/candles from `candles_by_tf` and call `chain.get_strike_by_delta`
    or `chain.get_atm_strike` to pick contracts. Underlying spot/mark for `underlying` is in
    `underlying_marks`.
    """

    now: dt.datetime
    chain: ChainCache
    candles_by_tf: dict[Underlying, dict[str, list[Candle]]]
    underlying_marks: dict[Underlying, float]
    quote_for: dict[str, QuoteSnapshot] = field(default_factory=dict)

    def candles(self, underlying: Underlying, timeframe: str) -> list[Candle]:
        return self.candles_by_tf.get(underlying, {}).get(timeframe, [])

    def spot(self, underlying: Underlying) -> float | None:
        return self.underlying_marks.get(underlying)


@dataclass
class StrategyContext:
    """Per-strategy runtime context: config + last-evaluation timestamps + cooldown bookkeeping."""

    config: StrategyConfig
    last_evaluate_ts: dt.datetime | None = None
    cooldown_until: dt.datetime | None = None
    notes: dict[str, Any] = field(default_factory=dict)

    def is_in_cooldown(self, now: dt.datetime) -> bool:
        return self.cooldown_until is not None and now < self.cooldown_until


class Strategy(ABC):
    """Abstract base for every strategy."""

    id: StrategyId

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.context = StrategyContext(config=config)

    @abstractmethod
    def evaluate(self, market: MarketState) -> tuple[list[Intent], list[dict[str, Any]]]:
        """Return (intents, decision_records).

        `intents` is the list of entry intents the strategy proposes this tick (usually 0 or 1).
        `decision_records` is the per-tick audit trail (passed/failed + feature vector + reason)
        to be inserted into the `decisions` table.
        """

    @abstractmethod
    def manage(
        self,
        position: PositionState,
        market: MarketState,
    ) -> list[Action]:
        """Return management actions (close / trail / no-op) for an existing position."""

    def select_strike(
        self,
        chain: ChainCache,
        underlying: Underlying,
        option_type: str,
        bucket: ExpiryBucket,
        *,
        target_delta: float | None = None,
        delta_min: float | None = None,
        delta_max: float | None = None,
        spot_price: float | None = None,
        atm_offset: int = 0,
        now: dt.datetime | None = None,
    ) -> StrikeSelection | None:
        """Convenience: route to delta picker if target_delta is set else ATM picker."""
        if target_delta is not None:
            return chain.get_strike_by_delta(
                Underlying(underlying.value if isinstance(underlying, Underlying) else underlying),
                option_type,  # type: ignore[arg-type]
                bucket,
                target_delta,
                delta_min=delta_min,
                delta_max=delta_max,
                now=now,
            )
        if spot_price is None:
            return None
        return chain.get_atm_strike(
            underlying,
            option_type,  # type: ignore[arg-type]
            bucket,
            spot_price,
            offset=atm_offset,
            now=now,
        )


__all__ = [
    "Action",
    "ActionType",
    "CloseAction",
    "DirectionalConfig",
    "ExitTrigger",
    "Intent",
    "IronCondorConfig",
    "LegIntent",
    "MarketState",
    "PositionState",
    "Strategy",
    "StrategyContext",
    "TrailAction",
    "VolStrangleConfig",
]

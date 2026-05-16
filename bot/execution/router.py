"""Execution router interface + shared dataclasses.

The router presents a single `submit_entry(EntryRequest)` and `submit_exit(ExitRequest)` API
to the main loop. Concrete backends (DryExecutor / LiveExecutor) implement the actual
order placement. Both backends are responsible for:

  * Maker-first entries: post-only LIMIT at mid (Delta India's tick-rounded bid for buys,
    rounded ask for sells), with a fallback to IOC marketable-limit after `maker_timeout_s`.
  * Atomic multi-leg helper: submit all legs as a group. If any leg fills only partially or
    rejects, roll back already-filled legs by submitting cancel + reduce-only close orders.
  * Reduce-only STOP-MARKET exits for trailing stops and target/SL.
  * Idempotent client_order_ids: re-submitting the same ID is safe (the wire layer dedupes).
"""

from __future__ import annotations

import abc
import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bot.config.models import StrategyId, Underlying
from bot.strategies.base import ExitTrigger, LegIntent


class LegSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OrderTicket:
    """One leg of an entry submission."""

    client_order_id: str
    symbol: str
    side: LegSide
    qty: float
    limit_price: float | None
    stop_price: float | None = None
    post_only: bool = True
    reduce_only: bool = False
    purpose: str = "entry"
    leg_idx: int = 0


@dataclass(frozen=True)
class LegFill:
    """Result of placing one leg. Populated by the executor."""

    symbol: str
    side: LegSide
    qty_requested: float
    qty_filled: float
    avg_fill_price: float | None
    leg_idx: int
    client_order_id: str
    exchange_order_id: int | None = None
    state: str = "filled"
    slippage_bps: float | None = None
    raw_response: dict[str, Any] | None = None

    @property
    def is_complete(self) -> bool:
        return self.state == "filled" and self.qty_filled >= self.qty_requested - 1e-9


@dataclass
class EntryRequest:
    strategy_id: StrategyId
    trade_id: int
    underlying: Underlying
    legs: list[LegIntent]
    lots: int
    intent_rationale: str
    spread_pct_max: float = 0.08
    maker_timeout_seconds: float = 30.0
    slip_bps_budget: int = 50


@dataclass
class EntryResult:
    success: bool
    trade_id: int
    fills: list[LegFill] = field(default_factory=list)
    error: str | None = None
    rollback_actions: list[str] = field(default_factory=list)
    submitted_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None

    @property
    def total_premium_inr(self) -> float:
        """Net premium for the whole request (per-lot fill price x lots, summed over legs)."""
        total = 0.0
        for f in self.fills:
            if f.avg_fill_price is None:
                continue
            sign = 1.0 if f.side == LegSide.BUY else -1.0
            total += sign * f.avg_fill_price * f.qty_filled
        return total


@dataclass
class ExitRequest:
    strategy_id: StrategyId
    trade_id: int
    underlying: Underlying
    legs: list[LegIntent]
    lots: int
    trigger: ExitTrigger
    stop_price: float | None = None


@dataclass
class ExitResult:
    success: bool
    trade_id: int
    fills: list[LegFill] = field(default_factory=list)
    error: str | None = None
    completed_at: dt.datetime | None = None


class ExecutionRouter(abc.ABC):
    """Common interface for the live / dry executors."""

    @abc.abstractmethod
    async def submit_entry(self, req: EntryRequest) -> EntryResult: ...

    @abc.abstractmethod
    async def submit_exit(self, req: ExitRequest) -> ExitResult: ...

    @abc.abstractmethod
    async def update_stop(
        self,
        trade_id: int,
        symbol: str,
        side: LegSide,
        qty: float,
        new_stop_price: float,
        client_order_id: str,
    ) -> dict[str, Any]: ...

    @abc.abstractmethod
    async def cancel_all_for_trade(self, trade_id: int) -> int: ...

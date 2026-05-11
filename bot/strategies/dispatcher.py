"""Strategy dispatcher: per-tick fan-out of evaluate/manage with timing + error isolation.

The dispatcher is intentionally side-effect-light: it does NOT persist decisions or place
orders. Its caller (the main loop) takes the returned (intents, decisions, actions) and
hands them to the risk module, decision-log writer, and execution router respectively.

Errors raised by one strategy are caught and recorded so the others continue evaluating;
this prevents one buggy strategy from blocking the others on a shared tick.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from bot.config.models import StrategyId
from bot.strategies.base import Action, Intent, MarketState, PositionState
from bot.strategies.registry import StrategyRegistry


@dataclass
class DispatchResult:
    intents_by_strategy: dict[StrategyId, list[Intent]] = field(default_factory=dict)
    decisions_by_strategy: dict[StrategyId, list[dict[str, Any]]] = field(default_factory=dict)
    actions_by_position: dict[int, list[Action]] = field(default_factory=dict)
    eval_time_ms: dict[StrategyId, float] = field(default_factory=dict)
    errors: dict[StrategyId, str] = field(default_factory=dict)

    @property
    def all_intents(self) -> list[Intent]:
        return [i for intents in self.intents_by_strategy.values() for i in intents]

    @property
    def all_decisions(self) -> list[dict[str, Any]]:
        return [d for decisions in self.decisions_by_strategy.values() for d in decisions]


class StrategyDispatcher:
    def __init__(self, registry: StrategyRegistry) -> None:
        self._registry = registry

    def evaluate_all(
        self,
        market: MarketState,
        *,
        only: Iterable[StrategyId] | None = None,
    ) -> DispatchResult:
        out = DispatchResult()
        wanted = set(only) if only is not None else None
        for strategy in self._registry.enabled():
            if wanted is not None and strategy.id not in wanted:
                continue
            t0 = time.perf_counter()
            try:
                intents, decisions = strategy.evaluate(market)
                strategy.context.last_evaluate_ts = market.now
            except Exception as exc:
                logger.exception("strategy {} raised in evaluate; isolated this tick", strategy.id.value)
                out.errors[strategy.id] = repr(exc)
                continue
            out.intents_by_strategy[strategy.id] = list(intents)
            out.decisions_by_strategy[strategy.id] = list(decisions)
            out.eval_time_ms[strategy.id] = (time.perf_counter() - t0) * 1000.0
        return out

    def manage_all(
        self,
        positions: Iterable[PositionState],
        market: MarketState,
        result: DispatchResult | None = None,
    ) -> DispatchResult:
        out = result if result is not None else DispatchResult()
        for position in positions:
            try:
                strategy = self._registry.get(position.strategy_id)
            except KeyError:
                logger.warning(
                    "no registered strategy for open position trade_id={} strategy={}",
                    position.trade_id,
                    position.strategy_id,
                )
                continue
            try:
                actions = strategy.manage(position, market)
            except Exception as exc:
                logger.exception(
                    "strategy {} raised in manage; isolated this tick", position.strategy_id.value
                )
                out.errors[position.strategy_id] = repr(exc)
                continue
            out.actions_by_position[position.trade_id] = list(actions)
        return out

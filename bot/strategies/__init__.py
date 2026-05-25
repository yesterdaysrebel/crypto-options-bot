"""Strategy interface + registry + dispatcher.

Every strategy implements `Strategy.evaluate(market_state) -> list[Intent]` (entry) and
`Strategy.manage(position, tick) -> list[Action]` (exit/adjust). The registry routes
ticks to enabled strategies and enforces per-strategy risk-budget allocation.
"""

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
    StrategyContext,
    TrailAction,
)
from bot.strategies.directional import DirectionalStrategy
from bot.strategies.dispatcher import StrategyDispatcher
from bot.strategies.credit_vertical import CreditVerticalStrategy
from bot.strategies.long_straddle import LongStraddleStrategy
from bot.strategies.registry import StrategyRegistry

__all__ = [
    "Action",
    "ActionType",
    "CloseAction",
    "DirectionalStrategy",
    "ExitTrigger",
    "Intent",
    "CreditVerticalStrategy",
    "LegIntent",
    "MarketState",
    "PositionState",
    "Strategy",
    "StrategyContext",
    "StrategyDispatcher",
    "StrategyRegistry",
    "TrailAction",
    "LongStraddleStrategy",
]

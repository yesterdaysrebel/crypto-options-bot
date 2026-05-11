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
from bot.strategies.iron_condor import IronCondorStrategy
from bot.strategies.registry import StrategyRegistry
from bot.strategies.vol_strangle import VolStrangleStrategy

__all__ = [
    "Action",
    "ActionType",
    "CloseAction",
    "DirectionalStrategy",
    "ExitTrigger",
    "Intent",
    "IronCondorStrategy",
    "LegIntent",
    "MarketState",
    "PositionState",
    "Strategy",
    "StrategyContext",
    "StrategyDispatcher",
    "StrategyRegistry",
    "TrailAction",
    "VolStrangleStrategy",
]

"""Strategy registry. Holds the Strategy instances for the current process and
distributes per-strategy risk budget shares of NAV based on `risk_weight`.
"""

from __future__ import annotations

from collections.abc import Iterable

from bot.config.models import StrategyConfig, StrategyId
from bot.strategies.base import Strategy


class StrategyRegistry:
    def __init__(self, strategies: Iterable[Strategy]) -> None:
        self._by_id: dict[StrategyId, Strategy] = {}
        for s in strategies:
            if s.id in self._by_id:
                raise ValueError(f"duplicate strategy registered: {s.id}")
            self._by_id[s.id] = s

    def __contains__(self, sid: StrategyId | str) -> bool:
        key = sid if isinstance(sid, StrategyId) else StrategyId(sid)
        return key in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)

    @property
    def all_ids(self) -> list[StrategyId]:
        return list(self._by_id.keys())

    def get(self, sid: StrategyId | str) -> Strategy:
        key = sid if isinstance(sid, StrategyId) else StrategyId(sid)
        return self._by_id[key]

    def enabled(self) -> list[Strategy]:
        return [s for s in self._by_id.values() if s.config.enabled]

    def all(self) -> list[Strategy]:
        return list(self._by_id.values())

    def configs(self) -> list[StrategyConfig]:
        return [s.config for s in self._by_id.values()]

    def risk_budget_inr(self, total_nav_inr: float, sid: StrategyId | str) -> float:
        """Return the absolute INR risk budget allocated to a strategy this tick.

        risk_budget = NAV * risk_weight * risk_per_trade_pct
        Strategies use this as the *cap* on a single trade's risk-INR (R, max-loss, or premium).
        """
        s = self.get(sid)
        return total_nav_inr * s.config.risk_weight * s.config.risk_per_trade_pct

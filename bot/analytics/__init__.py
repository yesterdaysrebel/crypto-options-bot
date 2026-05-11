"""Analytics subsystem.

Contains the decision-log writer (PR #16), daily aggregator + report (PR #17), and
per-trade journal generator (PR #18). These modules are deliberately decoupled from the
trading loop: they consume DB rows and emit Markdown / Prometheus artefacts.
"""

from bot.analytics.daily import (
    GLOBAL_KEY,
    DailyAggregator,
    DailyReport,
    NavSnapshot,
    StrategyDailyStats,
)
from bot.analytics.decision_log import DecisionLogWriter
from bot.analytics.journal import TradeJournal

__all__ = [
    "GLOBAL_KEY",
    "DailyAggregator",
    "DailyReport",
    "DecisionLogWriter",
    "NavSnapshot",
    "StrategyDailyStats",
    "TradeJournal",
]

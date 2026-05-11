"""Main process: Delta data feeds, strategies, dry/live execution hooks, persistence, reports."""

from __future__ import annotations

from bot.runtime.engine import run_trading_engine


async def run() -> None:
    """Run the full trading engine (metrics + WS + REST + dispatch + DB + nightly rollup)."""
    await run_trading_engine()

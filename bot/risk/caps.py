"""Three-tier loss caps: daily (-3% NAV), weekly (-6% NAV), lifetime peak-to-trough (-15% NAV).

The lifetime cap is the circuit breaker: once tripped, all entries are blocked and the
operator must run `make resume --confirm` to clear it.

This module is *pure*: callers (RiskManager) thread in the most recent daily/weekly PnL totals
and NAV history. The persistence-layer hook lives in PR #17 (analytics + nav_history rollup).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class CapStatus(StrEnum):
    OK = "ok"
    DAILY_TRIPPED = "daily_tripped"
    WEEKLY_TRIPPED = "weekly_tripped"
    CIRCUIT_BREAKER = "circuit_breaker"


@dataclass(frozen=True)
class LossCapResult:
    status: CapStatus
    daily_pnl_pct: float
    weekly_pnl_pct: float
    drawdown_from_peak_pct: float

    @property
    def trading_allowed(self) -> bool:
        return self.status == CapStatus.OK


@dataclass
class DrawdownCaps:
    daily_loss_pct: float
    weekly_loss_pct: float
    lifetime_dd_pct: float

    def evaluate(
        self,
        *,
        nav_now: float,
        nav_open_today: float,
        nav_open_week: float,
        peak_nav: float,
        circuit_breaker_tripped: bool,
    ) -> LossCapResult:
        if peak_nav <= 0:
            peak_nav = nav_now
        daily_pct = (nav_now - nav_open_today) / nav_open_today if nav_open_today > 0 else 0.0
        weekly_pct = (nav_now - nav_open_week) / nav_open_week if nav_open_week > 0 else 0.0
        dd = (nav_now - peak_nav) / peak_nav
        if circuit_breaker_tripped or dd <= -self.lifetime_dd_pct:
            return LossCapResult(
                status=CapStatus.CIRCUIT_BREAKER,
                daily_pnl_pct=daily_pct,
                weekly_pnl_pct=weekly_pct,
                drawdown_from_peak_pct=dd,
            )
        if weekly_pct <= -self.weekly_loss_pct:
            return LossCapResult(
                status=CapStatus.WEEKLY_TRIPPED,
                daily_pnl_pct=daily_pct,
                weekly_pnl_pct=weekly_pct,
                drawdown_from_peak_pct=dd,
            )
        if daily_pct <= -self.daily_loss_pct:
            return LossCapResult(
                status=CapStatus.DAILY_TRIPPED,
                daily_pnl_pct=daily_pct,
                weekly_pnl_pct=weekly_pct,
                drawdown_from_peak_pct=dd,
            )
        return LossCapResult(
            status=CapStatus.OK,
            daily_pnl_pct=daily_pct,
            weekly_pnl_pct=weekly_pct,
            drawdown_from_peak_pct=dd,
        )


@dataclass
class NavTracker:
    """Tracks rolling NAV state used by the cap evaluator.

    The bot writes `nav_now` continuously (every tick worth >= a small threshold) and
    `nav_open_today` once per trading-day boundary; `peak_nav` is monotonically increased.

    Persistence to the `nav_history` table is handled in PR #17.
    """

    nav_now: float
    nav_open_today: float
    nav_open_week: float
    peak_nav: float
    circuit_breaker_tripped: bool = False
    last_update_ts: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None))

    def update_nav(self, new_nav: float, *, now: dt.datetime | None = None) -> None:
        self.nav_now = float(new_nav)
        self.peak_nav = max(self.peak_nav, self.nav_now)
        self.last_update_ts = now or self.last_update_ts

    def roll_day(self, now: dt.datetime) -> None:
        self.nav_open_today = self.nav_now
        if now.weekday() == 0:
            self.nav_open_week = self.nav_now
        self.last_update_ts = now

    def trip_breaker(self, *, sentinel_path: Path | None = None) -> None:
        self.circuit_breaker_tripped = True
        if sentinel_path is not None:
            sentinel_path.write_text(f"tripped at {self.last_update_ts.isoformat()}\n")

    def clear_breaker(self, *, sentinel_path: Path | None = None) -> None:
        self.circuit_breaker_tripped = False
        if sentinel_path is not None and sentinel_path.exists():
            sentinel_path.unlink()

"""NAV bootstrap, IST day rolls, and circuit-breaker persistence."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy import func, select

from bot.risk.caps import CapStatus, NavTracker
from bot.risk.manager import RiskManager
from bot.risk.window import utc_to_ist
from bot.storage.db import Database
from bot.storage.models import NavHistory, Trade, TradeStatus


async def sum_realised_pnl_inr(db: Database) -> float:
    async with db.session() as session:
        total = await session.scalar(
            select(func.coalesce(func.sum(Trade.realised_pnl_inr), 0.0)).where(
                Trade.status == TradeStatus.CLOSED.value
            )
        )
    return float(total or 0.0)


async def load_nav_tracker(db: Database, *, base_nav_inr: float) -> NavTracker:
    """Rebuild in-memory NAV from starting capital + closed-trade PnL and `nav_history` metadata."""
    realised = await sum_realised_pnl_inr(db)
    nav_now = float(base_nav_inr) + realised
    today_ist = utc_to_ist(dt.datetime.now(dt.UTC).replace(tzinfo=None)).date()

    async with db.session() as session:
        latest = (
            await session.execute(select(NavHistory).order_by(NavHistory.trading_date.desc()).limit(1))
        ).scalar_one_or_none()
        row_today = (
            await session.execute(select(NavHistory).where(NavHistory.trading_date == today_ist))
        ).scalar_one_or_none()
        yesterday = today_ist - dt.timedelta(days=1)
        row_yesterday = (
            await session.execute(select(NavHistory).where(NavHistory.trading_date == yesterday))
        ).scalar_one_or_none()

    peak = float(nav_now)
    breaker = False
    if latest is not None:
        peak = max(peak, float(latest.peak_nav_inr))
        breaker = bool(latest.circuit_breaker_tripped)

    if row_today is not None:
        nav_open_today = float(row_today.nav_inr)
    elif row_yesterday is not None:
        nav_open_today = float(row_yesterday.nav_inr)
    else:
        nav_open_today = nav_now

    week_start = today_ist - dt.timedelta(days=today_ist.weekday())
    nav_open_week = nav_open_today
    if latest is not None and latest.trading_date >= week_start:
        nav_open_week = float(latest.nav_inr)

    return NavTracker(
        nav_now=nav_now,
        nav_open_today=nav_open_today,
        nav_open_week=nav_open_week,
        peak_nav=peak,
        circuit_breaker_tripped=breaker,
    )


def maybe_roll_ist_trading_day(nav: NavTracker, now_utc: dt.datetime, last_ist_date: dt.date | None) -> dt.date:
    """Advance daily/weekly open NAV anchors at the IST calendar boundary."""
    today = utc_to_ist(now_utc).date()
    if last_ist_date is not None and today > last_ist_date:
        nav.roll_day(now_utc)
    return today


async def persist_circuit_breaker_trip(
    db: Database,
    nav: NavTracker,
    *,
    runtime_dir: Path,
    now_utc: dt.datetime,
) -> None:
    """Persist breaker flag to DB + runtime marker (survives restart)."""
    trading_date = utc_to_ist(now_utc).date()
    dd = (nav.nav_now - nav.peak_nav) / nav.peak_nav if nav.peak_nav > 0 else 0.0
    async with db.session() as session:
        row = (
            await session.execute(select(NavHistory).where(NavHistory.trading_date == trading_date))
        ).scalar_one_or_none()
        if row is None:
            row = NavHistory(
                trading_date=trading_date,
                nav_inr=float(nav.nav_now),
                peak_nav_inr=float(nav.peak_nav),
                drawdown_from_peak_pct=float(dd),
                circuit_breaker_tripped=True,
            )
            session.add(row)
        else:
            row.nav_inr = float(nav.nav_now)
            row.peak_nav_inr = max(float(row.peak_nav_inr), float(nav.peak_nav))
            row.drawdown_from_peak_pct = float(dd)
            row.circuit_breaker_tripped = True

    runtime_dir.mkdir(parents=True, exist_ok=True)
    marker = runtime_dir / "circuit_breaker.json"
    nav.trip_breaker(sentinel_path=marker)
    payload = {
        "tripped_at": now_utc.isoformat(),
        "nav_inr": nav.nav_now,
        "peak_nav_inr": nav.peak_nav,
        "drawdown_pct": dd,
    }
    marker.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


async def sync_circuit_breaker_from_risk(
    db: Database,
    nav: NavTracker,
    risk: RiskManager,
    *,
    runtime_dir: Path,
    now_utc: dt.datetime,
) -> None:
    """Trip and persist when lifetime drawdown cap fires."""
    if nav.circuit_breaker_tripped:
        return
    cap = risk.evaluate_caps()
    if cap.status != CapStatus.CIRCUIT_BREAKER:
        return
    await persist_circuit_breaker_trip(db, nav, runtime_dir=runtime_dir, now_utc=now_utc)


__all__ = [
    "load_nav_tracker",
    "maybe_roll_ist_trading_day",
    "persist_circuit_breaker_trip",
    "sum_realised_pnl_inr",
    "sync_circuit_breaker_from_risk",
]

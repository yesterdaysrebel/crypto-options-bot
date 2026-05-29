"""NAV bootstrap and circuit-breaker persistence."""

from __future__ import annotations

import datetime as dt

import pytest
from bot.risk.caps import NavTracker
from bot.risk.window import IST, india_options_session_close_utc, utc_to_ist
from bot.runtime.nav_state import load_nav_tracker, maybe_roll_ist_trading_day
from bot.storage.models import NavHistory, Trade, TradeStatus


def test_india_options_session_close_utc_before_close() -> None:
    # 10:00 IST = 04:30 UTC on 2026-05-15
    now = dt.datetime(2026, 5, 15, 4, 30, 0)
    close = india_options_session_close_utc(now)
    assert utc_to_ist(close).hour == 17
    assert utc_to_ist(close).minute == 30
    assert close > now


def test_india_options_session_close_utc_after_close_rolls_to_next_day() -> None:
    # 18:00 IST = 12:30 UTC
    now = dt.datetime(2026, 5, 15, 12, 30, 0)
    close = india_options_session_close_utc(now)
    assert utc_to_ist(close).date() == dt.date(2026, 5, 16)


@pytest.mark.asyncio
async def test_load_nav_tracker_includes_realised_pnl(db) -> None:
    async with db.session() as session:
        session.add(
            Trade(
                strategy_id="directional",
                underlying="BTC",
                lots=1,
                status=TradeStatus.CLOSED.value,
                realised_pnl_inr=-500.0,
            )
        )
        session.add(
            NavHistory(
                trading_date=dt.date(2026, 5, 14),
                nav_inr=49_500.0,
                peak_nav_inr=50_000.0,
                drawdown_from_peak_pct=-0.01,
                circuit_breaker_tripped=False,
            )
        )

    nav = await load_nav_tracker(db, base_nav_inr=50_000.0)
    assert nav.nav_now == pytest.approx(49_500.0)
    assert nav.peak_nav == pytest.approx(50_000.0)
    assert nav.nav_open_today == pytest.approx(49_500.0)


def test_maybe_roll_ist_trading_day_at_midnight() -> None:
    nav = NavTracker(
        nav_now=48_000.0,
        nav_open_today=50_000.0,
        nav_open_week=50_000.0,
        peak_nav=50_000.0,
    )
    day1 = dt.date(2026, 5, 15)
    day2 = dt.date(2026, 5, 16)
    now = dt.datetime(2026, 5, 15, 18, 35, 0, tzinfo=IST).astimezone(dt.UTC).replace(tzinfo=None)
    last = maybe_roll_ist_trading_day(nav, now, day1)
    assert last == day1
    now2 = dt.datetime(2026, 5, 16, 4, 0, 0)
    last2 = maybe_roll_ist_trading_day(nav, now2, last)
    assert last2 == day2
    assert nav.nav_open_today == pytest.approx(48_000.0)

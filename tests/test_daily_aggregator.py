"""Tests for the nightly daily aggregator + Markdown report."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from bot.analytics.daily import GLOBAL_KEY, DailyAggregator
from bot.storage import DailyPnl, Database, NavHistory, Trade, TradeStatus, init_database
from sqlalchemy import select


@pytest.fixture
async def db() -> Database:
    return await init_database(":memory:")


async def _seed_trades(db: Database, trading_date: dt.date) -> None:
    base = dt.datetime.combine(trading_date, dt.time(15, 0))
    async with db.session() as session:
        session.add_all(
            [
                Trade(
                    strategy_id="directional",
                    underlying="BTC",
                    entry_ts=base - dt.timedelta(hours=2),
                    exit_ts=base,
                    status=TradeStatus.CLOSED.value,
                    lots=1,
                    realised_pnl_inr=600.0,
                    fees_inr=20.0,
                    delta_pnl_inr=500.0,
                    theta_pnl_inr=100.0,
                    r_multiple=2.0,
                    slippage_bps=15.0,
                ),
                Trade(
                    strategy_id="directional",
                    underlying="BTC",
                    entry_ts=base - dt.timedelta(hours=2),
                    exit_ts=base,
                    status=TradeStatus.CLOSED.value,
                    lots=1,
                    realised_pnl_inr=-200.0,
                    fees_inr=20.0,
                    delta_pnl_inr=-200.0,
                    theta_pnl_inr=0.0,
                    r_multiple=-1.0,
                    slippage_bps=25.0,
                ),
                Trade(
                    strategy_id="credit_vertical",
                    underlying="BTC",
                    entry_ts=base - dt.timedelta(hours=2),
                    exit_ts=base,
                    status=TradeStatus.CLOSED.value,
                    lots=1,
                    realised_pnl_inr=400.0,
                    fees_inr=40.0,
                    delta_pnl_inr=0.0,
                    theta_pnl_inr=400.0,
                    r_multiple=0.5,
                    slippage_bps=80.0,
                ),
                Trade(
                    strategy_id="long_straddle",
                    underlying="BTC",
                    entry_ts=base - dt.timedelta(hours=2),
                    exit_ts=base + dt.timedelta(days=1),  # excluded — not today
                    status=TradeStatus.CLOSED.value,
                    lots=1,
                    realised_pnl_inr=99999.0,
                    fees_inr=0.0,
                ),
            ]
        )


@pytest.mark.asyncio
async def test_aggregator_produces_per_strategy_and_global_rows(db: Database, tmp_path: Path) -> None:
    today = dt.date(2026, 5, 14)
    await _seed_trades(db, today)
    agg = DailyAggregator(db, reports_dir=tmp_path)
    report = await agg.run(
        trading_date=today,
        nav_inr=50_800.0,
        peak_nav_inr=51_200.0,
        circuit_breaker_tripped=False,
    )
    by_sid = {s.strategy_id: s for s in report.per_strategy}
    assert set(by_sid) == {GLOBAL_KEY, "directional", "credit_vertical"}
    g = by_sid[GLOBAL_KEY]
    assert g.n_trades == 3
    assert g.n_wins == 2
    assert g.n_losses == 1
    assert g.gross_pnl_inr == 600 - 200 + 400
    assert g.net_pnl_inr == g.gross_pnl_inr - (20 + 20 + 40)
    assert g.win_rate is not None and abs(g.win_rate - 2 / 3) < 1e-9


@pytest.mark.asyncio
async def test_aggregator_upserts_daily_pnl_and_nav(db: Database, tmp_path: Path) -> None:
    today = dt.date(2026, 5, 15)
    await _seed_trades(db, today)
    agg = DailyAggregator(db, reports_dir=tmp_path)
    await agg.run(trading_date=today, nav_inr=49_500.0, peak_nav_inr=50_000.0, circuit_breaker_tripped=False)
    async with db.session() as session:
        daily_rows = (
            (await session.execute(select(DailyPnl).where(DailyPnl.trading_date == today))).scalars().all()
        )
        nav_row = (
            await session.execute(select(NavHistory).where(NavHistory.trading_date == today))
        ).scalar_one()
    assert {r.strategy_id for r in daily_rows} == {GLOBAL_KEY, "directional", "credit_vertical"}
    assert nav_row.drawdown_from_peak_pct == pytest.approx(1.0)
    assert nav_row.circuit_breaker_tripped is False
    # Re-run is idempotent: should not create dups, should update in place.
    await agg.run(trading_date=today, nav_inr=49_000.0, peak_nav_inr=50_000.0, circuit_breaker_tripped=True)
    async with db.session() as session:
        nav_row2 = (
            await session.execute(select(NavHistory).where(NavHistory.trading_date == today))
        ).scalar_one()
    assert nav_row2.nav_inr == 49_000.0
    assert nav_row2.circuit_breaker_tripped is True


@pytest.mark.asyncio
async def test_aggregator_writes_markdown_report(db: Database, tmp_path: Path) -> None:
    today = dt.date(2026, 5, 16)
    await _seed_trades(db, today)
    agg = DailyAggregator(db, reports_dir=tmp_path)
    await agg.run(trading_date=today, nav_inr=51_200.0, peak_nav_inr=51_200.0, circuit_breaker_tripped=False)
    report_md = (tmp_path / f"{today.isoformat()}.md").read_text(encoding="utf-8")
    assert "# Daily Report" in report_md
    assert "directional" in report_md
    assert "credit_vertical" in report_md
    assert GLOBAL_KEY in report_md
    assert "Circuit breaker: OK" in report_md


@pytest.mark.asyncio
async def test_aggregator_handles_no_trades_for_day(db: Database, tmp_path: Path) -> None:
    today = dt.date(2026, 5, 17)
    agg = DailyAggregator(db, reports_dir=tmp_path)
    report = await agg.run(
        trading_date=today,
        nav_inr=50_000.0,
        peak_nav_inr=50_000.0,
        circuit_breaker_tripped=False,
    )
    assert len(report.per_strategy) == 1
    assert report.per_strategy[0].strategy_id == GLOBAL_KEY
    assert report.per_strategy[0].n_trades == 0
    md = (tmp_path / f"{today.isoformat()}.md").read_text(encoding="utf-8")
    assert "Trades" in md

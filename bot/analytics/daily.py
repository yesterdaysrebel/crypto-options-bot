"""Daily aggregator + Markdown report generator.

Nightly job (called by the main loop after the 22:00 IST window closes):

  1. For each strategy_id (including a `__global__` synthetic key) compute the day's
     trade stats from `trades` rows that closed today: n_trades, n_wins, n_losses,
     gross/net PnL, fees, delta/theta PnL splits, avg R, win-rate, avg slippage.
  2. Upsert the per-strategy stats into `daily_pnl` (PK trading_date+strategy_id).
  3. Update `nav_history`: append a row with today's NAV, peak NAV, drawdown-from-peak,
     and whether the circuit breaker is currently tripped.
  4. Emit a Markdown report `reports/<YYYY-MM-DD>.md` summarising the above.

This module is pure-ish: it reads from the DB and writes to (DB + filesystem). It does
NOT make trading decisions.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from bot.storage.db import Database
from bot.storage.models import DailyPnl, NavHistory, Trade, TradeStatus

GLOBAL_KEY = "__global__"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class StrategyDailyStats:
    strategy_id: str
    n_trades: int
    n_wins: int
    n_losses: int
    gross_pnl_inr: float
    net_pnl_inr: float
    fees_inr: float
    delta_pnl_inr: float
    theta_pnl_inr: float
    avg_r: float | None
    win_rate: float | None
    avg_slippage_bps: float | None


@dataclass(frozen=True)
class NavSnapshot:
    trading_date: dt.date
    nav_inr: float
    peak_nav_inr: float
    drawdown_from_peak_pct: float
    circuit_breaker_tripped: bool


@dataclass(frozen=True)
class DailyReport:
    trading_date: dt.date
    nav: NavSnapshot
    per_strategy: list[StrategyDailyStats]


class DailyAggregator:
    def __init__(self, db: Database, *, reports_dir: Path) -> None:
        self._db = db
        self._reports_dir = reports_dir
        self._reports_dir.mkdir(parents=True, exist_ok=True)

    async def run(
        self,
        *,
        trading_date: dt.date,
        nav_inr: float,
        peak_nav_inr: float,
        circuit_breaker_tripped: bool,
    ) -> DailyReport:
        per_strategy = await self._compute_per_strategy(trading_date)
        await self._persist_daily_pnl(trading_date, per_strategy)
        nav = await self._persist_nav(
            trading_date=trading_date,
            nav_inr=nav_inr,
            peak_nav_inr=peak_nav_inr,
            circuit_breaker_tripped=circuit_breaker_tripped,
        )
        report = DailyReport(trading_date=trading_date, nav=nav, per_strategy=per_strategy)
        self._write_markdown(report)
        return report

    async def _compute_per_strategy(self, trading_date: dt.date) -> list[StrategyDailyStats]:
        async with self._db.session() as session:
            start = dt.datetime.combine(trading_date, dt.time.min)
            end = start + dt.timedelta(days=1)
            stmt = (
                select(Trade)
                .where(Trade.status == TradeStatus.CLOSED.value)
                .where(Trade.exit_ts.isnot(None))
                .where(Trade.exit_ts >= start, Trade.exit_ts < end)
            )
            trades = list((await session.execute(stmt)).scalars().all())

        per_strategy: dict[str, list[Trade]] = {}
        for t in trades:
            per_strategy.setdefault(t.strategy_id, []).append(t)
        global_stats = _summarise(GLOBAL_KEY, trades)
        out = [global_stats]
        for sid, items in sorted(per_strategy.items()):
            out.append(_summarise(sid, items))
        return out

    async def _persist_daily_pnl(self, trading_date: dt.date, per_strategy: list[StrategyDailyStats]) -> None:
        if not per_strategy:
            return
        async with self._db.session() as session:
            existing_stmt = select(DailyPnl).where(DailyPnl.trading_date == trading_date)
            existing = {
                row.strategy_id: row for row in (await session.execute(existing_stmt)).scalars().all()
            }
            for stats in per_strategy:
                row = existing.get(stats.strategy_id)
                if row is None:
                    row = DailyPnl(trading_date=trading_date, strategy_id=stats.strategy_id)
                    session.add(row)
                row.n_trades = stats.n_trades
                row.n_wins = stats.n_wins
                row.n_losses = stats.n_losses
                row.gross_pnl_inr = stats.gross_pnl_inr
                row.net_pnl_inr = stats.net_pnl_inr
                row.fees_inr = stats.fees_inr
                row.delta_pnl_inr = stats.delta_pnl_inr
                row.theta_pnl_inr = stats.theta_pnl_inr
                row.avg_r = stats.avg_r
                row.win_rate = stats.win_rate
                row.avg_slippage_bps = stats.avg_slippage_bps

    async def _persist_nav(
        self,
        *,
        trading_date: dt.date,
        nav_inr: float,
        peak_nav_inr: float,
        circuit_breaker_tripped: bool,
    ) -> NavSnapshot:
        dd = 0.0
        if peak_nav_inr > 0:
            dd = max(0.0, (peak_nav_inr - nav_inr) / peak_nav_inr * 100.0)
        async with self._db.session() as session:
            existing = (
                await session.execute(select(NavHistory).where(NavHistory.trading_date == trading_date))
            ).scalar_one_or_none()
            if existing is None:
                existing = NavHistory(trading_date=trading_date, nav_inr=nav_inr, peak_nav_inr=peak_nav_inr)
                session.add(existing)
            existing.nav_inr = nav_inr
            existing.peak_nav_inr = peak_nav_inr
            existing.drawdown_from_peak_pct = dd
            existing.circuit_breaker_tripped = circuit_breaker_tripped
        return NavSnapshot(
            trading_date=trading_date,
            nav_inr=nav_inr,
            peak_nav_inr=peak_nav_inr,
            drawdown_from_peak_pct=dd,
            circuit_breaker_tripped=circuit_breaker_tripped,
        )

    def _write_markdown(self, report: DailyReport) -> Path:
        path = self._reports_dir / f"{report.trading_date.isoformat()}.md"
        lines: list[str] = [
            f"# Daily Report — {report.trading_date.isoformat()}",
            "",
            "## NAV",
            f"- NAV: ₹{report.nav.nav_inr:,.2f}",
            f"- Peak NAV: ₹{report.nav.peak_nav_inr:,.2f}",
            f"- Drawdown from peak: {report.nav.drawdown_from_peak_pct:.2f}%",
            f"- Circuit breaker: {'TRIPPED' if report.nav.circuit_breaker_tripped else 'OK'}",
            "",
            "## Strategy Summary",
            "",
            "| Strategy | Trades | Wins | Losses | Win % | Gross ₹ | Net ₹ | Fees ₹ | Avg R | Slip (bps) |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for s in report.per_strategy:
            lines.append(
                "| {sid} | {nt} | {w} | {l} | {wr} | {g:,.0f} | {n:,.0f} | {f:,.0f} | {r} | {slip} |".format(
                    sid=s.strategy_id,
                    nt=s.n_trades,
                    w=s.n_wins,
                    l=s.n_losses,
                    wr=f"{s.win_rate * 100:.1f}" if s.win_rate is not None else "—",
                    g=s.gross_pnl_inr,
                    n=s.net_pnl_inr,
                    f=s.fees_inr,
                    r=f"{s.avg_r:.2f}" if s.avg_r is not None else "—",
                    slip=f"{s.avg_slippage_bps:.1f}" if s.avg_slippage_bps is not None else "—",
                )
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


def _summarise(strategy_id: str, trades: list[Trade]) -> StrategyDailyStats:
    if not trades:
        return StrategyDailyStats(
            strategy_id=strategy_id,
            n_trades=0,
            n_wins=0,
            n_losses=0,
            gross_pnl_inr=0.0,
            net_pnl_inr=0.0,
            fees_inr=0.0,
            delta_pnl_inr=0.0,
            theta_pnl_inr=0.0,
            avg_r=None,
            win_rate=None,
            avg_slippage_bps=None,
        )
    realised = [float(t.realised_pnl_inr or 0.0) for t in trades]
    fees = [float(t.fees_inr or 0.0) for t in trades]
    wins = [r for r in realised if r > 0]
    losses = [r for r in realised if r < 0]
    n = len(trades)
    win_rate = len(wins) / n if n else None
    r_values = [t.r_multiple for t in trades if t.r_multiple is not None]
    avg_r = statistics.fmean(r_values) if r_values else None
    slips = [t.slippage_bps for t in trades if t.slippage_bps is not None]
    avg_slip = statistics.fmean(slips) if slips else None
    return StrategyDailyStats(
        strategy_id=strategy_id,
        n_trades=n,
        n_wins=len(wins),
        n_losses=len(losses),
        gross_pnl_inr=sum(realised),
        net_pnl_inr=sum(realised) - sum(fees),
        fees_inr=sum(fees),
        delta_pnl_inr=sum(float(t.delta_pnl_inr or 0.0) for t in trades),
        theta_pnl_inr=sum(float(t.theta_pnl_inr or 0.0) for t in trades),
        avg_r=avg_r,
        win_rate=win_rate,
        avg_slippage_bps=avg_slip,
    )


__all__ = ["GLOBAL_KEY", "DailyAggregator", "DailyReport", "NavSnapshot", "StrategyDailyStats"]

"""`bot status` one-screen rich dashboard.

Reads from the DB: today's NAV, open trades, recent decisions, daily PnL by strategy.
Renders to a single rich.Console so an operator on the VPS can `make status` and see
everything at a glance over SSH.
"""

from __future__ import annotations

import datetime as dt

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.storage.db import Database
from bot.storage.models import (
    DailyPnl,
    Decision,
    NavHistory,
    Trade,
    TradeStatus,
)


async def render_status_dashboard(db: Database, *, console: Console | None = None) -> str:
    console = console or Console(record=True, width=120)

    today = dt.date.today()
    nav: NavHistory | None = None
    open_trades: list[Trade] = []
    daily_rows: list[DailyPnl] = []
    recent_decisions: list[Decision] = []

    async with db.session() as session:
        nav_res = await session.execute(
            select(NavHistory)
            .where(NavHistory.trading_date <= today)
            .order_by(NavHistory.trading_date.desc())
            .limit(1)
        )
        nav = nav_res.scalar_one_or_none()
        open_res = await session.execute(
            select(Trade).where(Trade.status == TradeStatus.OPEN.value).options(selectinload(Trade.legs))
        )
        open_trades = list(open_res.scalars().all())
        daily_res = await session.execute(
            select(DailyPnl).where(DailyPnl.trading_date == today).order_by(DailyPnl.strategy_id)
        )
        daily_rows = list(daily_res.scalars().all())
        dec_res = await session.execute(select(Decision).order_by(Decision.ts.desc()).limit(10))
        recent_decisions = list(dec_res.scalars().all())

    console.print(_render_nav_panel(nav))
    console.print(_render_open_trades_table(open_trades))
    console.print(_render_daily_pnl_table(daily_rows))
    console.print(_render_decisions_table(recent_decisions))
    return console.export_text()


def _render_nav_panel(nav: NavHistory | None) -> Panel:
    if nav is None:
        return Panel("[yellow]no NAV history yet[/yellow]", title="NAV")
    cb = "[red bold]TRIPPED[/red bold]" if nav.circuit_breaker_tripped else "[green]OK[/green]"
    return Panel(
        (
            f"[bold]NAV:[/bold] ₹{nav.nav_inr:,.2f}\n"
            f"[bold]Peak NAV:[/bold] ₹{nav.peak_nav_inr:,.2f}\n"
            f"[bold]Drawdown:[/bold] {nav.drawdown_from_peak_pct:.2f}%\n"
            f"[bold]Circuit Breaker:[/bold] {cb}\n"
            f"[dim]As of {nav.trading_date.isoformat()}[/dim]"
        ),
        title="NAV",
    )


def _render_open_trades_table(trades: list[Trade]) -> Table:
    table = Table(title="Open Positions", expand=False)
    table.add_column("Trade")
    table.add_column("Strategy")
    table.add_column("Underlying")
    table.add_column("Lots", justify="right")
    table.add_column("Entry", justify="right")
    table.add_column("Peak", justify="right")
    table.add_column("Trough", justify="right")
    table.add_column("Stop", justify="right")
    table.add_column("Legs", justify="right")
    if not trades:
        table.add_row("—", "—", "—", "—", "—", "—", "—", "—", "—")
    else:
        for t in trades:
            table.add_row(
                str(t.id),
                t.strategy_id,
                t.underlying,
                str(t.lots),
                t.entry_ts.isoformat() if t.entry_ts else "—",
                f"{t.peak_pnl_inr or 0:,.0f}",
                f"{t.trough_pnl_inr or 0:,.0f}",
                f"{(t.notes or {}).get('trail_stop', '—')}",
                str(len(t.legs)),
            )
    return table


def _render_daily_pnl_table(rows: list[DailyPnl]) -> Table:
    table = Table(title=f"Daily PnL ({dt.date.today().isoformat()})", expand=False)
    table.add_column("Strategy")
    table.add_column("Trades", justify="right")
    table.add_column("Wins", justify="right")
    table.add_column("Losses", justify="right")
    table.add_column("Win %", justify="right")
    table.add_column("Gross ₹", justify="right")
    table.add_column("Net ₹", justify="right")
    if not rows:
        table.add_row("—", "0", "0", "0", "—", "0", "0")
    else:
        for r in rows:
            wr = f"{(r.win_rate or 0) * 100:.1f}" if r.win_rate is not None else "—"
            table.add_row(
                r.strategy_id,
                str(r.n_trades),
                str(r.n_wins),
                str(r.n_losses),
                wr,
                f"{r.gross_pnl_inr:,.0f}",
                f"{r.net_pnl_inr:,.0f}",
            )
    return table


def _render_decisions_table(rows: list[Decision]) -> Table:
    table = Table(title="Last 10 Decisions", expand=False)
    table.add_column("ts")
    table.add_column("strategy")
    table.add_column("passed")
    table.add_column("reason")
    table.add_column("symbol")
    if not rows:
        table.add_row("—", "—", "—", "—", "—")
    else:
        for r in rows:
            passed_str = "[green]yes[/green]" if r.passed else "[red]no[/red]"
            table.add_row(
                r.ts.strftime("%H:%M:%S") if r.ts else "—",
                r.strategy_id,
                passed_str,
                r.reason,
                r.symbol or "—",
            )
    return table


__all__ = ["render_status_dashboard"]

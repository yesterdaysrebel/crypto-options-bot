"""Per-trade Markdown journal generator.

When a Trade closes, the main loop calls `TradeJournal.write_for_trade(trade_id)`. This
module reads the trade and its legs from the DB, renders a structured Markdown file at
`journals/<YYYY-MM-DD>/<strategy>__<trade_id>.md`, and returns the path.

The journal includes: header, entry context (signal feature vector), execution detail
(per-leg fills + slippage), exit detail (trigger, prices, peak/trough PnL), and a
free-form notes section from `trade.notes`. Each trade gets its own file so the journal
can be reviewed in IDEs / git diff'd cleanly.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.storage.db import Database
from bot.storage.models import Order, Signal, Trade


class TradeJournal:
    def __init__(self, db: Database, *, journals_dir: Path) -> None:
        self._db = db
        self._journals_dir = journals_dir
        self._journals_dir.mkdir(parents=True, exist_ok=True)

    async def write_for_trade(self, trade_id: int) -> Path | None:
        async with self._db.session() as session:
            stmt = select(Trade).where(Trade.id == trade_id).options(selectinload(Trade.legs))
            trade = (await session.execute(stmt)).scalar_one_or_none()
            if trade is None:
                return None
            signal = None
            if trade.signal_id is not None:
                signal = (
                    await session.execute(select(Signal).where(Signal.id == trade.signal_id))
                ).scalar_one_or_none()
            orders = list(
                (await session.execute(select(Order).where(Order.trade_id == trade_id).order_by(Order.ts)))
                .scalars()
                .all()
            )
        date_str = (trade.exit_ts or trade.entry_ts).date().isoformat()
        out_dir = self._journals_dir / date_str
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{trade.strategy_id}__{trade.id}.md"
        path = out_dir / filename
        path.write_text(_render(trade, signal, orders), encoding="utf-8")
        return path


def _render(trade: Trade, signal: Signal | None, orders: list[Order]) -> str:
    lines = [
        f"# Trade #{trade.id} — {trade.strategy_id}",
        "",
        "## Summary",
        "",
        f"- Underlying: {trade.underlying}",
        f"- Lots: {trade.lots}",
        f"- Mode: {trade.mode}",
        f"- Status: {trade.status}",
        f"- Entry: {trade.entry_ts.isoformat() if trade.entry_ts else '—'}",
        f"- Exit: {trade.exit_ts.isoformat() if trade.exit_ts else '—'}",
        f"- Expiry: {trade.expiry.isoformat() if trade.expiry else '—'}",
        f"- Exit reason: {trade.exit_reason or '—'}",
        "",
        "## P&L",
        "",
        f"- Realised PnL: ₹{_fmt(trade.realised_pnl_inr)}",
        f"- Premium paid: ₹{_fmt(trade.premium_paid_inr)}",
        f"- Credit received: ₹{_fmt(trade.credit_received_inr)}",
        f"- Fees: ₹{_fmt(trade.fees_inr)}",
        f"- Delta PnL: ₹{_fmt(trade.delta_pnl_inr)}",
        f"- Theta PnL: ₹{_fmt(trade.theta_pnl_inr)}",
        f"- R-multiple: {_fmt(trade.r_multiple, ndigits=2)}",
        f"- Slippage: {_fmt(trade.slippage_bps, ndigits=1)} bps",
        f"- Peak PnL: ₹{_fmt(trade.peak_pnl_inr)}",
        f"- Trough PnL: ₹{_fmt(trade.trough_pnl_inr)}",
        f"- IV entry → exit: {_fmt(trade.entry_iv, ndigits=2)} → {_fmt(trade.exit_iv, ndigits=2)}",
        "",
    ]
    if signal is not None:
        lines += [
            "## Signal (Entry Context)",
            "",
            f"- Symbol: {signal.intended_symbol}",
            f"- Strike: {_fmt(signal.intended_strike)}",
            f"- Expiry: {signal.intended_expiry.isoformat() if signal.intended_expiry else '—'}",
            f"- Premium target: ₹{_fmt(signal.intended_premium_inr)}",
            "",
            "### Feature Vector",
            "",
        ]
        fv = signal.feature_vector or {}
        if fv:
            for k, v in sorted(fv.items()):
                lines.append(f"- `{k}`: {v}")
        else:
            lines.append("_no features captured_")
        lines.append("")
    lines += [
        "## Legs",
        "",
        "| # | Symbol | Side | Type | Strike | Lots | Entry ₹ | Exit ₹ | PnL ₹ | Status |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for leg in sorted(trade.legs, key=lambda leg_: leg_.leg_idx):
        lines.append(
            f"| {leg.leg_idx} | {leg.symbol} | {leg.side} | {leg.option_type or '—'} | "
            f"{_fmt(leg.strike, ndigits=0)} | {leg.lots} | {_fmt(leg.entry_price)} | "
            f"{_fmt(leg.exit_price)} | {_fmt(leg.pnl_inr)} | {leg.status} |"
        )
    lines += [
        "",
        "## Orders",
        "",
        "| ts | leg | client_order_id | side | type | qty | filled | price | state |",
        "|---|---:|---|---|---|---:|---:|---:|---|",
    ]
    for o in orders:
        lines.append(
            f"| {o.ts.isoformat() if o.ts else '—'} | {o.leg_idx if o.leg_idx is not None else '—'} | `{o.client_order_id}` | "
            f"{o.side} | {o.order_type} | {o.qty} | {o.filled_qty} | {_fmt(o.filled_price)} | {o.state} |"
        )
    lines += ["", "## Notes", "", _render_notes(trade.notes), ""]
    return "\n".join(lines) + "\n"


def _render_notes(notes: dict | None) -> str:
    if not notes:
        return "_no notes_"
    items = [f"- **{k}**: {v}" for k, v in sorted(notes.items())]
    return "\n".join(items)


def _fmt(value: float | int | None, *, ndigits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.{ndigits}f}"


__all__ = ["TradeJournal"]

"""Premium / credit fields on trades are stored per lot; entry fills report net totals."""

from __future__ import annotations

from collections.abc import Iterable

from bot.execution.router import LegFill, LegSide


def per_lot_premium_from_net(net_premium_inr: float, lots: int) -> tuple[float | None, float | None]:
    """Split `EntryResult.total_premium_inr` (all lots) into per-lot DB columns."""
    if lots < 1:
        lots = 1
    per_lot = net_premium_inr / lots
    if per_lot > 0:
        return per_lot, None
    if per_lot < 0:
        return None, -per_lot
    return None, None


def entry_cashflow_inr(
    *,
    premium_paid_per_lot: float | None,
    credit_received_per_lot: float | None,
    lots: int,
) -> float:
    """Cash paid at entry (negative) from per-lot premium/credit columns."""
    cash = 0.0
    if premium_paid_per_lot is not None:
        cash -= float(premium_paid_per_lot) * lots
    if credit_received_per_lot is not None:
        cash += float(credit_received_per_lot) * lots
    return cash


def exit_cashflow_inr(fills: Iterable[LegFill]) -> float:
    """Cash from exit fills (positive when closing a long premium position)."""
    cash = 0.0
    for f in fills:
        if f.avg_fill_price is None:
            continue
        sign = 1.0 if f.side == LegSide.SELL else -1.0
        cash += sign * f.avg_fill_price * f.qty_filled
    return cash


def realised_pnl_inr(
    *,
    premium_paid_per_lot: float | None,
    credit_received_per_lot: float | None,
    lots: int,
    exit_fills: Iterable[LegFill],
) -> float:
    return entry_cashflow_inr(
        premium_paid_per_lot=premium_paid_per_lot,
        credit_received_per_lot=credit_received_per_lot,
        lots=lots,
    ) + exit_cashflow_inr(exit_fills)


__all__ = [
    "entry_cashflow_inr",
    "exit_cashflow_inr",
    "per_lot_premium_from_net",
    "realised_pnl_inr",
]

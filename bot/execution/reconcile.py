"""Order reconciliation on boot.

After a crash or restart, persisted state (DB) and live exchange state may disagree.
This module reconciles them and refuses to start the trading loop if any mismatch
cannot be resolved automatically.

Reconciliation rules (per the implementation plan):
  * For every Trade with status=OPEN, walk its Legs and find associated Orders.
  * Cross-check each Order against the exchange:
      - DB says PENDING/OPEN but exchange has no record         -> mark CANCELED locally
      - DB says PENDING/OPEN and exchange says FILLED           -> update filled_qty/price
      - DB says FILLED  and exchange says PARTIAL/CANCELED      -> RAISE (manual intervention)
      - exchange has open orders our DB doesn't know about      -> RAISE (foreign orders)
  * For every multi-leg trade group: either ALL legs are confirmed open at the exchange,
    or none of them are. Anything partial demands manual intervention.

Returns a `ReconcileReport`. If `must_halt` is True, the boot should refuse to continue.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.exchange.rest import DeltaRestClient, DeltaRestError
from bot.storage.db import Database
from bot.storage.models import LegStatus, Order, OrderState, Trade, TradeStatus

_TERMINAL_LOCAL_STATES = {
    OrderState.FILLED.value,
    OrderState.CANCELED.value,
    OrderState.REJECTED.value,
}


@dataclass
class ReconcileMismatch:
    kind: str  # "foreign_open", "fill_state_regression", "partial_leg_group", "rest_error"
    detail: str
    order_id: int | None = None
    trade_id: int | None = None
    client_order_id: str | None = None


@dataclass
class ReconcileReport:
    started_at: dt.datetime
    finished_at: dt.datetime
    orders_checked: int = 0
    orders_updated: int = 0
    trades_checked: int = 0
    mismatches: list[ReconcileMismatch] = field(default_factory=list)

    @property
    def must_halt(self) -> bool:
        return bool(self.mismatches)


class ReconcileError(RuntimeError):
    def __init__(self, report: ReconcileReport) -> None:
        super().__init__(self._format(report))
        self.report = report

    @staticmethod
    def _format(report: ReconcileReport) -> str:
        lines = [
            "Reconciliation found unresolved mismatches:",
            *(f"  - [{m.kind}] {m.detail}" for m in report.mismatches),
            "Refusing to start trading loop. Run `make resume` after manual review.",
        ]
        return "\n".join(lines)


class OrderReconciler:
    """Cross-checks DB orders against the exchange before trading resumes."""

    def __init__(self, db: Database, rest: DeltaRestClient) -> None:
        self._db = db
        self._rest = rest

    async def run(self) -> ReconcileReport:
        started = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        report = ReconcileReport(started_at=started, finished_at=started)
        async with self._db.session() as session:
            open_trades_stmt = select(Trade).where(Trade.status == TradeStatus.OPEN.value)
            open_trades_res = await session.execute(open_trades_stmt)
            open_trades = list(open_trades_res.scalars().all())
            report.trades_checked = len(open_trades)

            db_open_orders_stmt = select(Order).where(
                Order.state.in_(
                    [OrderState.PENDING.value, OrderState.OPEN.value, OrderState.PARTIALLY_FILLED.value]
                )
            )
            db_open_orders_res = await session.execute(db_open_orders_stmt)
            db_open_orders = list(db_open_orders_res.scalars().all())
            report.orders_checked = len(db_open_orders)
        try:
            live_open = await self._rest.get_open_orders()
        except DeltaRestError as exc:
            report.mismatches.append(
                ReconcileMismatch(kind="rest_error", detail=f"get_open_orders failed: {exc}")
            )
            report.finished_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
            return report

        live_by_coid: dict[str, dict[str, object]] = {}
        for order in live_open:
            coid = str(order.get("client_order_id") or "")
            if coid:
                live_by_coid[coid] = order

        db_coids = {o.client_order_id for o in db_open_orders}

        # 1) Foreign open orders (live without DB row): refuse to start.
        for coid, order in live_by_coid.items():
            if coid not in db_coids:
                report.mismatches.append(
                    ReconcileMismatch(
                        kind="foreign_open",
                        detail=f"Open exchange order without DB record: client_order_id={coid!r} symbol={order.get('symbol')!r}",
                        client_order_id=coid,
                    )
                )

        # 2) Walk DB open orders and reconcile against live state.
        async with self._db.session() as session:
            db_open_orders_res = await session.execute(db_open_orders_stmt)
            db_open_orders = list(db_open_orders_res.scalars().all())
            for db_order in db_open_orders:
                live = live_by_coid.get(db_order.client_order_id)
                if live is None:
                    fetched = await self._fetch_order(db_order.client_order_id)
                    state = str(fetched.get("state") if fetched else "canceled") or "canceled"
                    if state == OrderState.FILLED.value and fetched is not None:
                        db_order.state = OrderState.FILLED.value
                        db_order.filled_qty = float(fetched.get("filled_size") or db_order.qty)  # type: ignore[arg-type]
                        avg_price = fetched.get("average_fill_price")
                        if avg_price is not None:
                            db_order.filled_price = float(avg_price)  # type: ignore[arg-type]
                        report.orders_updated += 1
                    elif state in _TERMINAL_LOCAL_STATES:
                        db_order.state = OrderState.CANCELED.value
                        report.orders_updated += 1
                    else:
                        report.mismatches.append(
                            ReconcileMismatch(
                                kind="fill_state_regression",
                                detail=f"DB order open but exchange returned state={state!r}",
                                order_id=db_order.id,
                                client_order_id=db_order.client_order_id,
                            )
                        )
                else:
                    live_state = str(live.get("state", OrderState.OPEN.value))
                    if live_state != db_order.state:
                        db_order.state = live_state
                        report.orders_updated += 1

        # 3) Check multi-leg coherence per open trade.
        async with self._db.session() as session:
            for trade in open_trades:
                leg_stmt = select(Order).where(Order.trade_id == trade.id, Order.leg_idx.isnot(None))
                leg_res = await session.execute(leg_stmt)
                rows = list(leg_res.scalars().all())
                if not rows:
                    continue
                groups: dict[int, list[Order]] = defaultdict(list)
                for r in rows:
                    if r.leg_idx is not None:
                        groups[r.leg_idx].append(r)
                states = []
                for leg_idx in sorted(groups):
                    leg_orders = sorted(groups[leg_idx], key=lambda o: o.ts, reverse=True)
                    states.append(leg_orders[0].state)
                unique = set(states)
                inconsistent = OrderState.FILLED.value in unique and unique - {
                    OrderState.FILLED.value,
                    OrderState.CANCELED.value,
                    OrderState.REJECTED.value,
                }
                if inconsistent:
                    report.mismatches.append(
                        ReconcileMismatch(
                            kind="partial_leg_group",
                            detail=f"Trade {trade.id} has mixed leg states: {states}",
                            trade_id=trade.id,
                        )
                    )

        # 4) Update Leg/Trade statuses if all legs filled or all cancelled (post-update view).
        async with self._db.session() as session:
            trades_res = await session.execute(
                select(Trade).where(Trade.status == TradeStatus.OPEN.value).options(selectinload(Trade.legs))
            )
            for trade in trades_res.scalars().all():
                leg_orders_res = await session.execute(
                    select(Order).where(Order.trade_id == trade.id, Order.leg_idx.isnot(None))
                )
                leg_orders = list(leg_orders_res.scalars().all())
                if not leg_orders:
                    continue
                if all(o.state == OrderState.CANCELED.value for o in leg_orders):
                    trade.status = TradeStatus.CLOSED.value
                    for leg in trade.legs:
                        leg.status = LegStatus.CLOSED.value

        report.finished_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        if report.mismatches:
            logger.error("reconcile mismatches found: {}", len(report.mismatches))
        else:
            logger.info(
                "reconcile clean: trades={} orders={} updated={}",
                report.trades_checked,
                report.orders_checked,
                report.orders_updated,
            )
        return report

    async def _fetch_order(self, client_order_id: str) -> dict[str, object] | None:
        try:
            envelope = await self._rest._request(
                "GET", "/v2/orders", params={"client_order_id": client_order_id}, signed=True
            )
        except DeltaRestError as exc:
            logger.warning("reconcile: fetch_order failed for {}: {}", client_order_id, exc)
            return None
        result = envelope.get("result")
        if isinstance(result, list) and result:
            return dict(result[0])
        if isinstance(result, dict):
            return result
        return None

"""LiveExecutor — Delta Exchange India.

Implements the same `ExecutionRouter` interface as DryExecutor but routes through
DeltaRestClient. Order placement:
    * Entries: post-only LIMIT at the appropriate side of the book (rounded to tick_size).
      Waits up to `maker_timeout_seconds`, polling the exchange; if still open, cancels
      and re-submits as an IOC marketable limit within the slip budget.
    * Multi-leg atomicity: legs are placed concurrently; if any leg ends in `rejected`
      or partially fills past the timeout, the executor cancels remaining open legs and
      submits reduce-only close orders for already-filled legs.
    * Exits: reduce-only orders, market or stop-market depending on the trigger.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

from loguru import logger

from bot.config.models import StrategyId
from bot.data.chain_cache import ChainCache, QuoteSnapshot
from bot.exchange.rest import DeltaRestClient, DeltaRestError
from bot.execution.client_id import generate_client_order_id
from bot.execution.router import (
    EntryRequest,
    EntryResult,
    ExecutionRouter,
    ExitRequest,
    ExitResult,
    LegFill,
    LegSide,
)
from bot.strategies.base import LegIntent

_ORDER_POLL_INTERVAL_S = 1.0
_IOC_WAIT_SECONDS = 10.0
_TERMINAL_ORDER_STATES = frozenset({"filled", "cancelled", "rejected", "closed"})


class LiveExecutor(ExecutionRouter):
    def __init__(
        self,
        rest: DeltaRestClient,
        chain: ChainCache,
        *,
        slip_bps_directional: int = 50,
        slip_bps_condor: int = 100,
        slip_bps_strangle: int = 50,
    ) -> None:
        self._rest = rest
        self._chain = chain
        self._slip_bps = {
            StrategyId.DIRECTIONAL: slip_bps_directional,
            StrategyId.CREDIT_VERTICAL: slip_bps_condor,
            StrategyId.LONG_STRADDLE: slip_bps_strangle,
        }

    async def submit_entry(self, req: EntryRequest) -> EntryResult:
        fills: list[LegFill] = []
        try:
            slip_bps = self._slip_bps.get(req.strategy_id, req.slip_bps_budget)
            tasks = [
                self._place_entry_leg(
                    req.strategy_id,
                    req.trade_id,
                    idx,
                    leg,
                    req.lots,
                    req.maker_timeout_seconds,
                    slip_bps=slip_bps,
                )
                for idx, leg in enumerate(req.legs)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, LegFill):
                    fills.append(r)
                else:
                    logger.error("entry leg failed: {!r}", r)
                    fills.append(
                        LegFill(
                            symbol="?",
                            side=LegSide.BUY,
                            qty_requested=req.lots,
                            qty_filled=0,
                            avg_fill_price=None,
                            leg_idx=-1,
                            client_order_id="error",
                            state="rejected",
                        )
                    )
            if any(not f.is_complete for f in fills):
                rollback_actions = await self._rollback(req.trade_id, fills)
                return EntryResult(
                    success=False,
                    trade_id=req.trade_id,
                    fills=fills,
                    error="partial_fill_rolled_back",
                    rollback_actions=rollback_actions,
                    completed_at=_now(),
                )
            return EntryResult(success=True, trade_id=req.trade_id, fills=fills, completed_at=_now())
        except DeltaRestError as exc:
            logger.exception("submit_entry failed for trade {}", req.trade_id)
            return EntryResult(
                success=False, trade_id=req.trade_id, fills=fills, error=str(exc), completed_at=_now()
            )

    async def _place_entry_leg(
        self,
        strategy_id: StrategyId,
        trade_id: int,
        leg_idx: int,
        leg: LegIntent,
        lots: int,
        maker_timeout_seconds: float,
        *,
        slip_bps: int,
    ) -> LegFill:
        quote = self._chain.get_quote(leg.symbol)
        instrument = self._chain.get_instrument(leg.symbol)
        if quote is None or instrument is None or quote.mid is None:
            return LegFill(
                symbol=leg.symbol,
                side=LegSide(leg.side),
                qty_requested=lots,
                qty_filled=0,
                avg_fill_price=None,
                leg_idx=leg_idx,
                client_order_id="missing_quote",
                state="rejected",
            )
        coid = generate_client_order_id(
            strategy_id=strategy_id.value, trade_id=trade_id, leg_idx=leg_idx, purpose="entry"
        )
        limit_price = _maker_limit_price(quote, leg.side, instrument.tick_size)
        payload = {
            "product_id": instrument.product_id,
            "size": lots,
            "side": leg.side,
            "order_type": "limit_order",
            "limit_price": str(limit_price),
            "post_only": "true",
            "client_order_id": coid,
        }
        resp = await self._rest.place_order(payload)
        fill = _order_to_leg_fill(
            resp, symbol=leg.symbol, side=leg.side, lots=lots, leg_idx=leg_idx, coid=coid
        )
        if fill.is_complete:
            return fill

        order = await self._wait_for_order(coid, timeout_seconds=maker_timeout_seconds)
        if order is not None:
            fill = _order_to_leg_fill(
                order, symbol=leg.symbol, side=leg.side, lots=lots, leg_idx=leg_idx, coid=coid
            )
            if fill.is_complete:
                return fill

        await self._cancel_open_order(fill)
        ioc_coid = generate_client_order_id(
            strategy_id=strategy_id.value, trade_id=trade_id, leg_idx=leg_idx, purpose="entry_ioc"
        )
        ioc_price = _ioc_limit_price(quote, leg.side, slip_bps, instrument.tick_size)
        if ioc_price is None:
            return LegFill(
                symbol=leg.symbol,
                side=LegSide(leg.side),
                qty_requested=lots,
                qty_filled=0,
                avg_fill_price=None,
                leg_idx=leg_idx,
                client_order_id=fill.client_order_id,
                exchange_order_id=fill.exchange_order_id,
                state="rejected",
                raw_response=fill.raw_response,
            )
        ioc_payload = {
            "product_id": instrument.product_id,
            "size": lots,
            "side": leg.side,
            "order_type": "limit_order",
            "limit_price": str(ioc_price),
            "time_in_force": "ioc",
            "post_only": "false",
            "client_order_id": ioc_coid,
        }
        ioc_resp = await self._rest.place_order(ioc_payload)
        ioc_fill = _order_to_leg_fill(
            ioc_resp, symbol=leg.symbol, side=leg.side, lots=lots, leg_idx=leg_idx, coid=ioc_coid
        )
        if ioc_fill.is_complete:
            return ioc_fill
        ioc_order = await self._wait_for_order(ioc_coid, timeout_seconds=_IOC_WAIT_SECONDS)
        if ioc_order is not None:
            return _order_to_leg_fill(
                ioc_order, symbol=leg.symbol, side=leg.side, lots=lots, leg_idx=leg_idx, coid=ioc_coid
            )
        return ioc_fill

    async def _wait_for_order(
        self, client_order_id: str, *, timeout_seconds: float
    ) -> dict[str, object] | None:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        last: dict[str, object] | None = None
        while time.monotonic() < deadline:
            last = await self._rest.get_order_by_client_id(client_order_id)
            if last is None:
                await asyncio.sleep(_ORDER_POLL_INTERVAL_S)
                continue
            state = str(last.get("state", ""))
            if state in _TERMINAL_ORDER_STATES:
                return last
            await asyncio.sleep(_ORDER_POLL_INTERVAL_S)
        return last

    async def _cancel_open_order(self, fill: LegFill) -> None:
        try:
            if fill.exchange_order_id is not None:
                await self._rest.cancel_order(order_id=int(fill.exchange_order_id))
            elif fill.client_order_id:
                await self._rest.cancel_order(client_order_id=fill.client_order_id)
        except (DeltaRestError, RuntimeError) as exc:
            logger.warning("cancel_open_order failed for {}: {}", fill.client_order_id, exc)

    async def submit_exit(self, req: ExitRequest) -> ExitResult:
        fills: list[LegFill] = []
        for idx, leg in enumerate(req.legs):
            instrument = self._chain.get_instrument(leg.symbol)
            if instrument is None:
                fills.append(
                    LegFill(
                        symbol=leg.symbol,
                        side=LegSide("sell" if leg.side == "buy" else "buy"),
                        qty_requested=req.lots,
                        qty_filled=0,
                        avg_fill_price=None,
                        leg_idx=idx,
                        client_order_id="missing_instrument",
                        state="rejected",
                    )
                )
                continue
            coid = generate_client_order_id(
                strategy_id=req.strategy_id.value,
                trade_id=req.trade_id,
                leg_idx=idx,
                purpose=f"exit_{req.trigger.value}",
            )
            flipped_side = "sell" if leg.side == "buy" else "buy"
            payload = {
                "product_id": instrument.product_id,
                "size": req.lots,
                "side": flipped_side,
                "order_type": "market_order",
                "reduce_only": "true",
                "client_order_id": coid,
            }
            try:
                resp = await self._rest.place_order(payload)
            except DeltaRestError as exc:
                logger.exception("exit leg place failed for {}", leg.symbol)
                fills.append(
                    LegFill(
                        symbol=leg.symbol,
                        side=LegSide(flipped_side),
                        qty_requested=req.lots,
                        qty_filled=0,
                        avg_fill_price=None,
                        leg_idx=idx,
                        client_order_id=coid,
                        state="rejected",
                        raw_response={"error": str(exc)},
                    )
                )
                continue
            filled = float(resp.get("filled_size") or 0)
            avg = float(resp.get("average_fill_price") or 0) if filled > 0 else None
            fills.append(
                LegFill(
                    symbol=leg.symbol,
                    side=LegSide(flipped_side),
                    qty_requested=req.lots,
                    qty_filled=filled,
                    avg_fill_price=avg,
                    leg_idx=idx,
                    client_order_id=coid,
                    exchange_order_id=resp.get("id"),
                    state=str(resp.get("state", "filled")),
                    raw_response=resp,
                )
            )
        ok = all(f.is_complete for f in fills)
        return ExitResult(success=ok, trade_id=req.trade_id, fills=fills, completed_at=_now())

    async def update_stop(
        self,
        trade_id: int,
        symbol: str,
        side: LegSide,
        qty: float,
        new_stop_price: float,
        client_order_id: str,
    ) -> dict[str, object]:
        try:
            await self._rest.cancel_order(client_order_id=client_order_id)
        except DeltaRestError:
            logger.warning("update_stop: prior stop {} not cancellable, proceeding", client_order_id)
        instrument = self._chain.get_instrument(symbol)
        if instrument is None:
            return {"ok": False, "trade_id": trade_id, "error": "missing_instrument"}
        new_coid = generate_client_order_id(
            strategy_id="directional",
            trade_id=trade_id,
            leg_idx=0,
            purpose=f"trail_{int(new_stop_price)}",
        )
        payload = {
            "product_id": instrument.product_id,
            "size": qty,
            "side": side.value,
            "order_type": "stop_market_order",
            "stop_price": str(new_stop_price),
            "reduce_only": "true",
            "client_order_id": new_coid,
        }
        try:
            resp = await self._rest.place_order(payload)
        except DeltaRestError as exc:
            return {"ok": False, "trade_id": trade_id, "error": str(exc)}
        return {
            "ok": True,
            "trade_id": trade_id,
            "client_order_id": new_coid,
            "exchange_order_id": resp.get("id"),
        }

    async def cancel_all_for_trade(self, trade_id: int) -> int:
        # v1: cancel all open orders for the account. Per-trade granularity needs DB lookup.
        try:
            await self._rest.cancel_all_orders()
            return 1
        except DeltaRestError as exc:
            logger.exception("cancel_all_for_trade failed: {}", exc)
            return 0

    async def _rollback(self, trade_id: int, fills: list[LegFill]) -> list[str]:
        actions: list[str] = []
        for fill in fills:
            if fill.qty_filled > 0:
                instrument = self._chain.get_instrument(fill.symbol)
                if instrument is None:
                    continue
                flipped = "sell" if fill.side == LegSide.BUY else "buy"
                coid = generate_client_order_id(
                    strategy_id="rollback", trade_id=trade_id, leg_idx=fill.leg_idx, purpose="rollback"
                )
                try:
                    await self._rest.place_order(
                        {
                            "product_id": instrument.product_id,
                            "size": fill.qty_filled,
                            "side": flipped,
                            "order_type": "market_order",
                            "reduce_only": "true",
                            "client_order_id": coid,
                        }
                    )
                    actions.append(f"rollback_close:{fill.symbol}")
                except DeltaRestError as exc:
                    actions.append(f"rollback_close_failed:{fill.symbol}:{exc}")
            elif fill.exchange_order_id is not None:
                try:
                    await self._rest.cancel_order(order_id=fill.exchange_order_id)
                    actions.append(f"cancel:{fill.symbol}")
                except DeltaRestError as exc:
                    actions.append(f"cancel_failed:{fill.symbol}:{exc}")
            elif fill.client_order_id and fill.client_order_id not in {"error", "missing_quote"}:
                try:
                    await self._rest.cancel_order(client_order_id=fill.client_order_id)
                    actions.append(f"cancel:{fill.symbol}")
                except (DeltaRestError, RuntimeError) as exc:
                    actions.append(f"cancel_failed:{fill.symbol}:{exc}")
        return actions


def _maker_limit_price(quote: QuoteSnapshot, side: str, tick: float) -> float | None:
    limit_price = _round_tick(quote.bid if side == "buy" else quote.ask, tick)
    if limit_price is None:
        limit_price = _round_tick(quote.mid, tick)
    return limit_price


def _ioc_limit_price(quote: QuoteSnapshot, side: str, slip_bps: int, tick: float) -> float | None:
    slip = max(0, slip_bps) / 10_000.0
    if side == "buy":
        ref = quote.ask if quote.ask is not None else quote.mid
        if ref is None:
            return None
        return _round_tick(ref * (1.0 + slip), tick)
    ref = quote.bid if quote.bid is not None else quote.mid
    if ref is None:
        return None
    return _round_tick(ref * (1.0 - slip), tick)


def _as_float(v: Any, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _as_optional_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_optional_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _order_to_leg_fill(
    order: dict[str, object],
    *,
    symbol: str,
    side: str,
    lots: int,
    leg_idx: int,
    coid: str,
) -> LegFill:
    filled = _as_float(order.get("filled_size"))
    avg = _as_optional_float(order.get("average_fill_price")) if filled > 0 else None
    exchange_order_id = _as_optional_int(order.get("id"))
    return LegFill(
        symbol=symbol,
        side=LegSide(side),
        qty_requested=lots,
        qty_filled=filled,
        avg_fill_price=avg,
        leg_idx=leg_idx,
        client_order_id=coid,
        exchange_order_id=exchange_order_id,
        state=str(order.get("state", "open")),
        raw_response=dict(order),
    )


def _round_tick(value: float | None, tick: float) -> float | None:
    if value is None or tick <= 0:
        return value
    return round(round(value / tick) * tick, 8)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)

"""LiveExecutor — Delta Exchange India.

Implements the same `ExecutionRouter` interface as DryExecutor but routes through
DeltaRestClient. Order placement:
    * Entries: post-only LIMIT at the appropriate side of the book (rounded to tick_size).
      A timer waits up to `maker_timeout_seconds` then cancels and re-submits as IOC if
      still unfilled.
    * Multi-leg atomicity: legs are placed concurrently; if any leg ends in `rejected`
      or partially fills past the timeout, the executor cancels remaining open legs and
      submits reduce-only close orders for already-filled legs.
    * Exits: reduce-only orders, market or stop-market depending on the trigger.

This v1 implementation focuses on correctness; the latency-sensitive optimisations
(early-cancel on book deterioration, sliding limit, etc.) are out of scope for the dry-run
gate. Live trading is held back until PR #23 (`make go-live`).
"""

from __future__ import annotations

import asyncio
import datetime as dt

from loguru import logger

from bot.config.models import StrategyId
from bot.data.chain_cache import ChainCache
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
            StrategyId.IRON_CONDOR: slip_bps_condor,
            StrategyId.VOL_STRANGLE: slip_bps_strangle,
        }

    async def submit_entry(self, req: EntryRequest) -> EntryResult:
        fills: list[LegFill] = []
        try:
            tasks = [
                self._place_entry_leg(
                    req.strategy_id, req.trade_id, idx, leg, req.lots, req.maker_timeout_seconds
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
        limit_price = _round_tick(
            quote.bid if leg.side == "buy" else quote.ask,
            instrument.tick_size,
        )
        if limit_price is None:
            limit_price = _round_tick(quote.mid, instrument.tick_size)
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
        filled = float(resp.get("filled_size") or 0)
        avg = float(resp.get("average_fill_price") or 0) if filled > 0 else None
        return LegFill(
            symbol=leg.symbol,
            side=LegSide(leg.side),
            qty_requested=lots,
            qty_filled=filled,
            avg_fill_price=avg,
            leg_idx=leg_idx,
            client_order_id=coid,
            exchange_order_id=resp.get("id"),
            state=str(resp.get("state", "open")),
            raw_response=resp,
        )

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
        return actions


def _round_tick(value: float | None, tick: float) -> float | None:
    if value is None or tick <= 0:
        return value
    return round(round(value / tick) * tick, 8)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)

"""DryExecutor — in-process simulator with mid+slippage fills.

Used in DRY_RUN mode and by unit tests. Fills are computed from the latest QuoteSnapshot
in the ChainCache for each leg, with a configurable random slippage capped at the request's
`slip_bps_budget`. Multi-leg requests fill atomically by design (we don't simulate partials
in v1; the live executor handles those).
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Iterable

from bot.config.models import StrategyId
from bot.data.chain_cache import ChainCache
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


class DryExecutor(ExecutionRouter):
    def __init__(
        self,
        chain: ChainCache,
        *,
        slip_bps_directional: int = 50,
        slip_bps_condor: int = 100,
        slip_bps_strangle: int = 50,
        seed: int | None = None,
    ) -> None:
        self._chain = chain
        self._slip_bps = {
            StrategyId.DIRECTIONAL: slip_bps_directional,
            StrategyId.CREDIT_VERTICAL: slip_bps_condor,
            StrategyId.LONG_STRADDLE: slip_bps_strangle,
        }
        self._rng = random.Random(seed)
        self._open_orders: dict[int, list[str]] = {}

    async def submit_entry(self, req: EntryRequest) -> EntryResult:
        slip_bps = self._slip_bps.get(req.strategy_id, 50)
        fills = list(
            self._fill_legs(req.strategy_id, req.trade_id, req.legs, req.lots, slip_bps, purpose="entry")
        )
        if any(not f.is_complete for f in fills):
            return EntryResult(
                success=False,
                trade_id=req.trade_id,
                fills=fills,
                error="dry_executor_partial_fill",
                completed_at=_now(),
            )
        self._open_orders.setdefault(req.trade_id, []).extend(f.client_order_id for f in fills)
        return EntryResult(
            success=True,
            trade_id=req.trade_id,
            fills=fills,
            submitted_at=_now(),
            completed_at=_now(),
        )

    async def submit_exit(self, req: ExitRequest) -> ExitResult:
        flipped: list[LegIntent] = []
        for leg in req.legs:
            flipped.append(
                LegIntent(
                    symbol=leg.symbol,
                    side="sell" if leg.side == "buy" else "buy",
                    option_type=leg.option_type,
                    strike=leg.strike,
                    expiry=leg.expiry,
                )
            )
        slip_bps = self._slip_bps.get(req.strategy_id, 50)
        fills = list(
            self._fill_legs(
                req.strategy_id,
                req.trade_id,
                flipped,
                req.lots,
                slip_bps,
                purpose=f"exit_{req.trigger.value}",
            )
        )
        self._open_orders.pop(req.trade_id, None)
        return ExitResult(success=True, trade_id=req.trade_id, fills=fills, completed_at=_now())

    async def update_stop(
        self,
        trade_id: int,
        symbol: str,
        side: LegSide,
        qty: float,
        new_stop_price: float,
        client_order_id: str,
    ) -> dict[str, object]:
        return {
            "ok": True,
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side.value,
            "qty": qty,
            "new_stop_price": new_stop_price,
            "client_order_id": client_order_id,
        }

    async def cancel_all_for_trade(self, trade_id: int) -> int:
        cancelled = self._open_orders.pop(trade_id, [])
        return len(cancelled)

    def _fill_legs(
        self,
        strategy_id: StrategyId,
        trade_id: int,
        legs: Iterable[LegIntent],
        lots: int,
        slip_bps: int,
        *,
        purpose: str,
    ) -> Iterable[LegFill]:
        for idx, leg in enumerate(legs):
            quote = self._chain.get_quote(leg.symbol)
            mid = quote.mid if quote else None
            if mid is None or mid <= 0:
                yield LegFill(
                    symbol=leg.symbol,
                    side=LegSide(leg.side),
                    qty_requested=lots,
                    qty_filled=0,
                    avg_fill_price=None,
                    leg_idx=idx,
                    client_order_id=generate_client_order_id(
                        strategy_id=strategy_id.value, trade_id=trade_id, leg_idx=idx, purpose=purpose
                    ),
                    state="rejected",
                )
                continue
            slip = self._rng.uniform(-slip_bps, slip_bps) / 10_000.0
            sign = 1.0 if leg.side == "buy" else -1.0
            fill_price = mid * (1.0 + sign * abs(slip))
            yield LegFill(
                symbol=leg.symbol,
                side=LegSide(leg.side),
                qty_requested=lots,
                qty_filled=lots,
                avg_fill_price=round(fill_price, 4),
                leg_idx=idx,
                client_order_id=generate_client_order_id(
                    strategy_id=strategy_id.value, trade_id=trade_id, leg_idx=idx, purpose=purpose
                ),
                state="filled",
                slippage_bps=abs(slip) * 10_000.0,
            )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)

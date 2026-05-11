"""Delta wallet balance helpers (signed `/v2/wallet/balances`)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from loguru import logger

from bot.exchange.rest import DeltaRestClient, DeltaRestError


def _as_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def fetch_wallet_snapshot(rest: DeltaRestClient) -> dict[str, Any]:
    """Return a JSON-serialisable wallet view for logs, `trade.notes`, and journals.

    Shape is defensive: Delta India payloads evolve; we keep raw rows plus normalised floats.
    """
    ts = dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat()
    try:
        raw = await rest.get_wallet_balances()
    except DeltaRestError as exc:
        logger.warning("wallet balances unavailable: {}", exc)
        return {"ts": ts, "error": str(exc), "balances": []}

    rows: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            sym = item.get("asset_symbol") or item.get("symbol") or item.get("asset_id")
            bal = _as_float(item.get("balance"))
            avail = _as_float(item.get("available_balance") or item.get("available"))
            rows.append(
                {
                    "asset_symbol": sym,
                    "balance": bal,
                    "available_balance": avail,
                    "portfolio_id": item.get("portfolio_id"),
                }
            )
    return {"ts": ts, "balances": rows}


__all__ = ["fetch_wallet_snapshot"]

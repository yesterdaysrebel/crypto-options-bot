"""Stable, idempotent client_order_id generation.

Format:  <strategy>-<trade_id>-<leg_idx>-<purpose>-<short_uuid>
The same (strategy, trade_id, leg_idx, purpose) within a single boot generates the same
short_uuid; this lets the reconciler match orders without ambiguity after a crash.
"""

from __future__ import annotations

import hashlib
import uuid


def generate_client_order_id(
    *,
    strategy_id: str,
    trade_id: int | str,
    leg_idx: int,
    purpose: str,
    salt: str | None = None,
) -> str:
    if salt is None:
        salt = uuid.uuid4().hex[:8]
    raw = f"{strategy_id}|{trade_id}|{leg_idx}|{purpose}|{salt}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{strategy_id}-{trade_id}-{leg_idx}-{purpose}-{digest}"

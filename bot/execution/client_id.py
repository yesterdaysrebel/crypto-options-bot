"""Stable, idempotent client_order_id generation.

Delta validates ``client_order_id`` at max 32 chars. Keep ids compact while preserving:
- deterministic output for identical inputs (including `salt`)
- uniqueness across strategy/trade/leg/purpose
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
    sid = _compact_strategy(strategy_id)
    tid = str(trade_id)[-6:]
    pid = _compact_purpose(purpose)
    # Format budget: sid(2)-tid(<=6)-leg(<=2)-pid(<=6)-digest(10) + 4 hyphens <= 32
    return f"{sid}-{tid}-{leg_idx}-{pid}-{digest}"


def _compact_strategy(strategy_id: str) -> str:
    mapping = {
        "directional": "dr",
        "credit_vertical": "cv",
        "long_straddle": "ls",
        "rollback": "rb",
    }
    return mapping.get(strategy_id, strategy_id[:2].lower())


def _compact_purpose(purpose: str) -> str:
    token = "".join(ch for ch in purpose.lower() if ch.isalnum())
    return token[:6] if token else "ord"

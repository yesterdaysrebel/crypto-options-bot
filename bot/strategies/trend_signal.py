"""Shared 15m trend-breakout signal (EMA separation + range breakout)."""

from __future__ import annotations

from typing import Any

import numpy as np

from bot.config.models import DirectionalEntry
from bot.data.candles import atr, ema


def evaluate_trend_breakout(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    entry: DirectionalEntry,
) -> tuple[bool, bool, dict[str, Any]]:
    """Return (long_setup, short_setup, feature_vector)."""
    min_bars = max(entry.ema_slow, entry.atr_period + 1, entry.prior_bars + 2)
    if len(closes) < min_bars:
        return False, False, {"error": "insufficient_history", "n_candles": len(closes)}

    ema_fast = ema(closes, entry.ema_fast)
    ema_slow = ema(closes, entry.ema_slow)
    atr_series = atr(highs, lows, closes, entry.atr_period)
    latest_atr = float(atr_series[-1])
    if not np.isfinite(latest_atr):
        return False, False, {"error": "atr_not_ready"}

    latest_close = float(closes[-1])
    ema_sep = float(ema_fast[-1] - ema_slow[-1])
    threshold = entry.breakout_atr_mult * latest_atr
    prior_high = float(highs[-(entry.prior_bars + 1) : -1].max())
    prior_low = float(lows[-(entry.prior_bars + 1) : -1].min())

    long_setup = ema_sep > threshold and latest_close > prior_high + threshold
    short_setup = ema_sep < -threshold and latest_close < prior_low - threshold

    features: dict[str, Any] = {
        "latest_close": latest_close,
        "ema_fast": float(ema_fast[-1]),
        "ema_slow": float(ema_slow[-1]),
        "ema_sep": ema_sep,
        "atr": latest_atr,
        "threshold": threshold,
        "prior_high": prior_high,
        "prior_low": prior_low,
        "long_setup": long_setup,
        "short_setup": short_setup,
    }
    return long_setup, short_setup, features

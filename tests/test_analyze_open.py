"""Tests for open-position stop proximity helpers."""

from __future__ import annotations

from bot.analytics.directional_postmortem import _is_bot_option_symbol, _underlying_sl_metrics


def test_is_bot_option_symbol() -> None:
    assert _is_bot_option_symbol("C-BTC-73800-290526")
    assert _is_bot_option_symbol("P-ETH-2000-290526")
    assert not _is_bot_option_symbol("XLMUSD")
    assert not _is_bot_option_symbol("BTCUSD")


def test_call_underlying_sl_past_when_spot_drops_one_atr() -> None:
    adverse, threshold, _room, at_sl = _underlying_sl_metrics("call", 100.0, 500.0, 99.0, 1.0)
    assert adverse == 1.0
    assert threshold == 500.0
    assert at_sl is False
    adverse2, threshold2, room2, at_sl2 = _underlying_sl_metrics("call", 100_000.0, 500.0, 99_400.0, 1.0)
    assert adverse2 == 600.0
    assert threshold2 == 500.0
    assert at_sl2 is True
    assert room2 < 0


def test_put_underlying_sl_when_spot_rallies() -> None:
    _, _, _, at_sl = _underlying_sl_metrics("put", 100.0, 10.0, 111.0, 1.0)
    assert at_sl is True

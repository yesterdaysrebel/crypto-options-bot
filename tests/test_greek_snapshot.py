"""Greek/IV snapshot helpers for trade entry and exit."""

from __future__ import annotations

from bot.data.chain_cache import QuoteSnapshot
from bot.desk.greek_snapshot import greeks_by_symbol, trade_iv_from_symbols


def test_trade_iv_averages_legs() -> None:
    quotes = {
        "C-BTC-100000-180526": QuoteSnapshot(symbol="C-BTC-100000-180526", iv=0.50),
        "P-BTC-95000-180526": QuoteSnapshot(symbol="P-BTC-95000-180526", iv=0.70),
    }
    iv = trade_iv_from_symbols(list(quotes), quotes)
    assert iv is not None
    assert abs(iv - 0.60) < 1e-9


def test_greeks_by_symbol_skips_missing() -> None:
    quotes = {
        "C-BTC-100000-180526": QuoteSnapshot(
            symbol="C-BTC-100000-180526",
            iv=0.55,
            delta=0.45,
            gamma=0.0001,
            theta=-12.0,
            vega=8.0,
            open_interest=120.0,
        ),
    }
    g = greeks_by_symbol(["C-BTC-100000-180526", "MISSING"], quotes)
    assert "C-BTC-100000-180526" in g
    assert "MISSING" not in g
    assert g["C-BTC-100000-180526"]["delta"] == 0.45

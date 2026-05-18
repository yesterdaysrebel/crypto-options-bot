"""Multi-leg OI and greek gate helpers."""

from __future__ import annotations

import datetime as dt

from bot.config.models import Underlying
from bot.data.chain_cache import InstrumentRecord, QuoteSnapshot, StrikeSelection
from bot.desk.leg_liquidity import check_multi_leg_liquidity


def _selection(symbol: str, *, delta: float | None = 0.2, oi: float | None = 100.0) -> StrikeSelection:
    expiry = dt.datetime(2026, 5, 15, 12, 0, 0)
    inst = InstrumentRecord(
        product_id=1,
        symbol=symbol,
        underlying=Underlying.BTC,
        option_type="call",
        strike=100_000.0,
        expiry=expiry,
        lot_size=0.001,
        tick_size=0.5,
    )
    quote = QuoteSnapshot(symbol=symbol, delta=delta, open_interest=oi, iv=0.5)
    return StrikeSelection(instrument=inst, quote=quote)


def test_low_open_interest_fails() -> None:
    legs = [
        ("call", _selection("C-BTC-100000-150526", oi=200.0)),
        ("put", _selection("P-BTC-95000-150526", oi=10.0)),
    ]
    result = check_multi_leg_liquidity(legs, min_open_interest=50.0, greeks_required=True)
    assert not result.ok
    assert result.reason == "low_open_interest"
    assert result.symbol == "P-BTC-95000-150526"


def test_missing_greeks_fails() -> None:
    legs = [("wing", _selection("C-BTC-100000-150526", delta=None, oi=200.0))]
    result = check_multi_leg_liquidity(legs, min_open_interest=0.0, greeks_required=True)
    assert not result.ok
    assert result.reason == "missing_greeks"


def test_passes_with_features() -> None:
    legs = [
        ("long_put", _selection("P-BTC-90000-150526", oi=120.0)),
        ("short_put", _selection("P-BTC-95000-150526", oi=150.0)),
    ]
    result = check_multi_leg_liquidity(legs, min_open_interest=50.0, greeks_required=True)
    assert result.ok
    assert result.features is not None
    assert result.features["long_put_delta"] == 0.2
    assert result.features["short_put_iv"] == 0.5

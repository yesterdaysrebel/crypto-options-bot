"""Reusable test fixtures: synthetic option chains, candle series, and MarketState helpers."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

from bot.config.models import (
    DirectionalConfig,
    ExpiryBucket,
    IronCondorConfig,
    Underlying,
    VolStrangleConfig,
)
from bot.data.candles import Candle
from bot.data.chain_cache import (
    ChainCache,
    InstrumentRecord,
    QuoteSnapshot,
)
from bot.desk.iv_history import IvPercentileResult
from bot.strategies.base import MarketState


class _NoNetRest:
    """A REST client double — ChainCache won't issue requests in tests."""

    async def get_products(self, **_kwargs: object) -> list[dict]:
        return []

    async def get_tickers(self, **_kwargs: object) -> list[dict]:
        return []


def make_chain(
    *,
    underlying: Underlying,
    expiry: dt.datetime,
    strikes: list[int],
    spot: float,
    delta_slope: float = 15.0,
    atm_mid: float = 300.0,
    decay_per_pct: float = 25.0,
    base_spread_pct: float = 0.04,
    open_interest: float | None = None,
) -> ChainCache:
    """Synthetic option chain.

    Pricing model: mid = max(0.1, atm_mid - decay_per_pct * |distance%|).
    For atm_mid=300 and decay_per_pct=25, a strike 1% OTM has mid=275, 5% OTM has mid=175, etc.
    This guarantees OTM < ATM and avoids the unrealistic 'far-OTM has higher mid' artifact.
    """
    cache = ChainCache(_NoNetRest())  # type: ignore[arg-type]
    pid = 1000
    for s in strikes:
        for opt_letter, opt_name in [("C", "call"), ("P", "put")]:
            symbol = f"{opt_letter}-{underlying.value}-{s}-{expiry.strftime('%d%m%y')}"
            distance = (s - spot) / spot
            distance_pct_abs = abs(distance) * 100.0
            if opt_name == "call":
                delta = max(0.01, min(0.99, 0.5 - distance * delta_slope))
            else:
                delta = -max(0.01, min(0.99, 0.5 + distance * delta_slope))
            mid = max(0.1, atm_mid - decay_per_pct * distance_pct_abs)
            spread = mid * base_spread_pct
            cache._instruments_by_symbol[symbol] = InstrumentRecord(
                product_id=pid,
                symbol=symbol,
                underlying=underlying,
                option_type=opt_name,
                strike=float(s),
                expiry=expiry,
                lot_size=0.001,
                tick_size=0.5,
            )
            cache.upsert_quote(
                QuoteSnapshot(
                    symbol=symbol,
                    bid=mid - spread / 2,
                    ask=mid + spread / 2,
                    mark_price=mid,
                    iv=0.55,
                    delta=delta,
                    gamma=0.0001,
                    theta=-2.0,
                    vega=5.0,
                    rho=0.5,
                    underlying_mark=spot,
                    open_interest=open_interest,
                )
            )
            pid += 1
    return cache


def make_trend_candles(
    n: int,
    *,
    start_price: float,
    step: float,
    base_range: float,
    bucket_seconds: int = 900,
    start_epoch: int = 1_700_000_000,
) -> list[Candle]:
    """Linear trend with a fixed bar range."""
    base = start_epoch - (start_epoch % bucket_seconds)
    candles: list[Candle] = []
    last_close = start_price
    for i in range(n):
        ts = dt.datetime.fromtimestamp(base + i * bucket_seconds, tz=dt.UTC).replace(tzinfo=None)
        open_ = last_close
        close = open_ + step
        high = max(open_, close) + base_range / 2
        low = min(open_, close) - base_range / 2
        candles.append(Candle(ts=ts, open=open_, high=high, low=low, close=close, n_ticks=10))
        last_close = close
    return candles


def make_flat_candles(
    n: int,
    *,
    price: float,
    base_range: float,
    bucket_seconds: int = 900,
    start_epoch: int = 1_700_000_000,
) -> list[Candle]:
    base = start_epoch - (start_epoch % bucket_seconds)
    candles: list[Candle] = []
    for i in range(n):
        ts = dt.datetime.fromtimestamp(base + i * bucket_seconds, tz=dt.UTC).replace(tzinfo=None)
        candles.append(
            Candle(
                ts=ts,
                open=price,
                high=price + base_range / 2,
                low=price - base_range / 2,
                close=price,
                n_ticks=10,
            )
        )
    return candles


def make_breakout_candles(
    *,
    consolidation_n: int,
    breakout_n: int,
    consolidation_price: float,
    breakout_step: float,
    consolidation_range: float,
    breakout_range: float,
    bucket_seconds: int = 900,
    start_epoch: int = 1_700_000_000,
) -> list[Candle]:
    """Flat consolidation followed by a sharp breakout."""
    base = start_epoch - (start_epoch % bucket_seconds)
    candles: list[Candle] = []
    for i in range(consolidation_n):
        ts = dt.datetime.fromtimestamp(base + i * bucket_seconds, tz=dt.UTC).replace(tzinfo=None)
        candles.append(
            Candle(
                ts=ts,
                open=consolidation_price,
                high=consolidation_price + consolidation_range / 2,
                low=consolidation_price - consolidation_range / 2,
                close=consolidation_price,
                n_ticks=10,
            )
        )
    last_close = consolidation_price
    for i in range(breakout_n):
        idx = consolidation_n + i
        ts = dt.datetime.fromtimestamp(base + idx * bucket_seconds, tz=dt.UTC).replace(tzinfo=None)
        open_ = last_close
        close = open_ + breakout_step
        high = max(open_, close) + breakout_range / 2
        low = min(open_, close) - breakout_range / 2
        candles.append(Candle(ts=ts, open=open_, high=high, low=low, close=close, n_ticks=10))
        last_close = close
    return candles


def make_noisy_then_quiet_candles(
    *,
    noisy_n: int,
    quiet_n: int,
    price: float,
    noisy_amplitude: float,
    quiet_amplitude: float,
    bucket_seconds: int = 900,
    start_epoch: int = 1_700_000_000,
) -> list[Candle]:
    """First `noisy_n` bars oscillate with `noisy_amplitude`, then `quiet_n` bars with
    `quiet_amplitude`. Used by the vol-strangle test to put the recent ATR/BBwidth
    at a low percentile of the lookback distribution.
    """
    base = start_epoch - (start_epoch % bucket_seconds)
    candles: list[Candle] = []
    for i in range(noisy_n):
        ts = dt.datetime.fromtimestamp(base + i * bucket_seconds, tz=dt.UTC).replace(tzinfo=None)
        sign = 1 if i % 2 == 0 else -1
        close = price + sign * noisy_amplitude / 2
        open_ = price - sign * noisy_amplitude / 2
        high = max(open_, close) + noisy_amplitude / 4
        low = min(open_, close) - noisy_amplitude / 4
        candles.append(Candle(ts=ts, open=open_, high=high, low=low, close=close, n_ticks=10))
    for i in range(quiet_n):
        idx = noisy_n + i
        ts = dt.datetime.fromtimestamp(base + idx * bucket_seconds, tz=dt.UTC).replace(tzinfo=None)
        sign = 1 if i % 2 == 0 else -1
        close = price + sign * quiet_amplitude / 2
        open_ = price - sign * quiet_amplitude / 2
        high = max(open_, close) + quiet_amplitude / 4
        low = min(open_, close) - quiet_amplitude / 4
        candles.append(Candle(ts=ts, open=open_, high=high, low=low, close=close, n_ticks=10))
    return candles


def set_quote_open_interest(chain: ChainCache, symbol: str, open_interest: float | None) -> None:
    quote = chain.get_quote(symbol)
    if quote is None:
        raise ValueError(f"unknown symbol: {symbol}")
    chain.upsert_quote(replace(quote, open_interest=open_interest))


def make_market_state(
    now: dt.datetime,
    *,
    chain: ChainCache,
    candles_by_tf: dict[Underlying, dict[str, list[Candle]]],
    spots: dict[Underlying, float],
    iv_percentiles: dict[tuple[Underlying, ExpiryBucket], IvPercentileResult] | None = None,
) -> MarketState:
    return MarketState(
        now=now,
        chain=chain,
        candles_by_tf=candles_by_tf,
        underlying_marks=spots,
        iv_percentiles=iv_percentiles or {},
    )


def directional_cfg(**overrides) -> DirectionalConfig:
    base = {
        "id": "directional",
        "enabled": True,
        "risk_weight": 0.60,
        "risk_per_trade_pct": 0.01,
        "max_lots_cap": 5,
        "underlyings": ["BTC"],
    }
    base.update(overrides)
    return DirectionalConfig.model_validate(base)


def condor_cfg(**overrides) -> IronCondorConfig:
    base = {
        "id": "iron_condor",
        "enabled": True,
        "risk_weight": 0.25,
        "risk_per_trade_pct": 0.015,
        "max_lots_cap": 3,
        "underlyings": ["BTC"],
    }
    base.update(overrides)
    return IronCondorConfig.model_validate(base)


def strangle_cfg(**overrides) -> VolStrangleConfig:
    base = {
        "id": "vol_strangle",
        "enabled": True,
        "risk_weight": 0.15,
        "risk_per_trade_pct": 0.01,
        "max_lots_cap": 2,
        "underlyings": ["BTC"],
    }
    base.update(overrides)
    return VolStrangleConfig.model_validate(base)

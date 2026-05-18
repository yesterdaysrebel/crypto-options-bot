"""Tests for the chain cache: ATM picker, delta picker, expiry bucketing, spread filter.

AC: returns the strike whose Greek-delta is closest to a target within <50ms.
"""

from __future__ import annotations

import datetime as dt
import time

import httpx
import pytest
from bot.config.models import ExpiryBucket, Underlying
from bot.config.settings import Settings
from bot.data.chain_cache import (
    ChainCache,
    QuoteSnapshot,
    bucket_for_expiry,
    parse_symbol,
)
from bot.exchange.rest import DeltaRestClient


def test_parse_symbol_call() -> None:
    parsed = parse_symbol("C-BTC-100000-130524")
    assert parsed is not None
    opt, underlying, strike, expiry = parsed
    assert opt == "call"
    assert underlying == Underlying.BTC
    assert strike == 100000.0
    assert expiry.date() == dt.date(2024, 5, 13)


def test_parse_symbol_put() -> None:
    parsed = parse_symbol("P-ETH-3000-130524")
    assert parsed is not None
    opt, _, strike, _ = parsed
    assert opt == "put"
    assert strike == 3000.0


def test_parse_symbol_rejects_non_option() -> None:
    assert parse_symbol("MARK:BTCUSD") is None
    assert parse_symbol("BTCUSD") is None


def test_bucket_for_expiry_d1_tomorrow() -> None:
    today = dt.datetime(2026, 5, 12, 10, 0, 0)
    tomorrow_expiry = dt.datetime(2026, 5, 13, 17, 30, 0)
    assert bucket_for_expiry(today, tomorrow_expiry) == ExpiryBucket.D2


def test_bucket_for_expiry_d1_same_day() -> None:
    today = dt.datetime(2026, 5, 12, 10, 0, 0)
    same_day_expiry = dt.datetime(2026, 5, 12, 17, 30, 0)
    assert bucket_for_expiry(today, same_day_expiry) == ExpiryBucket.D1


def test_bucket_for_expiry_w1() -> None:
    monday = dt.datetime(2026, 5, 11, 10, 0, 0)
    next_friday = dt.datetime(2026, 5, 15, 17, 30, 0)
    assert bucket_for_expiry(monday, next_friday) == ExpiryBucket.W1


def test_bucket_for_expiry_w2() -> None:
    monday = dt.datetime(2026, 5, 11, 10, 0, 0)
    second_friday = dt.datetime(2026, 5, 22, 17, 30, 0)
    assert bucket_for_expiry(monday, second_friday) == ExpiryBucket.W2


def _settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _mock_rest_client(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.india.delta.exchange", transport=transport)
    return DeltaRestClient(_settings(), client=client)


def _fake_btc_chain(today: dt.date) -> tuple[list[dict], list[dict]]:
    """Return (products, tickers) for a synthetic BTC chain on `today` (D1 expiry).

    A dense strike grid around spot=100k with a linear delta proxy that spans
    [0.01, 0.99] over the grid. This guarantees strikes inside the 0.05-0.30 delta bands
    used by the iron condor / strangle pickers.
    """
    expiry_str = today.strftime("%d%m%y")
    products: list[dict] = []
    tickers: list[dict] = []
    strikes = list(range(88000, 112001, 500))  # 49 strikes from 88k to 112k
    pid = 1000
    spot = 100000.0
    for s in strikes:
        for opt_letter, opt_name in [("C", "call_options"), ("P", "put_options")]:
            sym = f"{opt_letter}-BTC-{s}-{expiry_str}"
            products.append(
                {
                    "id": pid,
                    "symbol": sym,
                    "contract_type": opt_name,
                    "contract_value": 0.001,
                    "tick_size": 0.5,
                    "state": "live",
                }
            )
            distance = (s - spot) / spot
            if opt_letter == "C":
                delta_val = max(0.01, min(0.99, 0.5 - distance * 15.0))
            else:
                delta_val = -max(0.01, min(0.99, 0.5 + distance * 15.0))
            mid = max(0.1, 50.0 + abs(distance) * 200.0)
            tickers.append(
                {
                    "symbol": sym,
                    "mark_price": str(mid),
                    "spot_price": str(spot),
                    "oi_contracts": "120",
                    "volume": "45",
                    "greeks": {
                        "delta": delta_val,
                        "gamma": 0.0001,
                        "theta": -2.0,
                        "vega": 5.0,
                        "rho": 0.5,
                        "iv": 0.55,
                    },
                    "quotes": {
                        "best_bid": str(mid * 0.98),
                        "best_ask": str(mid * 1.02),
                    },
                }
            )
            pid += 1
    return products, tickers


@pytest.mark.asyncio
async def test_refresh_instruments_loads_btc_chain() -> None:
    today = dt.date.today()
    products, _ = _fake_btc_chain(today)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": products})

    async with _mock_rest_client(handler) as rest:
        cache = ChainCache(rest)
        n = await cache.refresh_instruments(force=True)
        assert n == len(products)
        assert cache.size == n


@pytest.mark.asyncio
async def test_refresh_quotes_populates_greeks() -> None:
    today = dt.date.today()
    products, tickers = _fake_btc_chain(today)
    requests: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req.url.path)
        if req.url.path == "/v2/products":
            return httpx.Response(200, json={"success": True, "result": products})
        return httpx.Response(200, json={"success": True, "result": tickers})

    async with _mock_rest_client(handler) as rest:
        cache = ChainCache(rest)
        await cache.refresh_instruments(force=True)
        await cache.refresh_quotes()
    quote = cache.get_quote(products[0]["symbol"])
    assert quote is not None
    assert quote.delta is not None
    assert quote.open_interest == 120.0
    assert quote.volume_24h == 45.0
    assert quote.spread_pct is not None
    assert 0.03 < quote.spread_pct < 0.05


FIXED_NOW = dt.datetime(2026, 5, 13, 8, 0, 0)
FIXED_TODAY = FIXED_NOW.date()


@pytest.mark.asyncio
async def test_get_atm_picks_strike_closest_to_spot() -> None:
    products, tickers = _fake_btc_chain(FIXED_TODAY)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/products":
            return httpx.Response(200, json={"success": True, "result": products})
        return httpx.Response(200, json={"success": True, "result": tickers})

    async with _mock_rest_client(handler) as rest:
        cache = ChainCache(rest)
        await cache.refresh_instruments(force=True)
        await cache.refresh_quotes()
        selection = cache.get_atm_strike(
            Underlying.BTC,
            "call",
            ExpiryBucket.D1,
            spot_price=100100.0,
            now=FIXED_NOW,
        )
        assert selection is not None
        assert selection.instrument.strike == 100000.0
        assert selection.selection_reason == "atm"
        otm = cache.get_atm_strike(
            Underlying.BTC,
            "call",
            ExpiryBucket.D1,
            spot_price=100100.0,
            offset=1,
            now=FIXED_NOW,
        )
        assert otm is not None
        assert otm.instrument.strike == 100500.0


@pytest.mark.asyncio
async def test_get_strike_by_delta_finds_closest_target() -> None:
    products, tickers = _fake_btc_chain(FIXED_TODAY)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/products":
            return httpx.Response(200, json={"success": True, "result": products})
        return httpx.Response(200, json={"success": True, "result": tickers})

    async with _mock_rest_client(handler) as rest:
        cache = ChainCache(rest)
        await cache.refresh_instruments(force=True)
        await cache.refresh_quotes()

        start = time.perf_counter()
        selection = cache.get_strike_by_delta(
            Underlying.BTC,
            "call",
            ExpiryBucket.D1,
            target_delta=0.20,
            delta_min=0.15,
            delta_max=0.25,
            now=FIXED_NOW,
        )
        elapsed = (time.perf_counter() - start) * 1000.0
        assert elapsed < 50.0, f"get_strike_by_delta exceeded 50ms ({elapsed:.1f}ms)"

    assert selection is not None
    abs_delta = abs(selection.quote.delta or 0)
    assert 0.15 <= abs_delta <= 0.25, f"selected delta {abs_delta} outside band"
    assert selection.selection_reason.startswith("closest_delta_")


@pytest.mark.asyncio
async def test_get_strike_by_delta_respects_puts_sign() -> None:
    products, tickers = _fake_btc_chain(FIXED_TODAY)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/products":
            return httpx.Response(200, json={"success": True, "result": products})
        return httpx.Response(200, json={"success": True, "result": tickers})

    async with _mock_rest_client(handler) as rest:
        cache = ChainCache(rest)
        await cache.refresh_instruments(force=True)
        await cache.refresh_quotes()
        sel = cache.get_strike_by_delta(
            Underlying.BTC,
            "put",
            ExpiryBucket.D1,
            target_delta=0.075,
            delta_min=0.05,
            delta_max=0.10,
            now=FIXED_NOW,
        )
    assert sel is not None
    assert (sel.quote.delta or 0) < 0
    assert 0.05 <= abs(sel.quote.delta or 0) <= 0.10


@pytest.mark.asyncio
async def test_spread_filter_via_quote() -> None:
    cache_quote = QuoteSnapshot(symbol="C-BTC-100000-130524", bid=95.0, ask=105.0)
    assert cache_quote.spread_pct == pytest.approx(0.10, abs=1e-9)
    cache_quote_tight = QuoteSnapshot(symbol="C-BTC-100000-130524", bid=99.0, ask=101.0)
    assert cache_quote_tight.spread_pct == pytest.approx(0.02, abs=1e-9)


def test_upsert_quote_replaces_snapshot() -> None:
    # ChainCache doesn't strictly need a REST client to test upsert
    class _Stub:
        async def get_products(self, **_):
            return []

        async def get_tickers(self, **_):
            return []

    cache = ChainCache(_Stub())  # type: ignore[arg-type]
    cache.upsert_quote(QuoteSnapshot(symbol="X", bid=1.0, ask=2.0))
    cache.upsert_quote(QuoteSnapshot(symbol="X", bid=1.1, ask=2.1))
    q = cache.get_quote("X")
    assert q is not None
    assert q.bid == 1.1

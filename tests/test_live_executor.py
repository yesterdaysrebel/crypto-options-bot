"""Tests for LiveExecutor maker wait + IOC fallback."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from bot.config.models import StrategyId, Underlying
from bot.config.settings import Settings
from bot.data.chain_cache import ChainCache, InstrumentRecord, QuoteSnapshot
from bot.exchange.rest import DeltaRestClient
from bot.execution.live import LiveExecutor, _ioc_limit_price, _maker_limit_price
from bot.execution.router import EntryRequest
from bot.strategies.base import LegIntent


def _settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        DELTA_API_KEY="test-key",
        DELTA_API_SECRET="test-secret",
    )


class _StubRest:
    pass


def _chain() -> ChainCache:
    cache = ChainCache(_StubRest())  # type: ignore[arg-type]
    expiry = dt.datetime(2026, 5, 26, 17, 30, 0)
    cache._instruments_by_symbol["P-BTC-77000-260526"] = InstrumentRecord(
        product_id=42,
        symbol="P-BTC-77000-260526",
        underlying=Underlying.BTC,
        option_type="put",
        strike=77000.0,
        expiry=expiry,
        lot_size=0.001,
        tick_size=0.5,
    )
    cache.upsert_quote(QuoteSnapshot(symbol="P-BTC-77000-260526", bid=100.0, ask=101.0, mark_price=100.5))
    return cache


def test_maker_and_ioc_limit_prices() -> None:
    quote = QuoteSnapshot(symbol="X", bid=100.0, ask=101.0, mark_price=100.5)
    assert _maker_limit_price(quote, "buy", 0.5) == 100.0
    assert _ioc_limit_price(quote, "buy", slip_bps=50, tick=0.5) == 101.5


@pytest.mark.asyncio
async def test_live_entry_waits_for_post_only_fill() -> None:
    polls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/v2/orders":
            body = json.loads(req.content.decode())
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "id": 7,
                        "client_order_id": body["client_order_id"],
                        "state": "open",
                        "filled_size": 0,
                    },
                },
            )
        if req.method == "GET" and req.url.path == "/v2/orders":
            polls["n"] += 1
            state = "filled" if polls["n"] >= 2 else "open"
            filled = 1 if state == "filled" else 0
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {
                            "id": 7,
                            "client_order_id": req.url.params["client_order_id"],
                            "state": state,
                            "filled_size": filled,
                            "average_fill_price": 100.0 if filled else None,
                        }
                    ],
                },
            )
        return httpx.Response(404)

    rest = _mock_rest(handler)
    try:
        executor = LiveExecutor(rest, _chain())
        req = EntryRequest(
            strategy_id=StrategyId.DIRECTIONAL,
            trade_id=1,
            underlying=Underlying.BTC,
            legs=[
                LegIntent(
                    symbol="P-BTC-77000-260526",
                    side="buy",
                    option_type="put",
                    strike=77000.0,
                    expiry=dt.datetime(2026, 5, 26, 17, 30, 0),
                )
            ],
            lots=1,
            intent_rationale="test",
            maker_timeout_seconds=3.0,
        )
        result = await executor.submit_entry(req)
    finally:
        await rest.aclose()

    assert result.success is True
    assert result.fills[0].state == "filled"
    assert polls["n"] >= 2


@pytest.mark.asyncio
async def test_live_entry_falls_back_to_ioc_when_maker_times_out() -> None:
    posts: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST" and req.url.path == "/v2/orders":
            body = json.loads(req.content.decode())
            posts.append(body)
            if body.get("time_in_force") == "ioc":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "result": {
                            "id": 9,
                            "client_order_id": body["client_order_id"],
                            "state": "filled",
                            "filled_size": 1,
                            "average_fill_price": 101.0,
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "id": 8,
                        "client_order_id": body["client_order_id"],
                        "state": "open",
                        "filled_size": 0,
                    },
                },
            )
        if req.method == "GET" and req.url.path == "/v2/orders":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": [
                        {
                            "id": 8,
                            "client_order_id": req.url.params["client_order_id"],
                            "state": "open",
                            "filled_size": 0,
                        }
                    ],
                },
            )
        if req.method == "DELETE" and req.url.path == "/v2/orders":
            return httpx.Response(200, json={"success": True, "result": {}})
        return httpx.Response(404)

    rest = _mock_rest(handler)
    try:
        executor = LiveExecutor(rest, _chain())
        req = EntryRequest(
            strategy_id=StrategyId.DIRECTIONAL,
            trade_id=2,
            underlying=Underlying.BTC,
            legs=[
                LegIntent(
                    symbol="P-BTC-77000-260526",
                    side="buy",
                    option_type="put",
                    strike=77000.0,
                    expiry=dt.datetime(2026, 5, 26, 17, 30, 0),
                )
            ],
            lots=1,
            intent_rationale="test",
            maker_timeout_seconds=0.1,
        )
        result = await executor.submit_entry(req)
    finally:
        await rest.aclose()

    assert result.success is True
    assert len(posts) == 2
    assert posts[0].get("post_only") == "true"
    assert posts[1].get("time_in_force") == "ioc"


def _mock_rest(handler) -> DeltaRestClient:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.india.delta.exchange", transport=transport)
    return DeltaRestClient(_settings(), client=client)

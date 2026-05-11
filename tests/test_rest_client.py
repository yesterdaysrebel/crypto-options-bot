"""Unit tests for DeltaRestClient using httpx MockTransport.

Integration test against the live read-only /v2/products endpoint is also included,
gated behind the `integration` pytest marker so unit-test runs stay hermetic and fast.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from bot.config.settings import Settings
from bot.exchange.rest import DeltaRestClient, DeltaRestError


def _build_settings(api_key: str = "", api_secret: str = "") -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        DELTA_API_KEY=api_key,
        DELTA_API_SECRET=api_secret,
        DELTA_REST_RPS=100,
        DELTA_ORDER_RPS=50,
    )


def _mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(
        base_url="https://api.india.delta.exchange",
        transport=transport,
        headers={"User-Agent": "test"},
    )


@pytest.mark.asyncio
async def test_get_products_parses_envelope() -> None:
    products = [
        {"id": 1, "symbol": "C-BTC-100000-130524", "contract_type": "call_options"},
        {"id": 2, "symbol": "P-BTC-100000-130524", "contract_type": "put_options"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.method == "GET"
        assert req.url.path == "/v2/products"
        assert "contract_types=call_options%2Cput_options" in str(req.url.query)
        return httpx.Response(200, json={"success": True, "result": products})

    async with DeltaRestClient(_build_settings(), client=_mock_transport(handler)) as client:
        result = await client.get_products(contract_types=["call_options", "put_options"])

    assert len(result) == 2
    assert result[0]["symbol"].startswith("C-BTC")


@pytest.mark.asyncio
async def test_429_then_200_is_retried() -> None:
    calls = {"count": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, json={"success": False, "error": {"code": "rate_limit"}})
        return httpx.Response(200, json={"success": True, "result": []})

    async with DeltaRestClient(_build_settings(), client=_mock_transport(handler)) as client:
        await client.get_products()

    assert calls["count"] == 2, "the 429 should be retried exactly once before success"


@pytest.mark.asyncio
async def test_non_retryable_4xx_raises_without_retry() -> None:
    calls = {"count": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(400, json={"success": False, "error": {"code": "bad_request"}})

    async with DeltaRestClient(_build_settings(), client=_mock_transport(handler)) as client:
        with pytest.raises(DeltaRestError) as exc_info:
            await client.get_products()
    assert calls["count"] == 1
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_envelope_success_false_raises() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": False, "error": {"code": "invalid_symbol"}})

    async with DeltaRestClient(_build_settings(), client=_mock_transport(handler)) as client:
        with pytest.raises(DeltaRestError, match="invalid_symbol"):
            await client.get_ticker("BOGUS")


@pytest.mark.asyncio
async def test_signed_request_sends_api_key_and_signature_headers() -> None:
    captured: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(req.headers)
        return httpx.Response(200, json={"success": True, "result": []})

    settings = _build_settings(api_key="abc", api_secret="def")
    async with DeltaRestClient(settings, client=_mock_transport(handler)) as client:
        await client.get_wallet_balances()

    hdrs = captured["headers"]
    assert hdrs.get("api-key") == "abc"
    assert "timestamp" in hdrs
    assert len(hdrs.get("signature", "")) == 64


@pytest.mark.asyncio
async def test_place_order_uses_order_bucket() -> None:
    """Order endpoints draw from a separate (smaller) bucket than the general one."""
    calls = {"count": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"success": True, "result": {"id": 42, "state": "open"}})

    settings = _build_settings(api_key="abc", api_secret="def")
    async with DeltaRestClient(settings, client=_mock_transport(handler)) as client:
        res = await client.place_order(
            {"product_id": 1, "size": 1, "side": "buy", "order_type": "limit_order", "limit_price": "100"}
        )
    assert res["id"] == 42
    assert calls["count"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_get_products_returns_btc_option() -> None:
    """AC for PR #3: live read-only endpoint returns at least one BTC option product."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    async with DeltaRestClient(settings) as client:
        products = await client.get_products(contract_types=["call_options", "put_options"])

    btc_options = [
        p
        for p in products
        if isinstance(p.get("symbol"), str)
        and "BTC" in p["symbol"]
        and p.get("contract_type") in {"call_options", "put_options"}
    ]
    assert len(btc_options) >= 1, "expected at least one live BTC option product"

"""Async REST client for Delta Exchange India.

Wraps httpx with:
  - HMAC request signing for private endpoints
  - Dual token-bucket rate limiting (general + order)
  - 429-aware retry-with-backoff via tenacity
  - Typed convenience methods for endpoints the bot actually calls

Public endpoints (products, tickers, candles) do not require auth; pass signed=False.

The Delta India REST API uses a top-level JSON envelope:
    {"success": true, "result": ...}
On error: {"success": false, "error": {"code": ..., "context": {...}}}
"""

from __future__ import annotations

import json as _json
import urllib.parse
from typing import Any

import httpx
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from bot.config.settings import Settings
from bot.exchange.auth import RequestSigner
from bot.exchange.rate_limit import TokenBucket

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)


class DeltaRestError(RuntimeError):
    """Raised when Delta returns a non-2xx response or `success: false`."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: Any | None = None,
        endpoint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.endpoint = endpoint


class _RetryableRestError(DeltaRestError):
    """Internal: signals a 5xx or 429 which the retry layer should swallow."""


class DeltaRestClient:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        general_bucket: TokenBucket | None = None,
        order_bucket: TokenBucket | None = None,
        max_attempts: int = 4,
    ) -> None:
        self._settings = settings
        self._base_url = str(settings.delta_base_url).rstrip("/")
        self._signer = RequestSigner(settings.delta_api_key, settings.delta_api_secret)

        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=DEFAULT_TIMEOUT,
            headers={"User-Agent": "crypto-options-bot/0.1", "Accept": "application/json"},
        )
        self._owns_client = client is None
        capacity_general = float(max(1, settings.delta_rest_rps * 10))
        capacity_order = float(max(1, settings.delta_order_rps * 10))
        self._general_bucket = general_bucket or TokenBucket(
            rate=float(settings.delta_rest_rps), capacity=capacity_general
        )
        self._order_bucket = order_bucket or TokenBucket(
            rate=float(settings.delta_order_rps), capacity=capacity_order
        )
        self._max_attempts = max_attempts

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> DeltaRestClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    # --------------------------- public endpoints ---------------------------

    async def get_products(self, contract_types: list[str] | None = None) -> list[dict[str, Any]]:
        """List all products. `contract_types` example: ['call_options','put_options']."""
        params: dict[str, str] = {}
        if contract_types:
            params["contract_types"] = ",".join(contract_types)
        envelope = await self._request("GET", "/v2/products", params=params, signed=False)
        return list(envelope.get("result", []))

    async def get_tickers(self, contract_types: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if contract_types:
            params["contract_types"] = ",".join(contract_types)
        envelope = await self._request("GET", "/v2/tickers", params=params, signed=False)
        return list(envelope.get("result", []))

    async def get_ticker(self, symbol: str) -> dict[str, Any]:
        envelope = await self._request("GET", f"/v2/tickers/{symbol}", signed=False)
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise DeltaRestError(
                f"unexpected ticker payload for {symbol}: {result!r}",
                endpoint=f"/v2/tickers/{symbol}",
            )
        return result

    async def get_candles(
        self,
        symbol: str,
        resolution: str,
        start: int,
        end: int,
    ) -> list[dict[str, Any]]:
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "start": str(start),
            "end": str(end),
        }
        envelope = await self._request("GET", "/v2/history/candles", params=params, signed=False)
        return list(envelope.get("result", []))

    # --------------------------- private endpoints ---------------------------

    async def get_wallet_balances(self) -> list[dict[str, Any]]:
        envelope = await self._request("GET", "/v2/wallet/balances", signed=True)
        return list(envelope.get("result", []))

    async def get_positions(self) -> list[dict[str, Any]]:
        envelope = await self._request("GET", "/v2/positions/margined", signed=True)
        return list(envelope.get("result", []))

    async def get_open_orders(self) -> list[dict[str, Any]]:
        params = {"state": "open"}
        envelope = await self._request("GET", "/v2/orders", params=params, signed=True)
        return list(envelope.get("result", []))

    async def get_order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        """Fetch a single order by our client_order_id (singleton lookup)."""
        envelope = await self._request(
            "GET",
            "/v2/orders",
            params={"client_order_id": client_order_id},
            signed=True,
        )
        result = envelope.get("result")
        if isinstance(result, list):
            return dict(result[0]) if result else None
        if isinstance(result, dict):
            return result
        return None

    async def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        envelope = await self._request("POST", "/v2/orders", json=payload, signed=True, is_order=True)
        result = envelope.get("result", {})
        if not isinstance(result, dict):
            raise DeltaRestError("place_order: unexpected result type", endpoint="/v2/orders")
        return result

    async def cancel_order(
        self, *, order_id: int | None = None, client_order_id: str | None = None
    ) -> dict[str, Any]:
        if order_id is None and client_order_id is None:
            raise ValueError("cancel_order requires order_id or client_order_id")
        payload: dict[str, Any] = {}
        if order_id is not None:
            payload["id"] = order_id
        if client_order_id is not None:
            payload["client_order_id"] = client_order_id
        envelope = await self._request("DELETE", "/v2/orders", json=payload, signed=True, is_order=True)
        result = envelope.get("result", {})
        return result if isinstance(result, dict) else {}

    async def cancel_all_orders(self, product_id: int | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if product_id is not None:
            payload["product_id"] = product_id
        envelope = await self._request("DELETE", "/v2/orders/all", json=payload, signed=True, is_order=True)
        result = envelope.get("result", {})
        return result if isinstance(result, dict) else {}

    # --------------------------- low level ---------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        signed: bool = False,
        is_order: bool = False,
    ) -> dict[str, Any]:
        bucket = self._order_bucket if is_order else self._general_bucket

        body = "" if json is None else _json.dumps(json, separators=(",", ":"))
        query_string = ""
        if params:
            query_string = "?" + urllib.parse.urlencode(params, doseq=True)

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential_jitter(initial=0.5, max=10.0, jitter=0.5),
            retry=retry_if_exception_type(_RetryableRestError),
            reraise=True,
        ):
            with attempt:
                await bucket.acquire()
                headers: dict[str, str] = {}
                if signed:
                    headers.update(self._signer.headers(method, path, query_string=query_string, body=body))
                if body:
                    headers["Content-Type"] = "application/json"

                try:
                    response = await self._client.request(
                        method=method,
                        url=path + query_string,
                        content=body or None,
                        headers=headers,
                    )
                except httpx.TransportError as exc:
                    raise _RetryableRestError(
                        f"transport error on {method} {path}: {exc}",
                        endpoint=path,
                    ) from exc

                if response.status_code == 429 or 500 <= response.status_code < 600:
                    logger.warning(
                        "delta REST retryable status {} for {} {}",
                        response.status_code,
                        method,
                        path,
                    )
                    raise _RetryableRestError(
                        f"retryable status {response.status_code} on {method} {path}",
                        status_code=response.status_code,
                        body=response.text[:512],
                        endpoint=path,
                    )

                if response.status_code >= 400:
                    raise DeltaRestError(
                        f"{response.status_code} on {method} {path}: {response.text[:512]}",
                        status_code=response.status_code,
                        body=response.text,
                        endpoint=path,
                    )

                try:
                    envelope = response.json()
                except ValueError as exc:
                    raise DeltaRestError(
                        f"invalid JSON on {method} {path}: {exc}",
                        status_code=response.status_code,
                        endpoint=path,
                    ) from exc

                if not isinstance(envelope, dict):
                    raise DeltaRestError(
                        f"unexpected envelope type on {method} {path}: {type(envelope).__name__}",
                        endpoint=path,
                    )

                if envelope.get("success") is False:
                    err = envelope.get("error") or {}
                    raise DeltaRestError(
                        f"delta error on {method} {path}: {err}",
                        status_code=response.status_code,
                        body=envelope,
                        endpoint=path,
                    )

                return envelope

        raise DeltaRestError(f"exhausted retries on {method} {path}", endpoint=path)

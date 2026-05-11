"""Delta Exchange India WebSocket client.

Connects, subscribes to channels (v2/ticker, mark_price, candlestick_*, ...), and pushes
parsed messages onto an asyncio.Queue. Reconnects with jittered exponential backoff on any
connection loss; resubscribes automatically.

Delta WS protocol (subset we use):
    Subscribe:
        {"type": "subscribe", "payload": {"channels": [{"name": "v2/ticker", "symbols": ["MARK:BTCUSD"]}]}}
    Heartbeat:
        {"type": "enable_heartbeat"}  -> server replies {"type":"heartbeat", ...} every 30s
    Auth (private channels - we don't need any in v1):
        {"type": "auth", "payload": {"api-key": "...", "signature": "...", "timestamp": "..."}}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from typing import Any

import websockets
from loguru import logger
from websockets.exceptions import ConnectionClosed

from bot.config.settings import Settings

DEFAULT_PING_INTERVAL = 20.0
DEFAULT_PING_TIMEOUT = 20.0
DEFAULT_MAX_BACKOFF = 60.0
DEFAULT_INITIAL_BACKOFF = 1.0


@dataclass(frozen=True)
class Subscription:
    """A single (channel, symbols) subscription."""

    channel: str
    symbols: tuple[str, ...]

    def payload(self) -> dict[str, Any]:
        return {"name": self.channel, "symbols": list(self.symbols)}


@dataclass
class WsStats:
    """Diagnostic counters for `make status` and the Prometheus textfile collector."""

    connected: bool = False
    connection_count: int = 0
    last_message_ts: float = field(default_factory=time.monotonic)
    messages_received: int = 0
    reconnects: int = 0

    @property
    def heartbeat_age_seconds(self) -> float:
        return time.monotonic() - self.last_message_ts


class DeltaWebSocketClient:
    """Reconnecting WS subscriber. Public-channel only (sufficient for v1)."""

    def __init__(
        self,
        settings: Settings,
        subscriptions: Iterable[Subscription],
        *,
        queue_maxsize: int = 4096,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        ping_timeout: float = DEFAULT_PING_TIMEOUT,
    ) -> None:
        self._settings = settings
        self._subs = tuple(subscriptions)
        if not self._subs:
            raise ValueError("at least one Subscription is required")
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_maxsize)
        self._stop = asyncio.Event()
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self.stats = WsStats()

    @property
    def queue(self) -> asyncio.Queue[dict[str, Any]]:
        return self._queue

    def stop(self) -> None:
        self._stop.set()

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over parsed messages. Yields until `stop()` is called."""
        while not self._stop.is_set():
            try:
                yield await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue

    async def run(self) -> None:
        """Run the reconnect loop until `stop()` is called."""
        backoff = self._initial_backoff
        while not self._stop.is_set():
            try:
                await self._connect_and_pump()
                backoff = self._initial_backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("WS error: {!r}; reconnecting in {:.1f}s", exc, backoff)
                self.stats.connected = False
                self.stats.reconnects += 1
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                jitter = random.uniform(0.5, 1.5)
                backoff = min(self._max_backoff, max(self._initial_backoff, backoff * 2 * jitter))

    async def _connect_and_pump(self) -> None:
        url = self._settings.delta_ws_url
        logger.info("WS connecting to {} with {} subscriptions", url, len(self._subs))
        async with websockets.connect(
            url,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            close_timeout=5.0,
            open_timeout=10.0,
            max_size=2**20,
        ) as ws:
            self.stats.connected = True
            self.stats.connection_count += 1
            await self._subscribe(ws)
            await self._enable_heartbeat(ws)
            await self._pump(ws)

    async def _subscribe(self, ws: Any) -> None:
        payload = {"type": "subscribe", "payload": {"channels": [s.payload() for s in self._subs]}}
        await ws.send(json.dumps(payload))
        logger.debug("WS sent subscribe: {}", payload)

    async def _enable_heartbeat(self, ws: Any) -> None:
        await ws.send(json.dumps({"type": "enable_heartbeat"}))

    async def _pump(self, ws: Any) -> None:
        try:
            while not self._stop.is_set():
                raw = await ws.recv()
                self.stats.last_message_ts = time.monotonic()
                self.stats.messages_received += 1
                msg = self._parse(raw)
                if msg is None:
                    continue
                if msg.get("type") == "heartbeat":
                    continue
                with contextlib.suppress(asyncio.QueueFull):
                    self._queue.put_nowait(msg)
        except ConnectionClosed as exc:
            close_code = exc.rcvd.code if exc.rcvd else None
            close_reason = exc.rcvd.reason if exc.rcvd else None
            logger.info("WS connection closed: code={} reason={!r}", close_code, close_reason)
            raise

    @staticmethod
    def _parse(raw: str | bytes) -> dict[str, Any] | None:
        try:
            obj = json.loads(raw)
        except ValueError:
            logger.warning("WS dropped non-JSON frame")
            return None
        if not isinstance(obj, dict):
            return None
        return obj

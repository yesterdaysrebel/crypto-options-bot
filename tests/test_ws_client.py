"""Tests for DeltaWebSocketClient. Uses a localhost WS server to validate:
- subscribe payload format
- message delivery to queue
- heartbeat enable
- reconnect on dropped connection with backoff
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
import websockets
from bot.config.settings import Settings
from bot.exchange.ws import DeltaWebSocketClient, Subscription


class _FakeServer:
    def __init__(self) -> None:
        self.received_subscribes: list[dict] = []
        self.received_heartbeats: int = 0
        self.connections: int = 0
        self._messages_to_send: list[str] = []
        self._drop_after: int | None = None
        self._server: websockets.Server | None = None

    async def start(self) -> int:
        async def handler(ws: websockets.ServerConnection) -> None:
            self.connections += 1
            try:
                # send the queued messages immediately on each connection
                for raw in self._messages_to_send:
                    await ws.send(raw)
                count = 0
                async for raw in ws:
                    if isinstance(raw, bytes):
                        continue
                    msg = json.loads(raw)
                    if msg.get("type") == "subscribe":
                        self.received_subscribes.append(msg)
                    elif msg.get("type") == "enable_heartbeat":
                        self.received_heartbeats += 1
                    count += 1
                    if self._drop_after is not None and count >= self._drop_after:
                        await ws.close()
                        return
            except websockets.ConnectionClosed:
                return

        self._server = await websockets.serve(handler, "127.0.0.1", 0)
        sock = next(iter(self._server.sockets))
        return sock.getsockname()[1]

    def queue_send(self, msg: dict) -> None:
        self._messages_to_send.append(json.dumps(msg))

    def drop_after(self, n: int) -> None:
        self._drop_after = n

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def server() -> AsyncIterator[_FakeServer]:
    s = _FakeServer()
    yield s
    await s.stop()


def _settings_for_port(port: int) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        DELTA_WS_URL=f"ws://127.0.0.1:{port}",
    )


@pytest.mark.asyncio
async def test_subscribe_payload_format_and_message_delivery(server: _FakeServer) -> None:
    server.queue_send({"type": "v2/ticker", "symbol": "MARK:BTCUSD", "mark_price": "100000"})
    port = await server.start()
    settings = _settings_for_port(port)

    client = DeltaWebSocketClient(
        settings,
        [Subscription("v2/ticker", ("MARK:BTCUSD", "MARK:ETHUSD"))],
        initial_backoff=0.05,
        max_backoff=0.10,
        ping_interval=60,
        ping_timeout=60,
    )

    runner = asyncio.create_task(client.run())
    try:
        async with asyncio.timeout(3.0):
            while not server.received_subscribes:
                await asyncio.sleep(0.02)
            msg = await asyncio.wait_for(client.queue.get(), timeout=2.0)
        assert msg["type"] == "v2/ticker"
        assert msg["symbol"] == "MARK:BTCUSD"
        sub = server.received_subscribes[0]
        assert sub["type"] == "subscribe"
        channels = sub["payload"]["channels"]
        assert channels == [{"name": "v2/ticker", "symbols": ["MARK:BTCUSD", "MARK:ETHUSD"]}]
        assert server.received_heartbeats >= 1
    finally:
        client.stop()
        runner.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await runner


@pytest.mark.asyncio
async def test_reconnects_on_dropped_connection(server: _FakeServer) -> None:
    server.queue_send({"type": "v2/ticker", "symbol": "MARK:BTCUSD"})
    server.drop_after(2)
    port = await server.start()
    settings = _settings_for_port(port)

    client = DeltaWebSocketClient(
        settings,
        [Subscription("v2/ticker", ("MARK:BTCUSD",))],
        initial_backoff=0.05,
        max_backoff=0.10,
        ping_interval=60,
        ping_timeout=60,
    )

    runner = asyncio.create_task(client.run())
    try:
        async with asyncio.timeout(5.0):
            while server.connections < 2:
                await asyncio.sleep(0.05)
        assert client.stats.reconnects >= 1
        assert client.stats.connection_count >= 2
    finally:
        client.stop()
        runner.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await runner


@pytest.mark.asyncio
async def test_empty_subscriptions_raise() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        DeltaWebSocketClient(settings, [])


@pytest.mark.asyncio
async def test_heartbeat_messages_are_filtered_out(server: _FakeServer) -> None:
    server.queue_send({"type": "heartbeat", "ts": 1700000000})
    server.queue_send({"type": "v2/ticker", "symbol": "MARK:BTCUSD"})
    port = await server.start()
    settings = _settings_for_port(port)

    client = DeltaWebSocketClient(
        settings,
        [Subscription("v2/ticker", ("MARK:BTCUSD",))],
        initial_backoff=0.05,
        ping_interval=60,
        ping_timeout=60,
    )

    runner = asyncio.create_task(client.run())
    try:
        msg = await asyncio.wait_for(client.queue.get(), timeout=3.0)
        assert msg["type"] == "v2/ticker"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(client.queue.get(), timeout=0.3)
    finally:
        client.stop()
        runner.cancel()
        with pytest.raises((asyncio.CancelledError, Exception)):
            await runner

"""Tiny aiohttp-free HTTP server exposing `/metrics` and `/health`.

We deliberately avoid pulling in a full web framework. A handful of routes served by
stdlib asyncio's StreamReader/Writer is enough. The server runs on the asyncio loop and
is cancelled at shutdown.

Endpoints:
  GET /metrics  -> 200 with Prometheus text format
  GET /health   -> 200 {"status":"ok", ...} if liveness check passes, else 503
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
from collections.abc import Awaitable, Callable

from loguru import logger

from bot.observability.metrics import MetricsRegistry


class MetricsServer:
    def __init__(
        self,
        registry: MetricsRegistry,
        *,
        host: str = "127.0.0.1",
        port: int = 9091,
        liveness_check: Callable[[], Awaitable[bool]] | None = None,
        liveness_extra: Callable[[], Awaitable[dict[str, object]]] | None = None,
    ) -> None:
        self._registry = registry
        self._host = host
        self._port = port
        self._liveness_check = liveness_check
        self._liveness_extra = liveness_extra
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        logger.info("metrics server listening on http://{}:{}", self._host, self._port)

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        finally:
            self._server = None

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                await self._write_response(writer, 400, b"Bad Request", "text/plain")
                return
            method, path = parts[0], parts[1]
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            if method != "GET":
                await self._write_response(writer, 405, b"Method Not Allowed", "text/plain")
                return
            if path == "/metrics":
                body = self._registry.render()
                await self._write_response(writer, 200, body, "text/plain; version=0.0.4")
            elif path == "/health":
                status, payload = await self._health_payload()
                await self._write_response(
                    writer,
                    status,
                    json.dumps(payload).encode("utf-8"),
                    "application/json",
                )
            else:
                await self._write_response(writer, 404, b"not found", "text/plain")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _health_payload(self) -> tuple[int, dict[str, object]]:
        ok = True
        if self._liveness_check is not None:
            try:
                ok = bool(await self._liveness_check())
            except Exception as exc:
                logger.warning("liveness_check raised: {}", exc)
                ok = False
        extra: dict[str, object] = {}
        if self._liveness_extra is not None:
            try:
                extra = dict(await self._liveness_extra())
            except Exception as exc:
                logger.warning("liveness_extra raised: {}", exc)
        payload: dict[str, object] = {
            "status": "ok" if ok else "unhealthy",
            "ts": dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat(),
            **extra,
        }
        return (200 if ok else 503, payload)

    @staticmethod
    async def _write_response(
        writer: asyncio.StreamWriter,
        status: int,
        body: bytes,
        content_type: str,
    ) -> None:
        reason = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            405: "Method Not Allowed",
            503: "Service Unavailable",
        }.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        await writer.drain()


__all__ = ["MetricsServer"]

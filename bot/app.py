"""Main bot loop (skeleton). Subsequent PRs flesh out each subsystem."""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time

from loguru import logger

from bot.config.settings import Settings
from bot.observability.metrics import MetricsRegistry
from bot.observability.server import MetricsServer


async def run() -> None:
    """Boot a minimal HTTP server on :9091 so Docker HEALTHCHECK and deploy/health-check.sh
    succeed, while the full trading loop is still TODO. Binds 0.0.0.0 so published ports work."""

    settings = Settings()
    logger.info("crypto-options-bot starting (skeleton, mode={})", settings.mode.value)

    registry = MetricsRegistry()
    registry.last_tick_seconds.set(time.time())

    async def _liveness() -> bool:
        return True

    async def _liveness_extra() -> dict[str, object]:
        return {"mode": settings.mode.value, "skeleton": True}

    server = MetricsServer(
        registry,
        host="0.0.0.0",
        port=settings.prom_http_port,
        liveness_check=_liveness,
        liveness_extra=_liveness_extra,
    )
    await server.start()

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("signal received, requesting graceful shutdown")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    try:
        await stop_event.wait()
    finally:
        await server.stop()
    logger.info("crypto-options-bot stopped")

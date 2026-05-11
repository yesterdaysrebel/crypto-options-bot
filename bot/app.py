"""Main bot loop (skeleton). Subsequent PRs flesh out each subsystem."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from loguru import logger


async def run() -> None:
    """Skeleton entry point. The full lifecycle (executor + risk + dispatcher + analytics)
    is wired together in PR #20-#23. For now this keeps a process responsive to signals so
    the systemd unit and Docker healthchecks can exercise it without a configured Delta key."""

    logger.info("crypto-options-bot starting (skeleton)")

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("signal received, requesting graceful shutdown")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()
    logger.info("crypto-options-bot stopped")

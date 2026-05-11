"""Process-wide Loguru configuration (stderr + rotating file under `logs_dir`)."""

from __future__ import annotations

import sys

from loguru import logger

from bot.config.settings import Settings


def configure_logging(settings: Settings) -> None:
    """Idempotent enough for tests: removes default sink, adds stderr + `logs_dir/bot.log`."""
    logger.remove()
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.logs_dir / "bot.log"
    logger.add(
        sys.stderr,
        level=settings.log_level.value,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    )
    logger.add(
        log_path,
        level=settings.log_level.value,
        rotation="50 MB",
        retention="14 days",
        compression="gz",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function} - {message}",
    )
    logger.info("logging to stderr and {}", log_path)


__all__ = ["configure_logging"]

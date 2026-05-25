"""Process-wide Loguru configuration (stderr + rotating file under `logs_dir`)."""

from __future__ import annotations

import datetime as dt
import sys
from typing import TYPE_CHECKING

from loguru import logger

from bot.config.settings import Settings
from bot.risk.window import IST

if TYPE_CHECKING:
    from loguru import Record

_LOG_FORMAT_STDERR = (
    "<green>{time:YYYY-MM-DD HH:mm:ss} IST</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>"
)
_LOG_FORMAT_FILE = "{time:YYYY-MM-DD HH:mm:ss} IST | {level: <8} | {name}:{function} - {message}"


def _ist_log_record_patcher(record: Record) -> None:
    """Render all log timestamps on the IST wall clock (independent of host TZ).

    Keep Loguru's ``record["time"]`` object and only convert timezone so ``{time:...}``
    format tokens still work (replacing with a bare ``datetime.now()`` breaks formatting).
    """
    t = record["time"]
    if not isinstance(t, dt.datetime):
        return
    if t.tzinfo is None:
        # Loguru uses naive local time; production container sets TZ=Asia/Kolkata.
        t = t.replace(tzinfo=IST)
    else:
        t = t.astimezone(IST)
    record["time"] = t


def configure_logging(settings: Settings) -> None:
    """Idempotent enough for tests: removes default sink, adds stderr + `logs_dir/bot.log`."""
    logger.remove()
    logger.configure(patcher=_ist_log_record_patcher)
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.logs_dir / "bot.log"
    logger.add(
        sys.stderr,
        level=settings.log_level.value,
        format=_LOG_FORMAT_STDERR,
    )
    logger.add(
        log_path,
        level=settings.log_level.value,
        rotation="50 MB",
        retention="14 days",
        compression="gz",
        format=_LOG_FORMAT_FILE,
    )
    logger.info("logging to stderr and {} (timestamps IST)", log_path)


__all__ = ["configure_logging"]

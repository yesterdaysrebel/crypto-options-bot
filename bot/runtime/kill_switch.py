"""Kill switch: clean cancel-all + persisted state for cooperative shutdown.

There are three distinct triggers that can request a shutdown:

  * SIGTERM / SIGINT from systemd or Ctrl-C  — graceful, expected.
  * Risk circuit breaker (lifetime drawdown) — cancels all open orders, closes any
    in-flight positions, and writes a marker file so the next boot refuses to start
    until `make resume` is invoked.
  * Operator file flag (`runtime/kill.flag`)  — a watcher reads this file every tick
    and trips the switch if present. Useful when the user wants to halt without SSH.

The KillSwitch consolidates all three into a single asyncio.Event and exposes a `shutdown`
coroutine that performs the cancel-all multi-leg aware sweep and persists the reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from loguru import logger

from bot.execution.router import ExecutionRouter


class KillSwitchReason(StrEnum):
    SIGTERM = "sigterm"
    SIGINT = "sigint"
    CIRCUIT_BREAKER = "circuit_breaker"
    OPERATOR_FILE = "operator_file"
    MANUAL = "manual"
    ERROR = "error"


@dataclass
class ShutdownReport:
    requested_at: dt.datetime
    completed_at: dt.datetime | None
    reason: KillSwitchReason
    cancellations: dict[int, int] = field(default_factory=dict)  # trade_id -> orders cancelled
    error: str | None = None
    persisted_path: Path | None = None


class KillSwitch:
    """Cooperative shutdown coordinator.

    Usage:
        kill = KillSwitch(executor=executor, state_path=Path("runtime/shutdown.json"))
        install_signal_handlers(kill)
        try:
            while not kill.requested:
                await tick(...)
                kill.poll_file(Path("runtime/kill.flag"))
        finally:
            await kill.shutdown(open_trade_ids=open_trade_ids)
    """

    def __init__(
        self,
        executor: ExecutionRouter,
        state_path: Path,
        *,
        on_shutdown: Callable[[ShutdownReport], Awaitable[None]] | None = None,
    ) -> None:
        self._executor = executor
        self._state_path = state_path
        self._on_shutdown = on_shutdown
        self._event = asyncio.Event()
        self._reason: KillSwitchReason | None = None
        self._completed = False
        self._report: ShutdownReport | None = None
        self._state_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> KillSwitchReason | None:
        return self._reason

    @property
    def report(self) -> ShutdownReport | None:
        return self._report

    async def wait(self) -> KillSwitchReason:
        await self._event.wait()
        assert self._reason is not None
        return self._reason

    def trip(self, reason: KillSwitchReason) -> None:
        if self._event.is_set():
            return
        self._reason = reason
        self._event.set()
        logger.warning("kill switch tripped: reason={}", reason.value)

    def poll_file(self, path: Path) -> bool:
        """Check operator flag file; trip if present. Returns True if tripped."""
        if path.exists():
            self.trip(KillSwitchReason.OPERATOR_FILE)
            return True
        return False

    async def shutdown(self, *, open_trade_ids: list[int]) -> ShutdownReport:
        if self._completed and self._report is not None:
            return self._report
        if self._reason is None:
            self._reason = KillSwitchReason.MANUAL
        report = ShutdownReport(
            requested_at=_now(),
            completed_at=None,
            reason=self._reason,
        )
        try:
            for trade_id in open_trade_ids:
                cancelled = await self._executor.cancel_all_for_trade(trade_id)
                report.cancellations[trade_id] = cancelled
        except Exception as exc:
            report.error = f"cancel_all_failed: {exc!r}"
            logger.exception("kill_switch.shutdown encountered cancel error")
        report.completed_at = _now()
        report.persisted_path = self._persist_state(report)
        if self._on_shutdown is not None:
            with contextlib.suppress(Exception):
                await self._on_shutdown(report)
        self._completed = True
        self._report = report
        return report

    def _persist_state(self, report: ShutdownReport) -> Path:
        payload = {
            "reason": report.reason.value,
            "requested_at": report.requested_at.isoformat(),
            "completed_at": report.completed_at.isoformat() if report.completed_at else None,
            "cancellations": report.cancellations,
            "error": report.error,
        }
        self._state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return self._state_path


def install_signal_handlers(kill: KillSwitch) -> None:
    """Attach SIGTERM/SIGINT handlers to the running asyncio loop.

    Windows-only test runners may not support `add_signal_handler`; we suppress the
    NotImplementedError so the kill switch can still be tripped manually.
    """
    loop = asyncio.get_running_loop()

    def _handler(reason: KillSwitchReason) -> Callable[[], None]:
        def _fn() -> None:
            kill.trip(reason)

        return _fn

    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGTERM, _handler(KillSwitchReason.SIGTERM))
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, _handler(KillSwitchReason.SIGINT))


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


__all__ = ["KillSwitch", "KillSwitchReason", "ShutdownReport", "install_signal_handlers"]

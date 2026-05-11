"""Tests for the kill switch, operator file polling, and persisted shutdown state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from bot.execution.router import (
    EntryRequest,
    EntryResult,
    ExecutionRouter,
    ExitRequest,
    ExitResult,
    LegSide,
)
from bot.runtime.kill_switch import KillSwitch, KillSwitchReason


class _StubExecutor(ExecutionRouter):
    def __init__(self) -> None:
        self.cancel_calls: list[int] = []

    async def submit_entry(self, req: EntryRequest) -> EntryResult:
        raise NotImplementedError

    async def submit_exit(self, req: ExitRequest) -> ExitResult:
        raise NotImplementedError

    async def update_stop(
        self,
        trade_id: int,
        symbol: str,
        side: LegSide,
        qty: float,
        new_stop_price: float,
        client_order_id: str,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def cancel_all_for_trade(self, trade_id: int) -> int:
        self.cancel_calls.append(trade_id)
        return 2 if trade_id else 0


@pytest.mark.asyncio
async def test_kill_switch_trips_and_persists(tmp_path: Path) -> None:
    executor = _StubExecutor()
    kill = KillSwitch(executor, state_path=tmp_path / "shutdown.json")
    assert not kill.requested
    kill.trip(KillSwitchReason.CIRCUIT_BREAKER)
    assert kill.requested
    assert kill.reason is KillSwitchReason.CIRCUIT_BREAKER
    report = await kill.shutdown(open_trade_ids=[1, 2])
    assert executor.cancel_calls == [1, 2]
    assert report.cancellations == {1: 2, 2: 2}
    assert report.persisted_path is not None
    payload = json.loads(report.persisted_path.read_text())
    assert payload["reason"] == "circuit_breaker"


@pytest.mark.asyncio
async def test_kill_switch_polls_operator_flag(tmp_path: Path) -> None:
    executor = _StubExecutor()
    kill = KillSwitch(executor, state_path=tmp_path / "shutdown.json")
    flag = tmp_path / "kill.flag"
    assert not kill.poll_file(flag)
    flag.write_text("halt now")
    assert kill.poll_file(flag)
    assert kill.requested
    assert kill.reason is KillSwitchReason.OPERATOR_FILE


@pytest.mark.asyncio
async def test_kill_switch_is_idempotent(tmp_path: Path) -> None:
    executor = _StubExecutor()
    kill = KillSwitch(executor, state_path=tmp_path / "shutdown.json")
    kill.trip(KillSwitchReason.SIGTERM)
    kill.trip(KillSwitchReason.MANUAL)
    assert kill.reason is KillSwitchReason.SIGTERM
    r1 = await kill.shutdown(open_trade_ids=[7])
    r2 = await kill.shutdown(open_trade_ids=[7, 8])
    assert r1 is r2
    assert executor.cancel_calls == [7]  # second call short-circuits


@pytest.mark.asyncio
async def test_kill_switch_records_executor_error(tmp_path: Path) -> None:
    class _ErroringExecutor(_StubExecutor):
        async def cancel_all_for_trade(self, trade_id: int) -> int:
            raise RuntimeError("network down")

    executor = _ErroringExecutor()
    kill = KillSwitch(executor, state_path=tmp_path / "shutdown.json")
    kill.trip(KillSwitchReason.MANUAL)
    report = await kill.shutdown(open_trade_ids=[1])
    assert report.error is not None
    assert "network down" in report.error
    assert report.persisted_path is not None

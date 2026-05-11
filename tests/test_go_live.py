"""Tests for the per-strategy go-live gate."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
import yaml
from bot.runtime.go_live import GoLiveGate
from bot.storage import (
    Database,
    NavHistory,
    Trade,
    TradeStatus,
    init_database,
)


@pytest.fixture
async def db() -> Database:
    return await init_database(":memory:")


def _write_strategy_yaml(config_dir: Path, strategy_id: str, **overrides: object) -> Path:
    strategies_dir = config_dir / "strategies"
    strategies_dir.mkdir(parents=True, exist_ok=True)
    payload = {"id": strategy_id, "enabled": True, "enabled_live": False, **overrides}
    path = strategies_dir / f"{strategy_id}.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


async def _seed_dry_trades(
    db: Database,
    strategy_id: str,
    *,
    n_trades: int,
    distinct_days: int,
) -> None:
    base = dt.datetime(2026, 5, 1, 16, 0)
    async with db.session() as session:
        for i in range(n_trades):
            day_offset = i % distinct_days
            session.add(
                Trade(
                    strategy_id=strategy_id,
                    underlying="BTC",
                    entry_ts=base + dt.timedelta(days=day_offset, hours=-2),
                    exit_ts=base + dt.timedelta(days=day_offset),
                    status=TradeStatus.CLOSED.value,
                    mode="dry",
                    lots=1,
                    realised_pnl_inr=100.0 - i,
                )
            )


@pytest.mark.asyncio
async def test_go_live_fails_when_insufficient_evidence(db: Database, tmp_path: Path) -> None:
    _write_strategy_yaml(tmp_path, "directional")
    gate = GoLiveGate(db, config_dir=tmp_path)
    report = await gate.evaluate("directional")
    assert not report.passed
    failed = [c.name for c in report.checks if not c.passed]
    assert "min_dry_days" in failed
    assert "min_closed_trades" in failed
    # File not modified.
    yaml_payload = yaml.safe_load(report.yaml_path.read_text())  # type: ignore[arg-type]
    assert yaml_payload["enabled_live"] is False


@pytest.mark.asyncio
async def test_go_live_dry_run_does_not_write_yaml_on_pass(db: Database, tmp_path: Path) -> None:
    path = _write_strategy_yaml(tmp_path, "directional")
    await _seed_dry_trades(db, "directional", n_trades=25, distinct_days=12)
    gate = GoLiveGate(db, config_dir=tmp_path)
    report = await gate.evaluate("directional", dry_run=True)
    assert report.passed
    assert not report.written
    yaml_payload = yaml.safe_load(path.read_text())
    assert yaml_payload["enabled_live"] is False


@pytest.mark.asyncio
async def test_go_live_flips_yaml_when_all_checks_pass(db: Database, tmp_path: Path) -> None:
    path = _write_strategy_yaml(tmp_path, "directional")
    await _seed_dry_trades(db, "directional", n_trades=25, distinct_days=12)
    gate = GoLiveGate(db, config_dir=tmp_path)
    report = await gate.evaluate("directional")
    assert report.passed, [(c.name, c.detail) for c in report.checks if not c.passed]
    assert report.written
    yaml_payload = yaml.safe_load(path.read_text())
    assert yaml_payload["enabled_live"] is True
    # Other keys preserved.
    assert yaml_payload["enabled"] is True
    assert yaml_payload["id"] == "directional"


@pytest.mark.asyncio
async def test_go_live_blocked_by_tripped_circuit_breaker(db: Database, tmp_path: Path) -> None:
    _write_strategy_yaml(tmp_path, "directional")
    await _seed_dry_trades(db, "directional", n_trades=25, distinct_days=12)
    async with db.session() as session:
        session.add(
            NavHistory(
                trading_date=dt.date(2026, 5, 10),
                nav_inr=44_000.0,
                peak_nav_inr=52_000.0,
                drawdown_from_peak_pct=15.4,
                circuit_breaker_tripped=True,
            )
        )
    gate = GoLiveGate(db, config_dir=tmp_path)
    report = await gate.evaluate("directional")
    assert not report.passed
    cb = next(c for c in report.checks if c.name == "circuit_breaker_clean")
    assert not cb.passed
    assert "make resume" in cb.detail


@pytest.mark.asyncio
async def test_go_live_rejects_unknown_strategy(db: Database, tmp_path: Path) -> None:
    gate = GoLiveGate(db, config_dir=tmp_path)
    report = await gate.evaluate("definitely_not_a_strategy")
    assert not report.passed
    assert any(not c.passed and c.name == "known_strategy" for c in report.checks)


@pytest.mark.asyncio
async def test_go_live_iron_condor_thresholds_are_stricter(db: Database, tmp_path: Path) -> None:
    _write_strategy_yaml(tmp_path, "iron_condor")
    # 20 trades but only 12 distinct days — fails the 28-day check for iron_condor.
    await _seed_dry_trades(db, "iron_condor", n_trades=20, distinct_days=12)
    gate = GoLiveGate(db, config_dir=tmp_path)
    report = await gate.evaluate("iron_condor")
    assert not report.passed
    days_check = next(c for c in report.checks if c.name == "min_dry_days")
    assert not days_check.passed
    assert "28" in days_check.detail

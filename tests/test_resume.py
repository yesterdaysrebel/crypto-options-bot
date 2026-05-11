"""Tests for the manual circuit-breaker recovery service."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from bot.runtime.resume import ResumeService, format_report
from bot.storage import Database, NavHistory, init_database
from sqlalchemy import select


@pytest.fixture
async def db() -> Database:
    return await init_database(":memory:")


@pytest.mark.asyncio
async def test_resume_reports_no_history_when_empty(db: Database, tmp_path: Path) -> None:
    svc = ResumeService(db, runtime_dir=tmp_path)
    report = await svc.evaluate(confirm=True)
    assert not report.breaker_was_tripped
    assert not report.cleared
    assert "no NAV history" in " ".join(report.notes)


@pytest.mark.asyncio
async def test_resume_without_confirm_does_not_clear(db: Database, tmp_path: Path) -> None:
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
    svc = ResumeService(db, runtime_dir=tmp_path)
    report = await svc.evaluate(confirm=False)
    assert report.breaker_was_tripped
    assert not report.cleared
    async with db.session() as session:
        row = (await session.execute(select(NavHistory))).scalar_one()
    assert row.circuit_breaker_tripped is True
    text = format_report(report)
    assert "Breaker tripped:  YES" in text
    assert "Cleared this run: no" in text


@pytest.mark.asyncio
async def test_resume_with_confirm_clears_breaker_and_audits(db: Database, tmp_path: Path) -> None:
    async with db.session() as session:
        session.add(
            NavHistory(
                trading_date=dt.date(2026, 5, 11),
                nav_inr=43_200.0,
                peak_nav_inr=52_000.0,
                drawdown_from_peak_pct=16.9,
                circuit_breaker_tripped=True,
            )
        )
    # Place a shutdown marker that should be cleaned up.
    marker = tmp_path / "shutdown.json"
    marker.write_text("{}", encoding="utf-8")
    svc = ResumeService(db, runtime_dir=tmp_path)
    report = await svc.evaluate(confirm=True)
    assert report.cleared
    assert report.marker_removed == marker
    assert not marker.exists()
    async with db.session() as session:
        row = (await session.execute(select(NavHistory))).scalar_one()
    assert row.circuit_breaker_tripped is False
    audit = tmp_path / "resume_audit.jsonl"
    assert audit.exists()
    audit_line = audit.read_text(encoding="utf-8").strip()
    assert "resumed_at" in audit_line
    assert "16.9" in audit_line


@pytest.mark.asyncio
async def test_resume_is_idempotent_when_already_clear(db: Database, tmp_path: Path) -> None:
    async with db.session() as session:
        session.add(
            NavHistory(
                trading_date=dt.date(2026, 5, 12),
                nav_inr=50_000.0,
                peak_nav_inr=50_000.0,
                drawdown_from_peak_pct=0.0,
                circuit_breaker_tripped=False,
            )
        )
    svc = ResumeService(db, runtime_dir=tmp_path)
    report = await svc.evaluate(confirm=True)
    assert not report.breaker_was_tripped
    assert not report.cleared
    assert any("not currently tripped" in note for note in report.notes)

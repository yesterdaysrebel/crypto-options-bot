"""Manual circuit-breaker recovery.

When the lifetime peak-to-trough drawdown hits -15% NAV the bot trips its circuit
breaker, sets `nav_history.circuit_breaker_tripped = true` on every row from that day
forward, and refuses to trade. The only way out is `make resume --confirm`.

This module surfaces the current state, requires the operator to pass `--confirm`, and
on confirmation:
  * Clears the breaker flag on the most recent `NavHistory` row
  * Removes the runtime shutdown marker if present
  * Writes a `resumed_at` audit line into the marker file's parent for traceability
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from bot.storage.db import Database
from bot.storage.models import NavHistory


@dataclass
class ResumeReport:
    nav_inr: float | None
    peak_nav_inr: float | None
    drawdown_pct: float | None
    trading_date: dt.date | None
    breaker_was_tripped: bool
    cleared: bool
    confirmed: bool
    marker_removed: Path | None
    notes: list[str]


class ResumeService:
    def __init__(self, db: Database, *, runtime_dir: Path) -> None:
        self._db = db
        self._runtime_dir = runtime_dir
        self._runtime_dir.mkdir(parents=True, exist_ok=True)

    async def evaluate(self, *, confirm: bool) -> ResumeReport:
        notes: list[str] = []
        async with self._db.session() as session:
            latest = (
                await session.execute(select(NavHistory).order_by(NavHistory.trading_date.desc()).limit(1))
            ).scalar_one_or_none()
        if latest is None:
            notes.append("no NAV history rows found; nothing to resume from")
            return ResumeReport(
                nav_inr=None,
                peak_nav_inr=None,
                drawdown_pct=None,
                trading_date=None,
                breaker_was_tripped=False,
                cleared=False,
                confirmed=confirm,
                marker_removed=None,
                notes=notes,
            )
        tripped = bool(latest.circuit_breaker_tripped)
        if not tripped:
            notes.append("circuit breaker is not currently tripped")
        cleared = False
        marker_removed: Path | None = None
        if tripped and confirm:
            async with self._db.session() as session:
                row = (
                    await session.execute(
                        select(NavHistory).where(NavHistory.trading_date == latest.trading_date)
                    )
                ).scalar_one()
                row.circuit_breaker_tripped = False
            cleared = True
            notes.append(f"circuit breaker cleared on {latest.trading_date.isoformat()}")
            marker_removed = self._maybe_remove_marker()
            self._write_audit(latest)
        elif tripped and not confirm:
            notes.append("breaker is tripped; pass --confirm to clear")
        return ResumeReport(
            nav_inr=float(latest.nav_inr),
            peak_nav_inr=float(latest.peak_nav_inr),
            drawdown_pct=float(latest.drawdown_from_peak_pct),
            trading_date=latest.trading_date,
            breaker_was_tripped=tripped,
            cleared=cleared,
            confirmed=confirm,
            marker_removed=marker_removed,
            notes=notes,
        )

    def _maybe_remove_marker(self) -> Path | None:
        marker = self._runtime_dir / "shutdown.json"
        if marker.exists():
            marker.unlink()
            return marker
        return None

    def _write_audit(self, latest: NavHistory) -> Path:
        audit = self._runtime_dir / "resume_audit.jsonl"
        record = {
            "resumed_at": dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat(),
            "trading_date": latest.trading_date.isoformat(),
            "nav_inr": float(latest.nav_inr),
            "peak_nav_inr": float(latest.peak_nav_inr),
            "drawdown_pct": float(latest.drawdown_from_peak_pct),
        }
        with audit.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        return audit


def format_report(report: ResumeReport) -> str:
    lines = [
        f"NAV (latest):     ₹{report.nav_inr:,.2f}" if report.nav_inr is not None else "NAV (latest):     —",
        f"Peak NAV:         ₹{report.peak_nav_inr:,.2f}"
        if report.peak_nav_inr is not None
        else "Peak NAV:         —",
        f"Drawdown:         {report.drawdown_pct:.2f}%"
        if report.drawdown_pct is not None
        else "Drawdown:         —",
        f"Trading date:     {report.trading_date.isoformat() if report.trading_date else '—'}",
        f"Breaker tripped:  {'YES' if report.breaker_was_tripped else 'no'}",
        f"Confirm flag:     {'set' if report.confirmed else 'NOT set'}",
        f"Cleared this run: {'YES' if report.cleared else 'no'}",
    ]
    if report.marker_removed:
        lines.append(f"Removed marker:   {report.marker_removed}")
    if report.notes:
        lines.append("")
        for n in report.notes:
            lines.append(f"  - {n}")
    return "\n".join(lines)


__all__ = ["ResumeReport", "ResumeService", "format_report"]

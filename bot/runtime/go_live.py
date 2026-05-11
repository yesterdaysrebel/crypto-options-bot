"""Per-strategy go-live promotion gate.

A strategy is in dry-run mode (`enabled_live: false`) by default. Promoting it to live
flips that flag in the strategy's yaml file, which is then committed (manually) and
deployed via CD. The gate is intentionally *blunt*: it doesn't trade for you; it just
refuses to flip the flag until enough evidence has accumulated.

Checks per strategy (CHECK_THRESHOLDS):
  directional:   min_days=10, min_closed_trades=20
  iron_condor:   min_days=28, min_closed_trades=8
  vol_strangle:  min_days=28, min_closed_trades=4

Additional checks (always run):
  * SQLite integrity_check: PRAGMA integrity_check returns "ok"
  * Lifetime circuit breaker not currently tripped
  * Strategy is enabled at all (`enabled: true`)
  * Strategy yaml file exists and is parseable

The gate returns a structured `GoLiveReport` and *only* writes the new yaml if every
check passes. `dry_run=True` reports without writing.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import func, select

from bot.storage.db import Database
from bot.storage.models import NavHistory, Trade, TradeStatus

CHECK_THRESHOLDS: dict[str, tuple[int, int]] = {
    "directional": (10, 20),
    "iron_condor": (28, 8),
    "vol_strangle": (28, 4),
}


@dataclass
class GoLiveCheck:
    name: str
    passed: bool
    detail: str


@dataclass
class GoLiveReport:
    strategy_id: str
    checks: list[GoLiveCheck] = field(default_factory=list)
    yaml_path: Path | None = None
    written: bool = False

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def add(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append(GoLiveCheck(name=name, passed=ok, detail=detail))


class GoLiveGate:
    """Evaluates the go-live checklist for a single strategy and (optionally) flips its flag."""

    def __init__(self, db: Database, *, config_dir: Path) -> None:
        self._db = db
        self._config_dir = config_dir

    async def evaluate(self, strategy_id: str, *, dry_run: bool = False) -> GoLiveReport:
        report = GoLiveReport(strategy_id=strategy_id)
        thresholds = CHECK_THRESHOLDS.get(strategy_id)
        if thresholds is None:
            report.add("known_strategy", False, f"unknown strategy_id={strategy_id!r}")
            return report
        min_days, min_trades = thresholds

        yaml_path = self._config_dir / "strategies" / f"{strategy_id}.yaml"
        if not yaml_path.exists():
            report.add("yaml_present", False, f"missing {yaml_path}")
            return report
        report.yaml_path = yaml_path

        try:
            payload = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            report.add("yaml_parses", False, f"yaml parse error: {exc}")
            return report

        report.add("yaml_parses", True, f"parsed {yaml_path}")
        report.add(
            "strategy_enabled",
            bool(payload.get("enabled", True)),
            "strategy.enabled is true" if payload.get("enabled", True) else "strategy.enabled is false",
        )

        await self._check_db_integrity(report)
        await self._check_circuit_breaker(report)
        await self._check_min_days(report, strategy_id, min_days)
        await self._check_min_trades(report, strategy_id, min_trades)

        if report.passed and not dry_run:
            payload["enabled_live"] = True
            new_text = yaml.safe_dump(payload, sort_keys=False)
            yaml_path.write_text(new_text, encoding="utf-8")
            report.written = True
        return report

    async def _check_db_integrity(self, report: GoLiveReport) -> None:
        try:
            async with self._db.raw_session() as session:
                row = (await session.execute(select(func.coalesce(1, 1)))).scalar()
                # Use a raw connection for PRAGMA integrity_check.
                conn = await session.connection()
                result = await conn.exec_driver_sql("PRAGMA integrity_check")
                rows = result.fetchall()
                payload = rows[0][0] if rows else "unknown"
            ok = (payload == "ok") and (row == 1)
            report.add("db_integrity_check", ok, f"PRAGMA integrity_check={payload!r}")
        except Exception as exc:
            report.add("db_integrity_check", False, f"integrity check raised: {exc!r}")

    async def _check_circuit_breaker(self, report: GoLiveReport) -> None:
        async with self._db.session() as session:
            stmt = select(NavHistory).where(NavHistory.circuit_breaker_tripped == True)  # noqa: E712
            tripped = (await session.execute(stmt)).scalars().first()
        if tripped is None:
            report.add("circuit_breaker_clean", True, "no circuit-breaker rows tripped")
        else:
            report.add(
                "circuit_breaker_clean",
                False,
                f"circuit-breaker tripped on {tripped.trading_date.isoformat()} — run `make resume` first",
            )

    async def _check_min_days(self, report: GoLiveReport, strategy_id: str, min_days: int) -> None:
        async with self._db.session() as session:
            stmt = (
                select(func.count(func.distinct(func.date(Trade.exit_ts))))
                .where(Trade.strategy_id == strategy_id)
                .where(Trade.status == TradeStatus.CLOSED.value)
                .where(Trade.mode == "dry")
                .where(Trade.exit_ts.isnot(None))
            )
            days = int((await session.execute(stmt)).scalar() or 0)
        report.add(
            "min_dry_days",
            days >= min_days,
            f"{days} distinct dry-run days with closed trades (>= {min_days} required)",
        )

    async def _check_min_trades(self, report: GoLiveReport, strategy_id: str, min_trades: int) -> None:
        async with self._db.session() as session:
            stmt = (
                select(func.count(Trade.id))
                .where(Trade.strategy_id == strategy_id)
                .where(Trade.status == TradeStatus.CLOSED.value)
                .where(Trade.mode == "dry")
            )
            n = int((await session.execute(stmt)).scalar() or 0)
        report.add(
            "min_closed_trades",
            n >= min_trades,
            f"{n} closed dry-run trades (>= {min_trades} required)",
        )


def format_report(report: GoLiveReport) -> str:
    lines = [
        f"Go-live gate for {report.strategy_id!r} — {dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat()}",
        "",
    ]
    for c in report.checks:
        status = "PASS" if c.passed else "FAIL"
        lines.append(f"  [{status}] {c.name}: {c.detail}")
    lines.append("")
    lines.append(f"Result: {'ALL PASSED' if report.passed else 'FAILED'}")
    if report.written:
        lines.append(f"enabled_live=true written to {report.yaml_path}")
    elif report.passed:
        lines.append("(dry-run — yaml NOT modified)")
    return "\n".join(lines)


__all__ = ["CHECK_THRESHOLDS", "GoLiveCheck", "GoLiveGate", "GoLiveReport", "format_report"]

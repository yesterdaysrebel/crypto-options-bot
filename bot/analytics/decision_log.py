"""Decision logging pipeline.

Every tick, each strategy returns a list of `decision_records` (plain dicts) describing
the evaluation outcome — passed/failed, the feature vector, and the rejection reason if
any. The risk module ALSO emits decision records when it gates an intent (window,
drawdown caps, concurrency). The DecisionLogWriter persists all of them to the `decisions`
table in one batched commit and, optionally, mirrors them to a JSONL file for offline
analysis.

The schema is fixed: one row per (strategy_id, tick) plus one row per intent that the
risk module evaluated. The downstream analytics jobs aggregate this table to compute
filter-pass rates, would-trade counts, and reasons-for-block dashboards.

Performance: SQLite inserts are batched via `session.add_all` to keep tick latency low
(<5 ms for 9 strategies x 1 row each in benchmarks).
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from bot.storage.db import Database
from bot.storage.models import Decision, DecisionKind, DecisionReason

_KNOWN_REASONS = {r.value for r in DecisionReason}
_KNOWN_KINDS = {k.value for k in DecisionKind}


@dataclass(frozen=True)
class DecisionRecord:
    """In-memory representation. Strategies & risk emit dicts; we normalise via `from_dict`."""

    strategy_id: str
    kind: str
    underlying: str | None = None
    symbol: str | None = None
    passed: bool = False
    reason: str = DecisionReason.OTHER.value
    feature_vector: dict[str, Any] = field(default_factory=dict)
    ts: dt.datetime | None = None

    @staticmethod
    def from_dict(d: dict[str, Any]) -> DecisionRecord:
        kind = str(d.get("kind", DecisionKind.EVALUATE.value))
        if kind not in _KNOWN_KINDS:
            kind = DecisionKind.EVALUATE.value
        reason = str(d.get("reason", DecisionReason.OTHER.value))
        if reason not in _KNOWN_REASONS:
            reason = DecisionReason.OTHER.value
        return DecisionRecord(
            strategy_id=str(d["strategy_id"]),
            kind=kind,
            underlying=d.get("underlying"),
            symbol=d.get("symbol"),
            passed=bool(d.get("passed", False)),
            reason=reason,
            feature_vector=dict(d.get("feature_vector") or {}),
            ts=d.get("ts"),
        )


class DecisionLogWriter:
    """Batched writer for `decisions` rows. Optional JSONL mirror for replay/analytics."""

    def __init__(
        self,
        db: Database,
        *,
        mirror_path: Path | None = None,
        max_batch: int = 500,
    ) -> None:
        self._db = db
        self._mirror_path = mirror_path
        self._max_batch = max_batch
        if mirror_path is not None:
            mirror_path.parent.mkdir(parents=True, exist_ok=True)

    async def write(self, records: Iterable[dict[str, Any] | DecisionRecord]) -> int:
        normalised = [r if isinstance(r, DecisionRecord) else DecisionRecord.from_dict(r) for r in records]
        if not normalised:
            return 0
        n_total = 0
        for chunk in _chunked(normalised, self._max_batch):
            n_total += await self._write_chunk(chunk)
        if self._mirror_path is not None:
            self._mirror_jsonl(normalised)
        return n_total

    async def _write_chunk(self, records: Sequence[DecisionRecord]) -> int:
        async with self._db.session() as session:
            now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
            rows = [
                Decision(
                    ts=rec.ts or now,
                    strategy_id=rec.strategy_id,
                    kind=rec.kind,
                    symbol=rec.symbol,
                    underlying=rec.underlying,
                    passed=rec.passed,
                    reason=rec.reason,
                    feature_vector=rec.feature_vector or None,
                )
                for rec in records
            ]
            session.add_all(rows)
        return len(rows)

    def _mirror_jsonl(self, records: Sequence[DecisionRecord]) -> None:
        try:
            with self._mirror_path.open("a", encoding="utf-8") as f:  # type: ignore[union-attr]
                for rec in records:
                    f.write(
                        json.dumps(
                            {
                                "ts": (rec.ts or dt.datetime.now(dt.UTC).replace(tzinfo=None)).isoformat(),
                                "strategy_id": rec.strategy_id,
                                "kind": rec.kind,
                                "underlying": rec.underlying,
                                "symbol": rec.symbol,
                                "passed": rec.passed,
                                "reason": rec.reason,
                                "feature_vector": rec.feature_vector,
                            },
                            default=str,
                        )
                        + "\n"
                    )
        except OSError as exc:
            logger.warning("decision_log mirror write failed: {}", exc)


def _chunked(seq: Sequence[DecisionRecord], n: int) -> Iterable[Sequence[DecisionRecord]]:
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


__all__ = ["DecisionLogWriter", "DecisionRecord"]
